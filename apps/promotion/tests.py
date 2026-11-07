from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from goods.models import GoodsSKU, GoodsSPU, GoodsType, IndexPromotionBanner
from order.models import OrderInfo
from user.models import Address, User

from .models import Promotion, PromotionScope, UserCoupon
from .services import (
    PricingError,
    bind_referral,
    build_quote,
    ensure_new_customer_coupon,
    grant_coupon,
    is_new_customer,
    price_skus,
    referral_available,
)


class PromotionPricingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="buyer", password="pass1234", email="buyer@example.com", is_active=True
        )
        self.goods_type = GoodsType.objects.create(name="测试分类", logo="test")
        self.spu = GoodsSPU.objects.create(name="测试商品")

    def make_sku(self, name, price, category=None):
        return GoodsSKU.objects.create(
            type=category or self.goods_type,
            spu=self.spu,
            name=name,
            price=Decimal(price),
            stock=100,
            sales=0,
            status=GoodsSKU.STATUS_ONLINE,
        )

    def make_promotion(self, **overrides):
        defaults = {
            "slug": f"promo-{Promotion.objects.count() + 1}",
            "name": "测试活动",
            "kind": Promotion.KIND_ITEM_DISCOUNT,
            "is_active": True,
            "discount_rate": Decimal("0.5000"),
            "stackable": False,
        }
        defaults.update(overrides)
        return Promotion.objects.create(**defaults)

    def test_free_shipping_boundary_uses_activity_price_subtotal(self):
        self.make_promotion(
            slug="free-shipping",
            name="满 99 免运费",
            kind=Promotion.KIND_FREE_SHIPPING,
            shipping_threshold=Decimal("99.00"),
            discount_amount=Decimal("10.00"),
            stackable=True,
        )
        below = self.make_sku("98.99 商品", "98.99")
        exact = self.make_sku("99.00 商品", "99.00")

        below_quote = build_quote(self.user, [{"sku": below, "count": 1}])
        self.assertEqual(below_quote.shipping_fee, Decimal("10.00"))
        self.assertEqual(below_quote.shipping_discount, Decimal("0.00"))
        self.assertEqual(below_quote.total_pay, Decimal("108.99"))

        exact_quote = build_quote(self.user, [{"sku": exact, "count": 1}])
        self.assertEqual(exact_quote.shipping_fee, Decimal("0.00"))
        self.assertEqual(exact_quote.shipping_discount, Decimal("10.00"))
        self.assertEqual(exact_quote.original_total_amount, Decimal("109.00"))
        self.assertEqual(exact_quote.total_pay, Decimal("99.00"))

    def test_coupon_does_not_lower_free_shipping_threshold(self):
        self.make_promotion(
            slug="free-shipping",
            name="满 99 免运费",
            kind=Promotion.KIND_FREE_SHIPPING,
            shipping_threshold=Decimal("99.00"),
            discount_amount=Decimal("10.00"),
            stackable=True,
        )
        newcomer = self.make_promotion(
            slug="new-customer",
            name="新人券",
            kind=Promotion.KIND_NEW_CUSTOMER,
            discount_amount=Decimal("20.00"),
        )
        coupon, created = grant_coupon(self.user, newcomer, "new-customer:test")
        self.assertTrue(created)
        sku = self.make_sku("99 元商品", "99.00")

        quote = build_quote(self.user, [{"sku": sku, "count": 1}], coupon_id=coupon.pk)
        self.assertEqual(quote.item_discount, Decimal("0.00"))
        self.assertEqual(quote.coupon_discount, Decimal("20.00"))
        self.assertEqual(quote.shipping_fee, Decimal("0.00"))
        self.assertEqual(quote.total_pay, Decimal("79.00"))

    def test_weekend_window_boundaries(self):
        base = timezone.now().replace(microsecond=0)
        promotion = self.make_promotion(
            slug="weekend",
            name="周末五折",
            start_at=base,
            end_at=base + timedelta(hours=2),
        )
        sku = self.make_sku("周末商品", "20.00")
        PromotionScope.objects.create(promotion=promotion, category=self.goods_type)

        before = build_quote(
            self.user,
            [{"sku": sku, "count": 1}],
            at=base - timedelta(seconds=1),
        )
        self.assertEqual(before.lines[0].unit_price, Decimal("20.00"))
        self.assertEqual(before.item_discount, Decimal("0.00"))

        start = build_quote(self.user, [{"sku": sku, "count": 1}], at=base)
        self.assertEqual(start.lines[0].unit_price, Decimal("10.00"))
        self.assertEqual(start.item_discount, Decimal("10.00"))

        end = build_quote(
            self.user,
            [{"sku": sku, "count": 1}],
            at=base + timedelta(hours=2),
        )
        self.assertEqual(end.lines[0].unit_price, Decimal("10.00"))

        after = build_quote(
            self.user,
            [{"sku": sku, "count": 1}],
            at=base + timedelta(hours=2, seconds=1),
        )
        self.assertEqual(after.lines[0].unit_price, Decimal("20.00"))
        self.assertEqual(after.item_discount, Decimal("0.00"))

    def test_scope_excludes_sku(self):
        promotion = self.make_promotion(slug="scope-test", name="范围测试")
        included = self.make_sku("参与商品", "20.00")
        excluded = self.make_sku("排除商品", "20.00")
        PromotionScope.objects.create(promotion=promotion, sku=excluded, include=False)

        prices = price_skus([included, excluded])
        self.assertTrue(prices[included.pk].has_discount)
        self.assertFalse(prices[excluded.pk].has_discount)

    def test_price_skus_uses_constant_number_of_queries(self):
        promotion = self.make_promotion(slug="batch-price", name="批量价格")
        skus = [self.make_sku(f"批量商品{i}", "20.00") for i in range(5)]
        PromotionScope.objects.create(promotion=promotion, category=self.goods_type)
        with self.assertNumQueries(2):
            prices = price_skus(skus)
        self.assertEqual(set(prices), {sku.pk for sku in skus})

    def test_new_customer_coupon_cannot_stack_with_item_discount(self):
        now = timezone.now()
        item_promo = self.make_promotion(
            slug="weekend-stack",
            name="周末五折",
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(hours=1),
        )
        newcomer = self.make_promotion(
            slug="new-stack",
            name="新人券",
            kind=Promotion.KIND_NEW_CUSTOMER,
            discount_amount=Decimal("20.00"),
        )
        sku = self.make_sku("叠加测试商品", "50.00")
        PromotionScope.objects.create(promotion=item_promo, category=self.goods_type)
        coupon, _ = grant_coupon(self.user, newcomer, "new-customer:stack")

        with self.assertRaises(PricingError):
            build_quote(self.user, [{"sku": sku, "count": 1}], coupon_id=coupon.pk)

    def test_used_or_expired_coupon_is_rejected(self):
        newcomer = self.make_promotion(
            slug="new-expired",
            name="新人券",
            kind=Promotion.KIND_NEW_CUSTOMER,
            discount_amount=Decimal("20.00"),
        )
        sku = self.make_sku("券测试商品", "50.00")
        coupon, _ = grant_coupon(self.user, newcomer, "new-customer:expired")
        coupon.expires_at = timezone.now() - timedelta(seconds=1)
        coupon.save(update_fields=["expires_at", "update_time"])
        with self.assertRaises(PricingError):
            build_quote(self.user, [{"sku": sku, "count": 1}], coupon_id=coupon.pk)

    def test_coupon_cannot_be_used_by_another_user(self):
        newcomer = self.make_promotion(
            slug="new-other-user",
            name="新人券",
            kind=Promotion.KIND_NEW_CUSTOMER,
            discount_amount=Decimal("20.00"),
        )
        other = User.objects.create_user(
            username="coupon-owner",
            password="pass1234",
            email="coupon-owner@example.com",
            is_active=True,
        )
        coupon, _ = grant_coupon(other, newcomer, "new-customer:other-user")
        sku = self.make_sku("越权测试商品", "50.00")
        with self.assertRaises(PricingError):
            build_quote(self.user, [{"sku": sku, "count": 1}], coupon_id=coupon.pk)

    def test_user_with_paid_order_is_not_new_customer(self):
        newcomer = self.make_promotion(
            slug="new-paid",
            name="新人券",
            kind=Promotion.KIND_NEW_CUSTOMER,
            discount_amount=Decimal("20.00"),
        )
        coupon, _ = grant_coupon(self.user, newcomer, "new-customer:paid")
        address = Address.objects.create(
            user=self.user, receiver="测试", addr="测试地址", phone="13800000000"
        )
        OrderInfo.objects.create(
            order_id="PAID-OLD-ORDER",
            user=self.user,
            addr=address,
            total_amount=Decimal("10.00"),
            transit_price=Decimal("0.00"),
            original_total_amount=Decimal("10.00"),
            item_discount=Decimal("0.00"),
            coupon_discount=Decimal("0.00"),
            shipping_discount=Decimal("0.00"),
            total_discount=Decimal("0.00"),
            pay_status=OrderInfo.STATUS_PAID,
        )
        sku = self.make_sku("已购用户商品", "50.00")
        self.assertFalse(is_new_customer(self.user))
        with self.assertRaises(PricingError):
            build_quote(self.user, [{"sku": sku, "count": 1}], coupon_id=coupon.pk)

    def test_new_customer_coupon_issue_is_idempotent(self):
        self.make_promotion(
            slug="new-idempotent",
            name="新人券",
            kind=Promotion.KIND_NEW_CUSTOMER,
            discount_amount=Decimal("20.00"),
        )
        first = ensure_new_customer_coupon(self.user)
        second = ensure_new_customer_coupon(self.user)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            UserCoupon.objects.filter(user=self.user, grant_key=f"new-customer:{self.user.pk}").count(),
            1,
        )

    @override_settings(PROMOTION_REFERRAL_ENABLED=True)
    def test_referral_binding_is_one_to_one(self):
        inviter = User.objects.create_user(
            username="inviter", password="pass1234", email="inviter@example.com", is_active=True
        )
        invitee = User.objects.create_user(
            username="invitee", password="pass1234", email="invitee@example.com", is_active=True
        )
        other = User.objects.create_user(
            username="other", password="pass1234", email="other@example.com", is_active=True
        )
        self.make_promotion(
            slug="referral-bind",
            name="邀请奖励",
            kind=Promotion.KIND_REFERRAL_REWARD,
            discount_amount=Decimal("10.00"),
        )
        first = bind_referral(inviter, invitee, "code-1")
        second = bind_referral(other, invitee, "code-2")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.inviter_id, inviter.pk)

    @override_settings(PROMOTION_REFERRAL_ENABLED=True)
    def test_referral_requires_an_active_campaign(self):
        inviter = User.objects.create_user(
            username="inviter-no-campaign",
            password="pass1234",
            email="inviter-no-campaign@example.com",
            is_active=True,
        )
        invitee = User.objects.create_user(
            username="invitee-no-campaign",
            password="pass1234",
            email="invitee-no-campaign@example.com",
            is_active=True,
        )
        self.assertFalse(referral_available())
        self.assertIsNone(bind_referral(inviter, invitee, "old-link"))


class PromotionInitializationTests(TestCase):
    def test_invite_campaign_is_removed_by_data_migration(self):
        self.assertFalse(IndexPromotionBanner.objects.filter(slug="invite-friends").exists())

    @override_settings(PROMOTION_REFERRAL_ENABLED=True)
    def test_user_center_hides_referral_without_active_campaign(self):
        user = User.objects.create_user(
            username="center-user",
            password="pass1234",
            email="center@example.com",
            is_active=True,
        )
        self.client.force_login(user)
        response = self.client.get(reverse("user:center"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "邀请好友")
        self.assertFalse(Promotion.objects.filter(slug="invite-reward").exists())

    def test_init_promotions_creates_only_current_campaigns(self):
        call_command("init_promotions", stdout=StringIO())
        self.assertEqual(
            set(Promotion.objects.values_list("slug", flat=True)),
            {"new-customer", "free-shipping", "weekend-sale"},
        )
        self.assertFalse(IndexPromotionBanner.objects.filter(slug="invite-friends").exists())
