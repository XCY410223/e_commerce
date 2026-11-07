"""订单模块视图（骨架实现）。

已完成：确认订单页、事务下单（按主键升序加锁 + 校验库存 + 扣减 + 累加销量）、
用户级串行化、提交令牌幂等、死锁/锁等待重试、订单列表、支付结果通知/查询、
超时未支付自动取消与库存回补。
待补：真实渠道退款与售后流程。
"""

import logging
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, OperationalError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import ListView
from django_redis import get_redis_connection
from redis.exceptions import RedisError

from goods.models import GoodsSKU
from promotion.services import (
    PricingError,
    available_coupons,
    build_quote,
    consume_coupon,
    ensure_new_customer_coupon,
    record_order_promotions,
)
from user.models import Address, User
from utils.cart import cart_fingerprint, clear_cart_after_order, read_cart
from utils.pagination import PaginationContextMixin

from . import payment
from .forms import OrderCommentForm
from .models import OrderComment, OrderGoods, OrderInfo
from .services import CommentError, submit_order_comment

logger = logging.getLogger(__name__)

# 提交令牌：每个用户只保留最新一张确认页的令牌，重新打开确认页会作废旧令牌
SUBMIT_TOKEN_TTL = 600
SUBMIT_TOKEN_KEY = "order:submit:{user_id}"

# 用户级下单锁：跨越“事务提交 + 清理购物车”整段临界区。数据库行锁只持续到
# 事务结束，无法阻止另一个请求在提交后、清空购物车之前读到旧购物车。
ORDER_LOCK_KEY = "order:lock:{user_id}"
ORDER_LOCK_TTL_MS = 30_000
ORDER_LOCK_RENEW_INTERVAL = 10.0
ORDER_LOCK_WAIT_SECONDS = 5.0
ORDER_LOCK_RETRY_INTERVAL = 0.05

# InnoDB 1213=死锁，1205=锁等待超时，这类错误重试即可
RETRYABLE_DB_ERRORS = {1213, 1205}
MAX_SUBMIT_ATTEMPTS = 3


# 只有当前令牌持有者能删除它，GET 与 DEL 在 Redis 内一步完成
_CLAIM_TOKEN_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_ACQUIRE_ORDER_LOCK_SCRIPT = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
    return 1
