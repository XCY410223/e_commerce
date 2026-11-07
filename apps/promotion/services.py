"""促销算价与权益发放的唯一服务入口。

购物车、确认订单、提交订单和支付校验都必须通过 build_quote 获取金额。
前端传入的金额只用于一致性校验，不能作为优惠依据。
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from goods.models import GoodsSKU

from .models import OrderPromotion, Promotion, PromotionScope, Referral, UserCoupon

logger = logging.getLogger(__name__)

MONEY = Decimal("0.01")
ZERO = Decimal("0.00")
REFERRAL_SALT = "ecommerce.promotion.referral"


class PricingError(ValueError):
    """可以直接展示给用户的算价失败。"""


@dataclass
class SkuPrice:
    sku_id: int
    original_unit_price: Decimal
    unit_price: Decimal
    discount_amount: Decimal
    promotion: Promotion | None = None
    promotion_name: str = ""

    @property
    def has_discount(self):
        return self.discount_amount > ZERO and bool(self.promotion_name)


@dataclass
class QuoteLine:
    sku: object
    count: int
    original_unit_price: Decimal
    unit_price: Decimal
    original_amount: Decimal
    amount: Decimal
    discount_amount: Decimal
    promotion: Promotion | None = None
    promotion_name: str = ""

    @property
    def has_discount(self):
        return self.discount_amount > ZERO


@dataclass
class AppliedPromotion:
    promotion: Promotion
    kind: str
    snapshot_name: str
    discount_amount: Decimal
    metadata: dict = field(default_factory=dict)


@dataclass
class Quote:
    lines: list[QuoteLine]
    total_count: int
    goods_original_total: Decimal
    goods_payable: Decimal
    item_discount: Decimal
    coupon_discount: Decimal
    shipping_fee: Decimal
    original_shipping_fee: Decimal
    shipping_discount: Decimal
    total_discount: Decimal
    original_total_amount: Decimal
    total_pay: Decimal
    coupon: UserCoupon | None = None
    shipping_threshold: Decimal | None = None
    free_shipping_gap: Decimal = ZERO
    applied_promotions: list[AppliedPromotion] = field(default_factory=list)


def money(value):
    """金额统一四舍五入到分，避免浮点和二进制误差进入订单。"""
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def standard_shipping_fee():
    return money(getattr(settings, "STANDARD_TRANSIT_PRICE", Decimal("10.00")))


def _pricing_enabled():
    return bool(getattr(settings, "PROMOTION_PRICING_ENABLED", True))


def _coupon_enabled():
    return bool(getattr(settings, "PROMOTION_COUPON_ENABLED", True))


def _referral_enabled():
    return bool(getattr(settings, "PROMOTION_REFERRAL_ENABLED", False))


def referral_available(at=None):
    """邀请活动是否实际可参与：全局开关打开且存在启用中的奖励规则。"""
    if not _referral_enabled():
        return False
    at = at or timezone.now()
    return Promotion.objects.effective(at).filter(kind=Promotion.KIND_REFERRAL_REWARD).exists()


def _load_scopes(promotions):
    promotion_ids = [promotion.pk for promotion in promotions]
    if not promotion_ids:
        return {}
    scopes = (
        PromotionScope.objects.filter(promotion_id__in=promotion_ids)
        .only("promotion_id", "category_id", "sku_id", "include")
        .order_by("id")
    )
    result = {}
    for scope in scopes:
        result.setdefault(scope.promotion_id, []).append(scope)
    return result


def _scope_matches(scope, sku):
    return (
        (scope.sku_id is not None and scope.sku_id == sku.pk)
        or (scope.category_id is not None and scope.category_id == sku.type_id)
    )


def _promotion_matches_sku(promotion, scopes, sku):
    """包含/排除范围判断；没有包含项时视为全店参加。"""
    include_scopes = [scope for scope in scopes if scope.include]
    exclude_scopes = [scope for scope in scopes if not scope.include]
    if any(_scope_matches(scope, sku) for scope in exclude_scopes):
        return False
    if include_scopes:
        return any(_scope_matches(scope, sku) for scope in include_scopes)
    return True


def _effective_item_promotions(at):
    promotions = list(
        Promotion.objects.effective(at)
        .filter(kind=Promotion.KIND_ITEM_DISCOUNT)
        .order_by("priority", "id")
    )
    return promotions, _load_scopes(promotions)


def _choose_item_promotion(sku, promotions, scope_map):
    for promotion in promotions:
        if _promotion_matches_sku(promotion, scope_map.get(promotion.pk, []), sku):
            return promotion
    return None


def _discounted_price(sku, count, promotion):
    original_unit = money(sku.price)
    original_amount = money(original_unit * count)
    if promotion is None:
        return SkuPrice(sku.pk, original_unit, original_unit, ZERO)

    unit_price = money(original_unit * promotion.discount_rate)
    line_amount = money(unit_price * count)
    discount = money(original_amount - line_amount)
    if promotion.max_discount is not None and discount > promotion.max_discount:
        discount = money(promotion.max_discount)
        line_amount = money(original_amount - discount)
        unit_price = money(line_amount / count)
        line_amount = money(unit_price * count)
        discount = money(original_amount - line_amount)
    return SkuPrice(
        sku.pk,
        original_unit,
        unit_price,
        max(ZERO, discount),
        promotion,
        promotion.name,
    )


def price_skus(skus, at=None):
    """批量生成商品展示价，活动查询固定为两条 SQL，不随商品数量增加。"""
    at = at or timezone.now()
    skus = list(skus)
    promotions, scope_map = _effective_item_promotions(at) if _pricing_enabled() else ([], {})
    result = {}
    for sku in skus:
        if not _pricing_enabled():
            result[sku.pk] = SkuPrice(sku.pk, money(sku.price), money(sku.price), ZERO)
            continue
        promotion = _choose_item_promotion(sku, promotions, scope_map)
        result[sku.pk] = _discounted_price(sku, 1, promotion)
    return result


def decorate_skus_with_prices(skus, at=None):
    """给模板对象附加展示字段，缺失字段时模板自动回退到 sku.price。"""
    skus = list(skus)
    prices = price_skus(skus, at=at)
    for sku in skus:
        price = prices.get(sku.pk)
        sku.original_price = sku.price
        sku.effective_price = price.unit_price if price else money(sku.price)
        sku.has_promotion_price = bool(price and price.has_discount)
        sku.promotion_name = price.promotion_name if price else ""
    return skus


def _has_paid_order(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    from order.models import OrderInfo

    return OrderInfo.objects.filter(
        user=user, pay_status=OrderInfo.STATUS_PAID
    ).exists()


def is_new_customer(user):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and not _has_paid_order(user)
    )


def _load_selected_coupon(user, coupon_id, at, lock):
    if coupon_id in (None, "", 0, "0"):
        return None
    if not user or not getattr(user, "is_authenticated", False):
        raise PricingError("登录后才能使用优惠券")
    if not _coupon_enabled():
        raise PricingError("优惠券功能暂停使用，请刷新页面后重试")
    try:
        coupon_id = int(coupon_id)
    except (TypeError, ValueError) as exc:
        raise PricingError("优惠券参数无效") from exc

    queryset = UserCoupon.objects.select_related("promotion")
    if lock:
        queryset = queryset.select_for_update()
    coupon = queryset.filter(pk=coupon_id, user=user).first()
    if coupon is None:
        raise PricingError("优惠券不存在或不属于当前账号")
    if coupon.status != UserCoupon.STATUS_AVAILABLE or coupon.used_order_id:
        raise PricingError("优惠券已使用，请勿重复提交")
    if coupon.expires_at <= at:
        raise PricingError("优惠券已过期")
    if not coupon.promotion.is_effective(at):
        raise PricingError("优惠券对应活动已结束")
    if coupon.promotion.kind == Promotion.KIND_NEW_CUSTOMER and not is_new_customer(user):
        raise PricingError("新人券仅限无已支付订单的新用户使用")
    return coupon


def _shipping_rule(goods_payable, at):
    if not _pricing_enabled():
        return None, None
    promotions = list(
        Promotion.objects.effective(at)
        .filter(kind=Promotion.KIND_FREE_SHIPPING)
        .order_by("priority", "id")
    )
    for promotion in promotions:
        threshold = promotion.shipping_threshold
        if threshold is None:
            threshold = promotion.min_amount
        threshold = money(threshold or 0)
        if goods_payable >= threshold:
            return promotion, threshold
    if promotions:
        first = promotions[0]
        threshold = first.shipping_threshold
        if threshold is None:
            threshold = first.min_amount
        return None, money(threshold or 0)
    return None, None


def build_quote(user, items, coupon_id=None, at=None, lock_coupon=False):
    """生成完整报价。

    items 必须是服务端购物车或已加锁 SKU 生成的 [{"sku": sku, "count": n}]。
    同一时刻只命中一个商品级活动；商品活动与券是否叠加由活动规则决定。
    """
    at = at or timezone.now()
    promotions, scope_map = _effective_item_promotions(at) if _pricing_enabled() else ([], {})
    lines = []
    total_count = 0
    goods_original_total = ZERO
    item_discount = ZERO
    item_applied = {}

    for item in items:
        sku = item["sku"]
        try:
            count = int(item["count"])
        except (TypeError, ValueError) as exc:
            raise PricingError("商品数量无效") from exc
        if count <= 0:
            raise PricingError("商品数量必须大于 0")
        if getattr(sku, "status", GoodsSKU.STATUS_ONLINE) != GoodsSKU.STATUS_ONLINE:
            raise PricingError(f"「{sku.name}」已下架")

        promotion = _choose_item_promotion(sku, promotions, scope_map) if _pricing_enabled() else None
        price = _discounted_price(sku, count, promotion)
        line = QuoteLine(
            sku=sku,
            count=count,
            original_unit_price=price.original_unit_price,
            unit_price=price.unit_price,
            original_amount=money(price.original_unit_price * count),
            amount=money(price.unit_price * count),
            discount_amount=money(
                price.original_unit_price * count - price.unit_price * count
            ),
            promotion=promotion,
            promotion_name=promotion.name if promotion else "",
        )
        lines.append(line)
        total_count += count
        goods_original_total += line.original_amount
        item_discount += line.discount_amount
        if promotion and line.discount_amount > ZERO:
            bucket = item_applied.setdefault(
                promotion.pk,
                {"promotion": promotion, "discount": ZERO, "skus": []},
            )
            bucket["discount"] += line.discount_amount
            bucket["skus"].append({"sku_id": sku.pk, "count": count})

    goods_original_total = money(goods_original_total)
    item_discount = money(item_discount)
    goods_payable = money(max(ZERO, goods_original_total - item_discount))
    original_shipping = standard_shipping_fee()

    coupon = _load_selected_coupon(user, coupon_id, at, lock_coupon)
    coupon_discount = ZERO
    applied_item_promotions = [bucket["promotion"] for bucket in item_applied.values()]
    if coupon is not None:
        if item_applied and (
            coupon.promotion.kind == Promotion.KIND_NEW_CUSTOMER
            or any(not promotion.stackable for promotion in applied_item_promotions)
        ):
            raise PricingError("当前商品活动不可与所选优惠券叠加，请取消用券后重试")
        if goods_payable < money(coupon.min_amount):
            raise PricingError(f"该券需满 ¥{money(coupon.min_amount)} 可用")
        coupon_discount = money(min(coupon.discount_amount, goods_payable))

    shipping_promotion, shipping_threshold = _shipping_rule(goods_payable, at)
    shipping_fee = original_shipping
    shipping_discount = ZERO
    if shipping_promotion is not None:
        shipping_fee = ZERO
        shipping_discount = original_shipping
    free_shipping_gap = ZERO
    if shipping_threshold is not None and shipping_fee > ZERO:
        free_shipping_gap = money(max(ZERO, shipping_threshold - goods_payable))

    total_discount = money(item_discount + coupon_discount + shipping_discount)
    original_total_amount = money(goods_original_total + original_shipping)
    total_pay = money(max(ZERO, original_total_amount - total_discount))

    applied = []
    for bucket in item_applied.values():
        promotion = bucket["promotion"]
        applied.append(
            AppliedPromotion(
                promotion=promotion,
                kind=promotion.kind,
                snapshot_name=promotion.name,
                discount_amount=money(bucket["discount"]),
                metadata={"skus": bucket["skus"]},
            )
        )
    if shipping_promotion is not None:
        applied.append(
            AppliedPromotion(
                promotion=shipping_promotion,
                kind=shipping_promotion.kind,
                snapshot_name=shipping_promotion.name,
                discount_amount=shipping_discount,
                metadata={
                    "threshold": str(shipping_threshold or ZERO),
                    "goods_payable": str(goods_payable),
                },
            )
        )
    if coupon is not None and coupon_discount > ZERO:
        applied.append(
            AppliedPromotion(
                promotion=coupon.promotion,
                kind=coupon.promotion.kind,
                snapshot_name=coupon.title,
                discount_amount=coupon_discount,
                metadata={"coupon_id": coupon.pk, "code": coupon.code},
            )
        )

    return Quote(
        lines=lines,
        total_count=total_count,
        goods_original_total=goods_original_total,
        goods_payable=goods_payable,
        item_discount=item_discount,
        coupon_discount=coupon_discount,
        shipping_fee=shipping_fee,
        original_shipping_fee=original_shipping,
        shipping_discount=shipping_discount,
        total_discount=total_discount,
        original_total_amount=original_total_amount,
        total_pay=total_pay,
        coupon=coupon,
        shipping_threshold=shipping_threshold,
        free_shipping_gap=free_shipping_gap,
        applied_promotions=applied,
    )


def grant_coupon(user, promotion, grant_key, title=None):
    """按 grant_key 幂等发券；重复调用返回已有券，不重复发放。"""
    if not user or not user.is_active or not promotion.is_coupon:
        return None, False
    now = timezone.now()
    expires_at = now + timedelta(days=max(1, promotion.coupon_valid_days))
    try:
        # 用保存点包住唯一键冲突，避免 PostgreSQL 等数据库把外层事务标记为
        # broken，导致支付回调后续的业务写入全部失败。
        with transaction.atomic():
            coupon, created = UserCoupon.objects.get_or_create(
                grant_key=grant_key,
                defaults={
                    "user": user,
                    "promotion": promotion,
                    "title": title or promotion.name,
                    "discount_amount": money(promotion.discount_amount),
                    "min_amount": money(promotion.min_amount),
                    "expires_at": expires_at,
                },
            )
    except IntegrityError:
        coupon = UserCoupon.objects.filter(grant_key=grant_key).first()
        created = False
    if created:
        logger.info(
            "发放优惠券 user_id=%s promotion=%s coupon_id=%s grant_key=%s",
            user.pk,
            promotion.slug,
            coupon.pk,
            grant_key,
        )
    return coupon, created


def ensure_new_customer_coupon(user, at=None):
    """为符合条件的新用户补发一次新人券，可用于激活和进入结算页时兜底。"""
    if not _coupon_enabled() or not is_new_customer(user):
        return None
    at = at or timezone.now()
    promotion = (
        Promotion.objects.effective(at)
        .filter(kind=Promotion.KIND_NEW_CUSTOMER)
        .order_by("priority", "id")
        .first()
    )
    if promotion is None:
        return None
    coupon, _ = grant_coupon(
        user,
        promotion,
        grant_key=f"new-customer:{user.pk}",
        title=promotion.name,
    )
    return coupon


def available_coupons(user, at=None):
    """返回当前可展示的券；是否满足商品活动叠加条件在 build_quote 中最终判断。"""
    if not user or not getattr(user, "is_authenticated", False) or not _coupon_enabled():
        return []
    at = at or timezone.now()
    coupons = list(
        UserCoupon.objects.filter(
            user=user,
            status=UserCoupon.STATUS_AVAILABLE,
            expires_at__gt=at,
            used_order__isnull=True,
        )
        .select_related("promotion")
        .order_by("expires_at", "id")
    )
    result = []
    new_customer_ok = is_new_customer(user)
    for coupon in coupons:
        promotion = coupon.promotion
        if not promotion.is_effective(at):
            continue
        if promotion.kind == Promotion.KIND_NEW_CUSTOMER and not new_customer_ok:
            continue
        result.append(coupon)
    return result


def consume_coupon(coupon, order):
    """在同一订单事务内核销优惠券；调用方负责事务和行锁。"""
    if coupon is None:
        return
    locked = UserCoupon.objects.select_for_update().filter(pk=coupon.pk).first()
    if locked is None or locked.status != UserCoupon.STATUS_AVAILABLE:
        raise PricingError("优惠券已被使用，请重新结算")
    if locked.used_order_id and locked.used_order_id != order.pk:
        raise PricingError("优惠券已被其他订单使用")
    locked.status = UserCoupon.STATUS_USED
    locked.used_order = order
    locked.used_at = timezone.now()
    locked.save(update_fields=["status", "used_order", "used_at", "update_time"])


def release_coupon_for_order(order, at=None):
    """整单取消时退回未过期优惠券；部分退款不在本期范围内。"""
    at = at or timezone.now()
    coupons = list(
        UserCoupon.objects.select_for_update()
        .filter(used_order=order, status=UserCoupon.STATUS_USED)
    )
    for coupon in coupons:
        coupon.used_order = None
        coupon.used_at = None
        coupon.status = (
            UserCoupon.STATUS_AVAILABLE
            if coupon.expires_at > at
            else UserCoupon.STATUS_EXPIRED
        )
        coupon.save(update_fields=["status", "used_order", "used_at", "update_time"])
        logger.info("取消订单退回优惠券 order_id=%s coupon_id=%s", order.order_id, coupon.pk)


def record_order_promotions(order, quote):
    snapshots = []
    for applied in quote.applied_promotions:
        if applied.discount_amount <= ZERO:
            continue
        snapshots.append(
            OrderPromotion(
                order=order,
                promotion=applied.promotion,
                kind=applied.kind,
                snapshot_name=applied.snapshot_name,
                discount_amount=money(applied.discount_amount),
                metadata=applied.metadata,
            )
        )
    if snapshots:
        OrderPromotion.objects.bulk_create(snapshots)
    return snapshots


def make_referral_code(user):
    return signing.dumps({"uid": user.pk}, salt=REFERRAL_SALT, compress=True)


def load_referral_inviter(code):
    """解析签名邀请码，任何篡改或过期都返回 None，不阻断注册。"""
    if not code or not referral_available():
        return None
    try:
        payload = signing.loads(
            code,
            salt=REFERRAL_SALT,
            max_age=int(getattr(settings, "REFERRAL_CODE_MAX_AGE", 180 * 86400)),
        )
        from user.models import User

        return User.objects.filter(pk=int(payload["uid"]), is_active=True).first()
    except Exception:
        logger.info("无效邀请码，已忽略")
        return None


def bind_referral(inviter, invitee, code):
    """创建待首单邀请关系；同一被邀请人只绑定一次。"""
    if not referral_available() or inviter is None or invitee is None:
        return None
    if inviter.pk == invitee.pk or not inviter.is_active:
        return None
    try:
        referral, created = Referral.objects.get_or_create(
            invitee=invitee,
            defaults={"inviter": inviter, "code": code[:255]},
        )
    except IntegrityError:
        return None
    if created:
        logger.info("绑定邀请关系 inviter=%s invitee=%s", inviter.pk, invitee.pk)
    return referral


def referral_summary(user):
    if (
        not user
        or not getattr(user, "is_authenticated", False)
        or not referral_available()
    ):
        return {"pending": 0, "rewarded": 0, "total": 0}
    queryset = Referral.objects.filter(inviter=user)
    return {
        "pending": queryset.filter(status=Referral.STATUS_PENDING).count(),
        "rewarded": queryset.filter(status=Referral.STATUS_REWARDED).count(),
        "total": queryset.count(),
    }


def grant_referral_reward(order):
    """被邀请人首单支付后给邀请人发券；重复回调通过行锁与幂等键保证只发一次。"""
    if not referral_available():
        return None
    from order.models import OrderInfo

    if order.pay_status != OrderInfo.STATUS_PAID:
        return None
    with transaction.atomic():
        referral = (
            Referral.objects.select_for_update()
            .select_related("inviter", "invitee")
            .filter(invitee=order.user)
            .first()
        )
        if referral is None or referral.status != Referral.STATUS_PENDING:
            return referral.reward_coupon if referral else None
        paid_count = OrderInfo.objects.filter(
            user=order.user, pay_status=OrderInfo.STATUS_PAID
        ).count()
        if paid_count != 1:
            referral.status = Referral.STATUS_REJECTED
            referral.reject_reason = "被邀请人不是首单支付"
            referral.save(update_fields=["status", "reject_reason", "update_time"])
            logger.warning("邀请奖励未发放：非首单 invitee=%s", order.user_id)
            return None

        now = timezone.now()
        promotion = (
            Promotion.objects.effective(now)
            .filter(kind=Promotion.KIND_REFERRAL_REWARD)
            .order_by("priority", "id")
            .first()
        )
        if promotion is None:
            logger.error("邀请奖励缺少启用中的 referral_reward 活动 referral_id=%s", referral.pk)
            return None
        coupon, _ = grant_coupon(
            referral.inviter,
            promotion,
            grant_key=f"referral:{referral.pk}",
            title=promotion.name,
        )
        if coupon is None:
            return None
        referral.status = Referral.STATUS_REWARDED
        referral.reward_coupon = coupon
        referral.completed_at = now
        referral.save(
            update_fields=["status", "reward_coupon", "completed_at", "update_time"]
        )
        logger.info(
            "邀请奖励已发放 referral_id=%s inviter=%s coupon_id=%s order_id=%s",
            referral.pk,
            referral.inviter_id,
            coupon.pk,
            order.order_id,
        )
        return coupon


def on_order_paid(order):
    """支付成功后的最佳努力钩子；营销权益失败不能回滚已成功支付。"""
    try:
        with transaction.atomic():
            grant_referral_reward(order)
    except Exception:
        logger.exception("订单支付后发放邀请奖励失败 order_id=%s", order.order_id)
