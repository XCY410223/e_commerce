"""购物车 Redis 读写、库存校验与下单清理补偿。

键设计（Hash，field 为 sku_id，value 为数量）：
    cart:{user_id}       —— 登录用户，有效期 CART_TTL
    cart:guest:{token}   —— 游客，Cookie 里只放随机 token，有效期 GUEST_CART_TTL
游客购物车在登录成功后由 merge_guest_cart() 合并进账号购物车。
购物车与订单模块共用本模块，避免循环依赖。
"""

import hashlib
import logging
import re
import secrets
import time
from collections import namedtuple
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection

from goods.models import GoodsSKU

logger = logging.getLogger(__name__)

CART_CLEANUP_MARKER_TTL = 30 * 24 * 60 * 60
# 同一用户两次“补偿重试”之间的最小间隔，避免每个页面都打一次待办表
CLEANUP_RETRY_INTERVAL = 60

GUEST_CART_COOKIE = "guest_cart"
GUEST_CART_TTL = 7 * 24 * 60 * 60
_GUEST_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")

# 购物车引用：登录用户有 user_id，游客只有 key，两者共用同一套读写逻辑。
# seq 是同前缀的 ZSET，score 为加入时间，用来按“最近加入”排序。
CartRef = namedtuple("CartRef", "key ttl user_id seq")