end
return 0
"""

_RELEASE_ORDER_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_RENEW_ORDER_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class OrderBusy(Exception):
    """同一用户已有下单请求在排队，当前请求等待超时。"""


def _submit_token_key(user_id):
    return SUBMIT_TOKEN_KEY.format(user_id=user_id)


def _issue_submit_token(user_id):
    """覆盖写入：同一用户只认最后一次打开的确认页。"""
    token = secrets.token_urlsafe(16)
    get_redis_connection("default").set(_submit_token_key(user_id), token, ex=SUBMIT_TOKEN_TTL)
    return token


def _consume_submit_token(user_id, token):
    """比较并删除：令牌不是当前值时消费失败，并发提交只有一个能通过。

    不携带令牌一律拒绝：确认页每次都会下发令牌，缺失说明是绕过页面的请求，
    放行会让下单幂等和金额指纹比对形同虚设。
    """
    if not token:
        return False
    return bool(
        get_redis_connection("default").eval(
            _CLAIM_TOKEN_SCRIPT, 1, _submit_token_key(user_id), token
        )
    )


@contextmanager
def _user_order_lock(user_id):
    """按用户串行化下单，覆盖事务提交与购物车清理。

    令牌只能拦住同一个确认页的重复提交；用户在新标签页重新打开确认页会拿到
    新令牌。这里用带过期时间的 Redis 锁把同一用户的整段下单流程排队，避免
    第二个请求在第一个事务提交后、购物车清空前进场。
    """
    conn = get_redis_connection("default")
    key = ORDER_LOCK_KEY.format(user_id=user_id)
    lock_token = secrets.token_urlsafe(12)
    deadline = time.monotonic() + ORDER_LOCK_WAIT_SECONDS
    while True:
        acquired = conn.eval(
            _ACQUIRE_ORDER_LOCK_SCRIPT, 1, key, lock_token, str(ORDER_LOCK_TTL_MS)
        )
        if int(acquired) == 1:
            break
        if time.monotonic() >= deadline:
            raise OrderBusy("当前下单人数较多，请稍后重试")
        time.sleep(ORDER_LOCK_RETRY_INTERVAL)

    stop_renew = threading.Event()

    def renew_lock():
        """后台续期，避免数据库或购物车清理耗时超过锁 TTL。"""
        while not stop_renew.wait(ORDER_LOCK_RENEW_INTERVAL):
            try:
                heartbeat = get_redis_connection("default")
                renewed = heartbeat.eval(
                    _RENEW_ORDER_LOCK_SCRIPT,
                    1,
                    key,
                    lock_token,
                    str(ORDER_LOCK_TTL_MS),
                )
                if int(renewed) != 1:
                    logger.error("下单锁已失效 user_id=%s", user_id)
                    return
            except Exception:
                logger.warning("续期下单锁失败 user_id=%s", user_id, exc_info=True)

    renew_thread = threading.Thread(
        target=renew_lock,
        name=f"order-lock-{user_id}",
        daemon=True,
    )
    renew_thread.start()
    try:
        yield
    finally:
        stop_renew.set()
        renew_thread.join(timeout=1)
        try:
            conn.eval(_RELEASE_ORDER_LOCK_SCRIPT, 1, key, lock_token)
        except Exception:
            logger.exception("释放下单锁失败 user_id=%s", user_id)


def _db_error_code(exc):
    return exc.args[0] if exc.args and isinstance(exc.args[0], int) else None


def _new_order_id():
    return f"{datetime.now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:6]}"


def _parse_expected_count(value):
    """确认页提交的商品件数快照；缺省时兼容旧页面和脚本。"""
    if value in (None, ""):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("确认页数据已失效，请返回购物车重新结算") from exc
    if count < 0:
        raise ValueError("确认页数据已失效，请返回购物车重新结算")
    return count


def _parse_expected_amount(value):
    """确认页提交的金额快照，避免购物车或价格变化后被静默下单。"""
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("确认页数据已失效，请返回购物车重新结算") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("确认页数据已失效，请返回购物车重新结算")
    return amount


def _parse_expected_fingerprint(value):
    """确认页提交的购物车内容指纹；缺省时兼容旧页面和脚本。"""
    if value in (None, ""):
        return None
    fingerprint = str(value).strip().lower()
    if len(fingerprint) != 64 or any(
        char not in "0123456789abcdef" for char in fingerprint
    ):
        raise ValueError("确认页数据已失效，请返回购物车重新结算")
    return fingerprint


def _parse_coupon_id(value):
    """0 或空值表示不使用券；其余必须是正整数主键，不能传任意对象。"""
    if value in (None, "", 0, "0"):
        return None
    try:
        coupon_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("优惠券参数无效，请刷新页面后重试") from exc
    if coupon_id <= 0:
        raise ValueError("优惠券参数无效，请刷新页面后重试")
    return coupon_id


def _get_posted_address(request):
    """校验 POST 中的地址 ID，非法值转换为可提示的业务异常。"""
    try:
        address_id = int(request.POST.get("addr_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError("请选择有效的收货地址") from exc
    addr = Address.objects.for_user(request.user).filter(pk=address_id).first()
    if addr is None:
        raise ValueError("收货地址不存在或已删除，请重新选择")
    return addr


def _lock_skus(items):
    """按主键升序一次锁住整批 SKU，避免两笔订单交叉加锁造成死锁。"""
    sku_ids = sorted({item["sku"].id for item in items})
    locked = (
        GoodsSKU.objects.select_for_update()
        .filter(id__in=sku_ids)
        .order_by("id")
    )
    return {sku.id: sku for sku in locked}


def _write_order(user, addr, pay_method, items, expected_amount, coupon_id=None):
    """事务体：锁库存、校验、扣减、写订单与明细。

    总额按锁定后的价格重算，与 OrderGoods 的快照价保持同源；若与购物车
    展示的金额不一致，说明期间调过价，直接让用户回购物车重新确认。
    """
    locked = _lock_skus(items)
    locked_items = []

    for item in items:
        sku = locked.get(item["sku"].id)
        if sku is None or sku.status != GoodsSKU.STATUS_ONLINE:
            raise ValueError(f"「{item['sku'].name}」已下架，请从购物车移除后重试")
        if sku.stock < item["count"]:
            raise ValueError(f"「{sku.name}」库存不足，仅剩 {sku.stock} 件")
        locked_items.append({"sku": sku, "count": item["count"]})

    quote = build_quote(
        user,
        locked_items,
        coupon_id=coupon_id,
        lock_coupon=True,
    )
    if expected_amount is not None and quote.total_pay != expected_amount:
        raise ValueError("商品价格已变化，请返回购物车确认后重新下单")

    order = OrderInfo.objects.create(
        order_id=_new_order_id(),
        user=user,
        addr=addr,
        pay_method=pay_method,
        total_count=quote.total_count,
        total_amount=quote.goods_original_total,
        original_total_amount=quote.original_total_amount,
        item_discount=quote.item_discount,
        coupon_discount=quote.coupon_discount,
        shipping_discount=quote.shipping_discount,
        total_discount=quote.total_discount,
        transit_price=quote.shipping_fee,
        receiver=addr.receiver,
        phone=addr.phone,
        address=addr.addr,
        pay_status=OrderInfo.STATUS_UNPAID,
    )

    for line in quote.lines:
        sku = line.sku
        sku.stock -= line.count
        sku.sales += line.count
        sku.save(update_fields=["stock", "sales", "update_time"])
        OrderGoods.objects.create(
            order=order,
            sku=sku,
            sku_name=sku.name,
            count=line.count,
            original_price=line.original_unit_price,
            price=line.unit_price,
            discount_amount=line.discount_amount,
            promotion_name=line.promotion_name,
        )

    consume_coupon(quote.coupon, order)
    record_order_promotions(order, quote)
    logger.info(
        "订单优惠快照已写入 order_id=%s user_id=%s original=%s item_discount=%s "
        "coupon_discount=%s shipping=%s shipping_discount=%s total_discount=%s total_pay=%s",
        order.order_id,
        user.pk,
        quote.goods_original_total,
        quote.item_discount,
        quote.coupon_discount,
        quote.shipping_fee,
        quote.shipping_discount,
        quote.total_discount,
        quote.total_pay,
    )
    return order, quote.total_count


def _create_order(
    user,
    addr,
    pay_method,
    expected_amount=None,
    expected_count=None,
    expected_fingerprint=None,
    coupon_id=None,
):
    """带重试的下单入口。

    外层 Redis 用户锁覆盖“提交事务 + 清理购物车”，同一用户的并发请求会
    排队；事务内再锁一次用户行，作为数据库层面的兜底。第二个请求拿到锁时
    购物车已清空，会直接结束，不会重复下单。死锁/锁等待超时重试，订单号
    冲突重新生成。
    """
    with _user_order_lock(user.id):
        for attempt in range(1, MAX_SUBMIT_ATTEMPTS + 1):
            try:
                with transaction.atomic():
                    User.objects.select_for_update().filter(pk=user.pk).first()

                    # 地址读取发生在用户锁之前，期间可能已被另一个标签页删除。
                    # 拿到用户锁后重新确认，避免给已删除地址继续下单。
                    valid_addr = Address.objects.for_user(user).filter(pk=addr.pk).first()
                    if valid_addr is None:
                        raise ValueError("收货地址已删除，请重新选择")
                    addr = valid_addr

                    items, total_count, total_amount = read_cart(user.id)
                    if not items:
                        if expected_count not in (None, 0):
                            raise ValueError(
                                "购物车商品已变化或库存不足，请返回购物车重新确认"
                            )
                        raise ValueError("购物车是空的，请先去挑选商品")

                    if expected_count is not None and total_count != expected_count:
                        raise ValueError("购物车商品数量已变化，请返回确认页重新核对")
                    if (
                        expected_fingerprint is not None
                        and cart_fingerprint(items) != expected_fingerprint
                    ):
                        raise ValueError("购物车商品已变化，请返回确认页重新核对")

                    order, _ = _write_order(
                        user,
                        addr,
                        pay_method,
                        items,
                        expected_amount,
                        coupon_id=coupon_id,
                    )

                # 事务提交后再清理购物车：Redis 不参与数据库事务，避免提交阶段
                # 失败回滚时订单不存在、购物车却已经被清空。Redis 用户锁仍持有，
                # 其他下单请求不会在这个空档读到旧购物车。
                cart_cleared = clear_cart_after_order(user.id, items, order.order_id)
                return order, cart_cleared
            except OperationalError as exc:
                if _db_error_code(exc) not in RETRYABLE_DB_ERRORS or attempt == MAX_SUBMIT_ATTEMPTS:
                    raise
                logger.warning("下单遇到数据库锁冲突，第 %s 次重试", attempt, exc_info=True)
            except IntegrityError as exc:
                if "order_id" not in str(exc) or attempt == MAX_SUBMIT_ATTEMPTS:
                    raise
                logger.warning("订单号冲突，重新生成后重试", exc_info=True)
            time.sleep(0.05 * attempt)


@login_required
def place(request):
    """确认订单页：展示购物车明细、收货地址、支付方式。"""
    try:
        items, total_count, total_amount = read_cart(request.user.id)
    except RedisError:
        logger.exception("读取购物车失败，无法进入确认订单页 user_id=%s", request.user.id)
        messages.error(request, "购物车服务暂时不可用，请稍后重试")
        return redirect("goods:index")
    if not items:
        messages.info(request, "购物车是空的，先去挑点东西吧")
        return redirect("cart:index")

    # 已激活但注册时活动尚未配置的老用户，在进入结算页时补发一次；发放幂等。
    ensure_new_customer_coupon(request.user)
    coupons = available_coupons(request.user)
    explicit_coupon = request.GET.get("coupon_id") is not None
    try:
        selected_coupon_id = _parse_coupon_id(request.GET.get("coupon_id"))
    except ValueError as exc:
        messages.error(request, str(exc))
        selected_coupon_id = None
    if not explicit_coupon and coupons:
        selected_coupon_id = coupons[0].pk

    try:
        quote = build_quote(request.user, items, coupon_id=selected_coupon_id)
    except PricingError as exc:
        if explicit_coupon:
            messages.error(request, str(exc))
        selected_coupon_id = None
        quote = build_quote(request.user, items, coupon_id=None)

    addresses = Address.objects.for_user(request.user)
    default_address = Address.objects.default_for(request.user)

    return render(
        request,
        "order/place.html",
        {
            "items": items,
            "quote": quote,
            "total_count": quote.total_count,
            "total_amount": quote.goods_original_total,
            "transit_price": quote.shipping_fee,
            "total_pay": quote.total_pay,
            "coupons": coupons,
            "selected_coupon_id": selected_coupon_id,
            "addresses": addresses,
            "default_address": default_address,
            "pay_choices": OrderInfo.PAY_CHOICES,
            "submit_token": _issue_submit_token(request.user.id),
            "cart_fingerprint": cart_fingerprint(items),
        },
    )


@login_required
@require_POST
def commit(request):
    """提交订单：事务内校验并扣库存、写订单明细，成功后幂等清理购物车。"""
    # 先校验地址和确认页快照，再消费令牌：参数非法时不会白白作废这张确认页
    try:
        addr = _get_posted_address(request)
        expected_count = _parse_expected_count(request.POST.get("expected_count"))
        expected_amount = _parse_expected_amount(request.POST.get("expected_amount"))
        expected_fingerprint = _parse_expected_fingerprint(
            request.POST.get("expected_fingerprint")
        )
        coupon_id = _parse_coupon_id(request.POST.get("coupon_id"))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("cart:index")

    try:
        token_ok = _consume_submit_token(
            request.user.id, request.POST.get("submit_token", "")
        )
    except RedisError:
        logger.exception("校验提交令牌失败 user_id=%s", request.user.id)
        messages.error(request, "下单服务暂时不可用，请稍后重试")
        return redirect("cart:index")
    if not token_ok:
        logger.warning("重复提交被拦截 user_id=%s", request.user.id)
        messages.warning(request, "订单已提交或页面已过期，请勿重复提交")
        return redirect("order:list")

    try:
        pay_method = int(request.POST.get("pay_method", OrderInfo.PAY_ALIPAY))
    except (TypeError, ValueError):
        pay_method = OrderInfo.PAY_ALIPAY
    if pay_method not in dict(OrderInfo.PAY_CHOICES):
        pay_method = OrderInfo.PAY_ALIPAY

    try:
        order, cart_cleared = _create_order(
            request.user,
            addr,
            pay_method,
            expected_amount=expected_amount,
            expected_count=expected_count,
            expected_fingerprint=expected_fingerprint,
            coupon_id=coupon_id,
        )
    except OrderBusy as exc:
        logger.warning("下单等待用户锁超时 user_id=%s", request.user.id)
        messages.warning(request, str(exc))
        return redirect("cart:index")
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("cart:index")
    except OperationalError:
        logger.exception("下单失败：锁冲突重试耗尽 user_id=%s", request.user.id)
        messages.error(request, "当前下单人数较多，请稍后重试")
        return redirect("cart:index")
    except IntegrityError:
        logger.exception("下单失败：订单号写入冲突 user_id=%s", request.user.id)
        messages.error(request, "订单号生成冲突，请重新提交")
        return redirect("cart:index")
    except RedisError:
        logger.exception("下单失败：缓存服务不可用 user_id=%s", request.user.id)
        messages.error(request, "下单服务暂时不可用，请稍后重试")
        return redirect("cart:index")

    messages.success(request, f"订单 {order.order_id} 提交成功")
    if not cart_cleared:
        messages.warning(request, "购物车清理暂时未完成，系统会在后续访问时自动重试")
    return redirect("order:list")


class OrderListView(LoginRequiredMixin, PaginationContextMixin, ListView):
    """当前用户的订单列表。"""

    model = OrderInfo
    template_name = "order/list.html"
    paginate_by = 10

    def get_queryset(self):
        queryset = (
            OrderInfo.objects.for_user(self.request.user)
            .select_related("addr")
        .prefetch_related("skus__sku", "promotions", "used_coupons")
        )
        status = self.request.GET.get("status", "")
        if status == "unpaid":
            queryset = queryset.filter(pay_status=OrderInfo.STATUS_UNPAID)
        elif status == "paid":
            queryset = queryset.filter(pay_status=OrderInfo.STATUS_PAID)
        elif status == "cancelled":
            queryset = queryset.filter(pay_status=OrderInfo.STATUS_CANCELLED)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        status = self.request.GET.get("status", "")
        context["status_filter"] = (
            status if status in {"unpaid", "paid", "cancelled"} else ""
        )
        return context


@login_required
def order_detail(request, order_id):
    """订单详情：只允许查看当前用户自己的订单。"""
    order = get_object_or_404(
        OrderInfo.objects.for_user(request.user)
        .select_related("addr")
        .prefetch_related("skus__sku", "skus__review", "promotions", "used_coupons"),
        order_id=order_id,
    )
    return render(
        request,
        "order/detail.html",
        {
            "order": order,
            "comment_max_length": OrderComment.MAX_CONTENT_LENGTH,
            "comment_enabled": settings.ORDER_COMMENT_ENABLED,
        },
    )


@login_required
@require_POST
def order_comment_submit(request, order_goods_id):
    """提交订单商品评论；所有者和支付状态由视图与服务层双重校验。"""
    if not settings.ORDER_COMMENT_ENABLED:
        messages.warning(request, "评论功能暂时关闭，请稍后再试")
        return redirect("order:list")

    line = get_object_or_404(
        OrderGoods.objects.select_related("order").filter(
            order__user=request.user
        ),
        pk=order_goods_id,
    )
    redirect_url = (
        reverse("order:detail", args=[line.order.order_id]) + "#comments"
    )

    form = OrderCommentForm(request.POST)
    if not form.is_valid():
        errors = form.errors.get("content")
        messages.error(
            request,
            errors[0] if errors else "评论内容不合法",
        )
        return redirect(redirect_url)

    try:
        _, created = submit_order_comment(
            request.user,
            line.pk,
            form.cleaned_data["content"],
        )
    except CommentError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request,
            "评论已提交，等待审核" if created else "评论已重新提交，等待审核",
        )
    return redirect(redirect_url)


@login_required
def pay(request, order_id):
    """发起支付：生成收银台地址并跳转，已支付/已取消订单直接回列表。"""
    order = get_object_or_404(
        OrderInfo.objects.for_user(request.user), order_id=order_id
    )
    if order.pay_status == OrderInfo.STATUS_PAID:
        messages.info(request, "订单已支付，无需重复支付")
        return redirect("order:list")
    if order.pay_status == OrderInfo.STATUS_CANCELLED:
        messages.warning(request, "订单已取消，不能继续支付")
        return redirect("order:list")

    try:
        intent = payment.create_payment(order, request)
    except payment.PaymentError as exc:
        logger.warning("发起支付失败 order_id=%s: %s", order_id, exc)
        messages.error(request, str(exc))
        return render(request, "order/pay.html", {"order": order, "payment_error": str(exc)})
    return redirect(intent.url)


@login_required
@require_GET
def pay_status(request, order_id):
    """轮询支付结果：必要时主动向渠道查询，并返回当前订单状态。"""
    order = get_object_or_404(
        OrderInfo.objects.for_user(request.user), order_id=order_id
    )
    order = payment.refresh_order_status(order)
    return JsonResponse(
        {
            "order_id": order.order_id,
            "pay_status": order.pay_status,
            "pay_status_display": order.get_pay_status_display(),
            "trade_no": order.trade_no,
            "paid_amount": str(order.paid_amount) if order.paid_amount is not None else None,
            "pay_time": order.pay_time.isoformat() if order.pay_time else None,
            "cancelled": order.is_cancelled,
            "redirect_url": reverse("order:list"),
        }
    )


@csrf_exempt
@require_POST
def pay_notify(request, channel):
    """支付平台异步通知入口：验签、幂等落库、按平台要求返回确认。"""
    try:
        provider = payment.get_provider(channel)
        result = provider.verify_notify(request)
    except payment.PaymentError as exc:
        logger.warning("支付通知校验失败 channel=%s: %s", channel, exc)
        return HttpResponse("failure", status=400, content_type="text/plain")

    ok, reason = payment.apply_result(result, expected_channel=provider.name)
    if reason == "order_cancelled":
        logger.critical(
            "已取消订单收到渠道通知 channel=%s order_id=%s trade_no=%s",
            channel,
            result.out_trade_no,
            result.trade_no,
        )
        # 已取消订单无法恢复为已支付，先确认通知避免平台无限重试；
        # 真实渠道需由对账/退款流程处理这笔迟到的款项。
        return HttpResponse(provider.notify_ack, content_type="text/plain")
    if not ok and reason != "already_paid":
        logger.warning(
            "支付通知落库失败 channel=%s order_id=%s reason=%s",
            channel,
            result.out_trade_no,
            reason,
        )
        return HttpResponse("failure", status=400, content_type="text/plain")
    return HttpResponse(provider.notify_ack, content_type="text/plain")


@login_required
def pay_return(request, channel):
    """同步返回页：验签/主动查询后展示结果，不能直接相信 URL 参数。"""
    order_id = request.GET.get("out_trade_no", "")
    if not order_id:
        messages.error(request, "支付返回缺少订单号")
        return redirect("order:list")
    order = get_object_or_404(
        OrderInfo.objects.for_user(request.user), order_id=order_id
    )

    try:
        provider = payment.get_provider(channel)
        if hasattr(provider, "verify_return"):
            result = provider.verify_return(request)
            if result.paid:
                payment.apply_result(result, expected_channel=provider.name)
    except payment.PaymentError as exc:
        logger.warning("支付同步返回校验失败 channel=%s order_id=%s: %s", channel, order_id, exc)

    order.refresh_from_db()
    if order.pay_status != OrderInfo.STATUS_PAID:
        order = payment.refresh_order_status(order, force=True)
    if order.pay_status == OrderInfo.STATUS_PAID:
        messages.success(request, "支付成功")
        return redirect("order:list")
    if order.is_cancelled:
        messages.warning(request, "订单已取消，支付结果未生效")
        return redirect("order:list")

    return render(
        request,
        "order/pay_wait.html",
        {
            "order": order,
            "poll_url": reverse("order:pay_status", kwargs={"order_id": order.order_id}),
            "poll_interval": max(1, settings.PAYMENT_STATUS_POLL_INTERVAL),
        },
    )


@login_required
def mock_cashier(request, order_id):
    """本地模拟收银台，仅在开发环境（DEBUG）且 PAYMENT_PROVIDER=mock 时可用。"""
    if not payment.mock_payment_enabled():
        raise Http404
    order = get_object_or_404(
        OrderInfo.objects.for_user(request.user), order_id=order_id
    )
    if order.pay_status == OrderInfo.STATUS_PAID:
        messages.info(request, "订单已支付")
        return redirect("order:list")
    if order.is_cancelled:
        messages.warning(request, "订单已取消，不能继续支付")
        return redirect("order:list")
    return render(request, "order/mock_pay.html", {"order": order})


@login_required
@require_POST
def mock_pay_success(request, order_id):
    """模拟支付成功：调用与真实通知相同的 mark_paid 落库逻辑。"""
    if not payment.mock_payment_enabled():
        raise Http404
    order = get_object_or_404(
        OrderInfo.objects.for_user(request.user), order_id=order_id
    )
    if order.pay_status == OrderInfo.STATUS_PAID:
        messages.info(request, "订单已支付")
        return redirect("order:list")
    if order.is_cancelled:
        messages.warning(request, "订单已取消，不能继续支付")
        return redirect("order:list")

    trade_no = f"MOCK{timezone.now():%Y%m%d%H%M%S}{secrets.token_hex(4)}"
    ok, reason = payment.mark_paid(order.order_id, trade_no, order.total_pay)
    if ok:
        messages.success(request, f"订单 {order.order_id} 模拟支付成功")
    elif reason == "order_cancelled":
        messages.warning(request, "订单已取消，不能继续支付")
    else:
        messages.error(request, f"模拟支付失败：{reason}")
    return redirect("order:list")
