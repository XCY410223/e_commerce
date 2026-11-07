"""购物车模块视图。

购物车数据落在 Redis Hash：登录用户为 cart:{user_id}，游客为 cart:guest:{token}。
游客 token 只放在 Cookie 里，首次加购时下发，登录后由 user 模块合并进账号购物车。
读写、上架/软删除/库存校验统一封装在 utils/cart.py，供订单模块复用。
"""

from decimal import Decimal

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from redis.exceptions import RedisError

from promotion.services import PricingError, build_quote
from utils.cart import (
    GUEST_CART_COOKIE,
    CartError,
    add_to_cart_ref,
    cart_count_ref,
    delete_cart_item_ref,
    guest_cart_ttl,
    read_cart_ref,
    read_cart_ref_report,
    sku_cart_ref,
    update_cart_item_ref,
)

def _with_guest_cookie(request, response):
    """游客首次加购才下发 token，登录用户不会拿到这个 Cookie。"""
    token = getattr(request, "_guest_cart_new_token", None)
    if token:
        response.set_cookie(
            GUEST_CART_COOKIE,
            token,
            max_age=guest_cart_ttl(),
            httponly=True,
            samesite="Lax",
        )
    return response


def _cart_error_response(exc):
    return JsonResponse(
        {"code": exc.code, "msg": str(exc)},
        status=exc.status_code,
    )


def _expected_count(value):
    """页面回传的“我看到的是几件”，用于并发覆盖校验；不传表示不校验。"""
    if value in (None, ""):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise CartError(
            "购物车数据已过期，请刷新页面后重试",
            code="cart_conflict",
            status_code=409,
        ) from exc
    if count < 0:
        raise CartError(
            "购物车数据已过期，请刷新页面后重试",
            code="cart_conflict",
            status_code=409,
        )
    return count


def _row_payload(items, sku_id):
    """返回某一行更新后的数量和金额，前端据此做局部刷新。"""
    for item in items:
        if item["sku"].id == sku_id:
            return item["count"], str(item["amount"])
    return 0, "0.00"


def _quote_for_request(request, items):
    user = request.user if getattr(request.user, "is_authenticated", False) else None
    return build_quote(user, items, coupon_id=None)


def _attach_quote_lines(items, quote):
    by_sku = {line.sku.pk: line for line in quote.lines}
    for item in items:
        item["pricing"] = by_sku.get(item["sku"].pk)
    return items


def cart_index(request):
    ref = sku_cart_ref(request)
    if ref is None:  # 游客还没有 Cookie，直接按空购物车渲染
        items, total_count, total_amount, removed_ids = [], 0, Decimal("0.00"), []
    else:
        try:
            items, total_count, total_amount, removed_ids = read_cart_ref_report(ref)
        except RedisError:
            # 缓存故障时仍返回页面骨架，避免基础设施问题直接变成 500。
            messages.error(request, "购物车服务暂时不可用，请稍后重试")
            items, total_count, total_amount, removed_ids = [], 0, Decimal("0.00"), []
    try:
        quote = _quote_for_request(request, items)
    except PricingError:
        quote = None
    if quote is not None:
        items = _attach_quote_lines(items, quote)
        display_total = quote.goods_payable
    else:
        display_total = total_amount
    # 母版里的角标直接复用这次结果，避免同一页面再读一遍购物车
    request._cart_summary = (items, total_count, total_amount)
    response = render(
        request,
        "cart/cart.html",
        {
            "items": items,
            "total_count": total_count,
            "total_amount": display_total,
            "quote": quote,
            "removed_count": len(removed_ids),
        },
    )
    return _with_guest_cookie(request, response)


@require_POST
def add(request, sku_id):
    ref = sku_cart_ref(request, create=True)
    try:
        add_to_cart_ref(ref, sku_id, request.POST.get("count", 1))
    except CartError as exc:
        return _cart_error_response(exc)
    except RedisError:
        return _redis_error_response()

    response = JsonResponse(
        {"code": 0, "msg": "已加入购物车", "cart_count": cart_count_ref(ref)}
    )
    return _with_guest_cookie(request, response)


@require_POST
def update(request, sku_id):
    ref = sku_cart_ref(request, create=True)
    try:
        update_cart_item_ref(
            ref,
            sku_id,
            request.POST.get("count", 1),
            _expected_count(request.POST.get("expected")),
        )
    except CartError as exc:
        return _cart_error_response(exc)
    except RedisError:
        return _redis_error_response()

    try:
        items, total_count, total_amount = read_cart_ref(ref)
    except RedisError:
        return _redis_error_response()
    try:
        quote = _quote_for_request(request, items)
    except PricingError:
        return JsonResponse(
            {"code": "pricing_error", "msg": "活动价格异常，请刷新页面后重试"},
            status=409,
        )
    count, amount = _row_payload(items, sku_id)
    line = next((line for line in quote.lines if line.sku.pk == sku_id), None)
    response = JsonResponse(
        {
            "code": 0,
            "sku_id": sku_id,
            "count": count,
            "amount": str(line.amount) if line else amount,
            "total_count": total_count,
            "total_amount": str(quote.goods_payable),
            "item_discount": str(quote.item_discount),
            "shipping_fee": str(quote.shipping_fee),
            "total_pay": str(quote.total_pay),
            "free_shipping_gap": str(quote.free_shipping_gap),
            "cart_count": total_count,
        }
    )
    return _with_guest_cookie(request, response)


@require_POST
def delete(request, sku_id):
    ref = sku_cart_ref(request, create=True)
    try:
        delete_cart_item_ref(
            ref,
            sku_id,
            expected=_expected_count(request.POST.get("expected")),
        )
    except CartError as exc:
        return _cart_error_response(exc)
    except RedisError:
        return _redis_error_response()
    try:
        items, total_count, total_amount = read_cart_ref(ref)
    except RedisError:
        return _redis_error_response()
    try:
        quote = _quote_for_request(request, items)
        total_payable = quote.goods_payable
        total_pay = quote.total_pay
        shipping_fee = quote.shipping_fee
    except PricingError:
        total_payable = total_amount
        total_pay = total_amount
        shipping_fee = Decimal("0.00")
    response = JsonResponse(
        {
            "code": 0,
            "msg": "已移除",
            "sku_id": sku_id,
            "count": 0,
            "amount": "0.00",
            "total_count": total_count,
            "total_amount": str(total_payable),
            "total_pay": str(total_pay),
            "shipping_fee": str(shipping_fee),
            "cart_count": total_count,
        }
    )
    return _with_guest_cookie(request, response)


def _redis_error_response():
    return JsonResponse(
        {"code": "service_unavailable", "msg": "购物车服务暂时不可用，请稍后重试"},
        status=503,
    )