class CartError(Exception):
    """可直接转换为 AJAX 错误响应的购物车业务异常。"""

    def __init__(self, message, *, code="cart_error", status_code=400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


_ADD_ITEM_SCRIPT = """
local current = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '0') or 0
if current < 0 then current = 0 end
local increment = tonumber(ARGV[2]) or 0
local stock = tonumber(ARGV[3]) or 0
local next_count = current + increment
if increment <= 0 or next_count > stock then
    return -1
end
redis.call('HSET', KEYS[1], ARGV[1], next_count)
redis.call('ZADD', KEYS[2], 'NX', tonumber(ARGV[5]) or 0, ARGV[1])
local ttl = tonumber(ARGV[4]) or 0
if ttl > 0 then
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('EXPIRE', KEYS[2], ttl)
end
return next_count
"""


_UPDATE_ITEM_SCRIPT = """
local current = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '0') or 0
local expected = tonumber(ARGV[2])
if expected >= 0 and current ~= expected then
    return -1
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
redis.call('ZADD', KEYS[2], 'NX', tonumber(ARGV[5]) or 0, ARGV[1])
local ttl = tonumber(ARGV[4]) or 0
if ttl > 0 then
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('EXPIRE', KEYS[2], ttl)
end
return tonumber(ARGV[3])
"""


_DELETE_ITEM_SCRIPT = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
local expected = ARGV[2]
if not current then
    if expected == '-1' then
        return 0
    end
    return -1
end
if expected ~= '-1' and tonumber(current) ~= tonumber(expected) then
    return -1
end
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
local ttl = tonumber(ARGV[3]) or 0
if ttl > 0 and redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('EXPIRE', KEYS[2], ttl)
end
return 1
"""


_NORMALIZE_SCRIPT = """
local ttl = tonumber(ARGV[1]) or 0
local changed = 0
local index = 2
while index <= #ARGV do
    local field = ARGV[index]
    local expected = ARGV[index + 1]
    local replacement = ARGV[index + 2]
    if redis.call('HGET', KEYS[1], field) == expected then
        if replacement == '-1' then
            redis.call('HDEL', KEYS[1], field)
            redis.call('ZREM', KEYS[2], field)
        else
            redis.call('HSET', KEYS[1], field, replacement)
        end
        changed = changed + 1
    end
    index = index + 3
end
if ttl > 0 and redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('EXPIRE', KEYS[2], ttl)
end
return changed
"""


_CLEANUP_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then
    return 0
end
local cart_ttl = tonumber(ARGV[1]) or 0
local marker_ttl = tonumber(ARGV[2]) or 2592000
local index = 3
while index <= #ARGV do
    local field = ARGV[index]
    local ordered = tonumber(ARGV[index + 1]) or 0
    local current = tonumber(redis.call('HGET', KEYS[1], field) or '0') or 0
    if ordered > 0 and current > 0 then
        if current <= ordered then
            redis.call('HDEL', KEYS[1], field)
            redis.call('ZREM', KEYS[3], field)
        else
            redis.call('HSET', KEYS[1], field, current - ordered)
        end
    end
    index = index + 2
end
if cart_ttl > 0 and redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('EXPIRE', KEYS[1], cart_ttl)
    redis.call('EXPIRE', KEYS[3], cart_ttl)
end
redis.call('SET', KEYS[2], '1', 'EX', marker_ttl)
return 1
"""


_MERGE_SCRIPT = """
local source = KEYS[1]
local target = KEYS[2]
local source_seq = KEYS[3]
local target_seq = KEYS[4]
local ttl = tonumber(ARGV[1]) or 0
if redis.call('EXISTS', source) == 0 then
    redis.call('DEL', source_seq)
    return 0
end
for _, field in ipairs(redis.call('HKEYS', source)) do
    local value = tonumber(redis.call('HGET', source, field) or '0') or 0
    if value > 0 then
        local current = tonumber(redis.call('HGET', target, field) or '0') or 0
        redis.call('HSET', target, field, current + value)
    end
end
local entries = redis.call('ZRANGE', source_seq, 0, -1, 'WITHSCORES')
local index = 1
while index < #entries do
    redis.call('ZADD', target_seq, tonumber(entries[index + 1]) or 0, entries[index])
    index = index + 2
end
if ttl > 0 then
    redis.call('EXPIRE', target, ttl)
    redis.call('EXPIRE', target_seq, ttl)
end
redis.call('DEL', source)
redis.call('DEL', source_seq)
return 1
"""


def cart_key(user_id):
    return f"cart:{user_id}"


def guest_cart_key(token):
    return f"cart:guest:{token}"


def valid_guest_token(token):
    """只接受自己生成的 token，避免用任意 Cookie 值拼出 Redis 键。"""
    return bool(_GUEST_TOKEN_PATTERN.match(token or ""))


def new_guest_token():
    return secrets.token_urlsafe(24)


def user_cart(user_id):
    key = cart_key(user_id)
    return CartRef(key, _cart_ttl(), user_id, f"{key}:seq")


def guest_cart(token):
    key = guest_cart_key(token)
    return CartRef(key, guest_cart_ttl(), None, f"{key}:seq")


def sku_cart_ref(request, create=False):
    """按请求主体解析购物车：登录用账号键，游客用 Cookie 里的 token 键。

    create=True 时允许为游客生成新 token，调用方负责把 token 写回 Cookie。
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user_cart(user.id)

    token = request.COOKIES.get(GUEST_CART_COOKIE, "")
    if not valid_guest_token(token):
        if not create:
            return None
        token = new_guest_token()
        request._guest_cart_new_token = token
    return guest_cart(token)


def _cleanup_marker_key(order_id):
    return f"cart:cleanup:{order_id}"


def _cart_ttl():
    return max(0, int(getattr(settings, "CART_TTL", 0) or 0))


def guest_cart_ttl():
    """游客购物车有效期，Cookie 的 max_age 也复用它，保证两边一致。"""
    return max(0, int(getattr(settings, "GUEST_CART_TTL", GUEST_CART_TTL) or 0))


def _refresh_ttl(conn, ref):
    if ref.ttl > 0 and conn.exists(ref.key):
        conn.expire(ref.key, ref.ttl)
        conn.expire(ref.seq, ref.ttl)


def _parse_add_count(value):
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise CartError("购买数量不正确", code="invalid_count") from exc
    if count <= 0:
        raise CartError("购买数量必须大于 0", code="invalid_count")
    return count


def _parse_update_count(value):
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CartError("购买数量不正确", code="invalid_count") from exc


def _available_sku(sku_id):
    """只允许存在且已上架的商品进入购物车（软删除由 Manager 默认过滤）。"""
    return GoodsSKU.objects.online().filter(pk=sku_id).first()


def _available_skus(sku_ids):
    """批量读取有效商品，供购物车明细和角标共用。"""
    if not sku_ids:
        return {}
    queryset = (
        GoodsSKU.objects.online()
        .filter(id__in=sku_ids)
        .prefetch_related("images")
    )
    return {sku.id: sku for sku in queryset}


def _parse_raw_cart(raw):
    counts = {}
    raw_by_id = {}
    cleanup_ops = []
    for raw_field, raw_value in raw.items():
        try:
            sku_id = int(raw_field)
            count = int(raw_value)
        except (TypeError, ValueError):
            cleanup_ops.append((raw_field, raw_value, None))
            continue
        if sku_id <= 0 or count <= 0:
            cleanup_ops.append((raw_field, raw_value, None))
            continue
        counts[sku_id] = count
        raw_by_id[sku_id] = (raw_field, raw_value)
    return counts, raw_by_id, cleanup_ops


def _apply_normalization(conn, ref, ops):
    """用 Lua 仅修改仍保持原值的字段，避免覆盖并发加购。"""
    args = [str(ref.ttl)]
    for field, expected, replacement in ops:
        args.extend(
            [
                field,
                expected,
                b"-1" if replacement is None else str(replacement).encode(),
            ]
        )
    conn.eval(_NORMALIZE_SCRIPT, 2, ref.key, ref.seq, *args)


def _retry_cleanup_throttled(user_id):
    """补偿重试按用户节流：先看缓存窗口，再确认是否真有待办，最后才重试。"""
    try:
        if not cache.add(f"cart:cleanup:check:{user_id}", 1, CLEANUP_RETRY_INTERVAL):
            return 0
        from cart.models import CartCleanupTask

        if not CartCleanupTask.objects.filter(user_id=user_id).exists():
            return 0
        return retry_pending_cart_cleanups(user_id=user_id)
    except Exception:  # 补偿表或缓存不可用时不能阻断正常购物车读取
        logger.warning("处理购物车清理补偿任务失败 user_id=%s", user_id, exc_info=True)
        return 0


def _sort_ids_by_seq(conn, ref, counts):
    """按 ZSET 里的加入时间倒序排列（最近加入在前），老数据补 0 分排最后。"""
    ordered = [int(member) for member in conn.zrevrange(ref.seq, 0, -1)]
    position = {sku_id: index for index, sku_id in enumerate(ordered) if sku_id in counts}

    missing = [sku_id for sku_id in counts if sku_id not in position]
    if missing:  # 兼容没有顺序记录的旧购物车，补一个最低分保持稳定
        conn.zadd(ref.seq, {str(sku_id): 0 for sku_id in missing})
        offset = len(position)
        for index, sku_id in enumerate(sorted(missing)):
            position[sku_id] = offset + index
        if ref.ttl > 0:
            conn.expire(ref.seq, ref.ttl)

    return sorted(counts, key=lambda sku_id: position[sku_id])


def _load_cart_state(ref, report=False):
    """返回经上架、软删除和库存规则校正后的购物车状态。

    report=True 时额外返回本次被移除的失效 SKU id，供购物车页提示用户。
    """
    if ref.user_id is not None:
        _retry_cleanup_throttled(ref.user_id)

    conn = get_redis_connection("default")
    raw = conn.hgetall(ref.key)
    if not raw:
        return ([], 0, Decimal("0.00"), []) if report else ([], 0, Decimal("0.00"))

    counts, raw_by_id, cleanup_ops = _parse_raw_cart(raw)
    skus = _available_skus(counts)
    items = []
    total_count = 0
    total_amount = Decimal("0.00")
    removed_ids = []

    for sku_id in _sort_ids_by_seq(conn, ref, counts):
        count = counts[sku_id]
        raw_field, raw_value = raw_by_id[sku_id]
        sku = skus.get(sku_id)
        if sku is None or sku.stock <= 0:
            cleanup_ops.append((raw_field, raw_value, None))
            removed_ids.append(sku_id)
            continue

        effective_count = min(count, sku.stock)
        if effective_count != count:
            cleanup_ops.append((raw_field, raw_value, effective_count))

        amount = sku.price * effective_count
        item = {"sku": sku, "count": effective_count, "amount": amount}
        if effective_count != count:
            item["adjusted"] = True
        items.append(item)
        total_count += effective_count
        total_amount += amount

    _apply_normalization(conn, ref, cleanup_ops)
    if report:
        return items, total_count, total_amount, removed_ids
    return items, total_count, total_amount


def read_cart(user_id):
    """读取有效购物车明细，并清理失效字段、校正超出库存的数量。"""
    return _load_cart_state(user_cart(user_id))


def cart_count(user_id):
    """角标与购物车页共用同一套有效商品和库存校正逻辑。"""
    return read_cart_ref(user_cart(user_id))[1]


def read_cart_ref(ref):
    """游客购物车与账号购物车共用的读取入口。"""
    return _load_cart_state(ref)


def read_cart_ref_report(ref):
    """购物车页专用：额外返回本次被自动移除的失效 SKU id。"""
    return _load_cart_state(ref, report=True)


def cart_count_ref(ref):
    return read_cart_ref(ref)[1]


def add_to_cart(user_id, sku_id, raw_count):
    """原子累加数量，并在 Redis 内保证累计值不超过当前库存。"""
    return add_to_cart_ref(user_cart(user_id), sku_id, raw_count)


def add_to_cart_ref(ref, sku_id, raw_count):
    """游客与账号共用的加购实现，ref 决定写入哪个 Redis 键。"""
    count = _parse_add_count(raw_count)
    sku = _available_sku(sku_id)
    if sku is None:
        raise CartError("商品已下架或不存在", code="item_unavailable", status_code=409)
    if sku.stock <= 0:
        raise CartError(f"「{sku.name}」暂时售罄", code="out_of_stock", status_code=409)
    if count > sku.stock:
        raise CartError(
            f"「{sku.name}」库存仅剩 {sku.stock} 件",
            code="stock_limit",
            status_code=409,
        )

    conn = get_redis_connection("default")
    result = conn.eval(
        _ADD_ITEM_SCRIPT,
        2,
        ref.key,
        ref.seq,
        str(sku.id),
        str(count),
        str(sku.stock),
        str(ref.ttl),
        str(time.time()),
    )
    if int(result) < 0:
        raise CartError(
            f"「{sku.name}」库存仅剩 {sku.stock} 件，购物车数量不能超过库存",
            code="stock_limit",
            status_code=409,
        )
    return int(result)


def update_cart_item(user_id, sku_id, raw_count, expected=None):
    return update_cart_item_ref(user_cart(user_id), sku_id, raw_count, expected)


def update_cart_item_ref(ref, sku_id, raw_count, expected=None):
    """设置绝对数量；0 或负数表示删除，正数不得超过当前库存。

    expected 不为 None 时按“页面看到的值”做 CAS：Redis 里的数量已被其他
    标签页改过就返回 cart_conflict，避免后写覆盖。
    """
    count = _parse_update_count(raw_count)
    conn = get_redis_connection("default")

    if count <= 0:
        delete_cart_item_ref(ref, sku_id, expected=expected)
        return 0

    sku = _available_sku(sku_id)
    if sku is None:
        raise CartError("商品已下架或不存在", code="item_unavailable", status_code=409)
    if sku.stock <= 0:
        raise CartError(f"「{sku.name}」暂时售罄", code="out_of_stock", status_code=409)
    if count > sku.stock:
        raise CartError(
            f"「{sku.name}」库存仅剩 {sku.stock} 件",
            code="stock_limit",
            status_code=409,
        )

    result = conn.eval(
        _UPDATE_ITEM_SCRIPT,
        2,
        ref.key,
        ref.seq,
        str(sku.id),
        "-1" if expected is None else str(expected),
        str(count),
        str(ref.ttl),
        str(time.time()),
    )
    if int(result) < 0:
        raise CartError(
            "购物车已在其他页面发生变化，请刷新后重试",
            code="cart_conflict",
            status_code=409,
        )
    return count


def delete_cart_item(user_id, sku_id):
    return delete_cart_item_ref(user_cart(user_id), sku_id)


def delete_cart_item_ref(ref, sku_id, expected=None):
    """原子删除；expected 不为 None 时只在数量仍与页面一致时删除。"""
    conn = get_redis_connection("default")
    result = conn.eval(
        _DELETE_ITEM_SCRIPT,
        2,
        ref.key,
        ref.seq,
        str(sku_id),
        "-1" if expected is None else str(expected),
        str(ref.ttl),
    )
    if int(result) < 0:
        raise CartError(
            "购物车已在其他页面发生变化，请刷新后重试",
            code="cart_conflict",
            status_code=409,
        )
    return bool(int(result))


def clear_cart(user_id):
    """显式清空整个购物车；下单流程使用更精确的幂等清理。"""
    conn = get_redis_connection("default")
    ref = user_cart(user_id)
    conn.delete(ref.key, ref.seq)


def merge_guest_cart(user_id, token):
    """登录后把游客购物车并入账号购物车，数量相加并在读取时按库存收敛。"""
    if not valid_guest_token(token):
        return False

    conn = get_redis_connection("default")
    merged = conn.eval(
        _MERGE_SCRIPT,
        4,
        guest_cart_key(token),
        cart_key(user_id),
        guest_cart_key(token) + ":seq",
        cart_key(user_id) + ":seq",
        str(_cart_ttl()),
    )
    return bool(int(merged))


def _snapshot_ordered_counts(items):
    counts = {}
    for item in items:
        sku_id = str(item["sku"].id)
        count = int(item["count"])
        if count > 0:
            counts[sku_id] = counts.get(sku_id, 0) + count
    return counts


def cart_fingerprint(items):
    """生成购物车内容指纹，供确认页防止“总件数/总金额相同但商品组成变化”。"""
    parts = []
    for item in sorted(items, key=lambda entry: entry["sku"].id):
        parts.append(
            f"{item['sku'].id}:{int(item['count'])}:{item['sku'].price:.2f}"
        )
    return hashlib.sha256("|".join(parts).encode("ascii")).hexdigest()


def _run_cart_cleanup(user_id, ordered_counts, order_id):
    """按订单快照扣减购物车；同一 order_id 通过 Redis 标记保证幂等。"""
    if not ordered_counts:
        return 0
    conn = get_redis_connection("default")
    cart_ttl = _cart_ttl()
    marker_ttl = max(CART_CLEANUP_MARKER_TTL, cart_ttl)
    args = [str(cart_ttl), str(marker_ttl)]
    for sku_id, count in ordered_counts.items():
        args.extend([sku_id, str(count)])
    return int(
        conn.eval(
            _CLEANUP_SCRIPT,
            3,
            cart_key(user_id),
            _cleanup_marker_key(order_id),
            cart_key(user_id) + ":seq",
            *args,
        )
    )


def _queue_cart_cleanup(user_id, ordered_counts, order_id, error):
    try:
        from cart.models import CartCleanupTask

        CartCleanupTask.objects.update_or_create(
            order_id=order_id,
            defaults={
                "user_id": user_id,
                "items": ordered_counts,
                "last_error": f"{type(error).__name__}: {error}"[:2000],
            },
        )
    except Exception:
        logger.exception("写入购物车清理补偿任务失败 order_id=%s", order_id)


def clear_cart_after_order(user_id, items, order_id):
    """下单成功后清理已购买数量；失败时落库等待补偿。"""
    ordered_counts = _snapshot_ordered_counts(items)
    try:
        _run_cart_cleanup(user_id, ordered_counts, order_id)
    except Exception as exc:
        logger.warning(
            "下单后清理购物车失败，已进入补偿流程 user_id=%s order_id=%s",
            user_id,
            order_id,
            exc_info=True,
        )
        _queue_cart_cleanup(user_id, ordered_counts, order_id, exc)
        return False
    return True


def retry_pending_cart_cleanups(user_id=None, limit=100):
    """重试数据库中的购物车清理待办，返回成功处理的条数。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 100
    if limit <= 0:
        return 0

    from cart.models import CartCleanupTask

    queryset = CartCleanupTask.objects.order_by("create_time")
    if user_id is not None:
        queryset = queryset.filter(user_id=user_id)

    processed = 0
    for task in list(queryset[:limit]):
        try:
            _run_cart_cleanup(task.user_id, task.items or {}, task.order_id)
        except Exception as exc:
            task.attempts += 1
            task.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            task.save(update_fields=["attempts", "last_error", "update_time"])
            logger.warning(
                "购物车清理补偿重试失败 task_id=%s order_id=%s",
                task.pk,
                task.order_id,
                exc_info=True,
            )
        else:
            task.delete()
            processed += 1
    return processed
