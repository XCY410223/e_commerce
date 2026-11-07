"""浏览历史读写（Redis List）。

Key: ``history:<user_id>``，元素为 SKU id，最近浏览置顶，只保留
``HISTORY_LIMIT`` 条。读历史时只返回仍在售的 SKU，避免下线商品
出现在列表里点进去 404。
"""

import logging

from .models import GoodsSKU

logger = logging.getLogger(__name__)

try:
    from django_redis import get_redis_connection
except ImportError:  # Redis 未安装时不影响页面渲染
    get_redis_connection = None

HISTORY_LIMIT = 5  # 浏览历史保留条数


def _history_key(user_id):
    return f"history:{user_id}"


def record_history(user_id, sku_id):
    """记录一次浏览；Redis 不可用时静默失败，不影响商品页。"""
    if get_redis_connection is None or not user_id:
        return False
    try:
        conn = get_redis_connection("default")
        key = _history_key(user_id)
        conn.lrem(key, 0, sku_id)  # 去重，保证最近访问置顶
        conn.lpush(key, sku_id)
        conn.ltrim(key, 0, HISTORY_LIMIT - 1)
    except Exception:
        logger.warning(
            "写入浏览历史失败 user_id=%s sku_id=%s", user_id, sku_id, exc_info=True
        )
        return False
    return True


def read_history(user_id, limit=None, exclude_sku_id=None):
    """按“最近浏览在前”返回在线 SKU。

    - 下线、软删除的商品会被跳过。
    - ``exclude_sku_id`` 用于详情页排除当前商品。
    """
    if get_redis_connection is None or not user_id:
        return []

    limit = HISTORY_LIMIT if limit is None else max(0, limit)
    if not limit:
        return []

    try:
        conn = get_redis_connection("default")
        raw_ids = conn.lrange(_history_key(user_id), 0, limit - 1)
    except Exception:
        logger.debug("读取浏览历史失败 user_id=%s", user_id, exc_info=True)
        return []

    sku_ids = []
    for raw_id in raw_ids:
        try:
            sku_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if sku_id not in sku_ids:
            sku_ids.append(sku_id)
    if not sku_ids:
        return []

    queryset = GoodsSKU.objects.online().filter(id__in=sku_ids)
    if exclude_sku_id is not None:
        queryset = queryset.exclude(id=exclude_sku_id)

    # 按 Redis 中的顺序还原，不能用 filter(id__in=...) 的默认排序
    mapping = {sku.id: sku for sku in queryset.prefetch_related("images")}
    return [mapping[sku_id] for sku_id in sku_ids if sku_id in mapping][:limit]
