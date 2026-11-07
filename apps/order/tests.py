import re
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django_redis import get_redis_connection

from goods.models import GoodsSKU, GoodsSPU, GoodsType
from promotion.models import Promotion, Referral, UserCoupon
from promotion.services import build_quote, grant_coupon
from user.models import Address, User
from utils.cart import clear_cart

from . import payment
from .models import OrderComment, OrderGoods, OrderInfo
from .services import CommentError, audit_order_comment, cancel_order, submit_order_comment
from .views import _write_order


class OrderPromotionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="order-user",
            password="pass1234",
            email="order@example.com",
            is_active=True,
        )
        self.address = Address.objects.create(
            user=self.user,
            receiver="下单人",
            addr="江苏省南京市测试路 1 号",
            phone="13900000000",
            is_default=True,
        )
        self.goods_type = GoodsType.objects.create(name="订单分类", logo="order")
        self.spu = GoodsSPU.objects.create(name="订单商品")
        self.sku = GoodsSKU.objects.create(
            type=self.goods_type,
            spu=self.spu,
            name="订单测试商品",
            price=Decimal("120.00"),
            stock=20,
            sales=0,
            status=GoodsSKU.STATUS_ONLINE,
        )
        Promotion.objects.create(
            slug="free-shipping-order",
            name="满 99 免运费",
            kind=Promotion.KIND_FREE_SHIPPING,
            is_active=True,
            shipping_threshold=Decimal("99.00"),
            discount_amount=Decimal("10.00"),
            stackable=True,
        )
        self.newcomer = Promotion.objects.create(
            slug="new-customer-order",
            name="新客立减 20 元",
            kind=Promotion.KIND_NEW_CUSTOMER,
            is_active=True,
            discount_amount=Decimal("20.00"),
            stackable=False,
        )

    def tearDown(self):
        try:
            clear_cart(self.user.pk)
        except Exception:
            pass

    def test_write_order_saves_snapshots_and_consumes_coupon(self):
        coupon, _ = grant_coupon(self.user, self.newcomer, "new-customer:order-test")
        items = [{"sku": self.sku, "count": 1}]
        quote = build_quote(self.user, items, coupon_id=coupon.pk)
        self.assertEqual(quote.total_pay, Decimal("100.00"))

        with transaction.atomic():
            order, count = _write_order(
                self.user,
                self.address,
                OrderInfo.PAY_ALIPAY,
                items,
                quote.total_pay,
                coupon_id=coupon.pk,
            )

        self.assertEqual(count, 1)
        self.assertEqual(order.total_amount, Decimal("120.00"))
        self.assertEqual(order.original_total_amount, Decimal("130.00"))
        self.assertEqual(order.item_discount, Decimal("0.00"))
        self.assertEqual(order.coupon_discount, Decimal("20.00"))
        self.assertEqual(order.shipping_discount, Decimal("10.00"))
        self.assertEqual(order.total_discount, Decimal("30.00"))
        self.assertEqual(order.transit_price, Decimal("0.00"))
        self.assertEqual(order.total_pay, Decimal("100.00"))

        coupon.refresh_from_db()
        self.assertEqual(coupon.status, UserCoupon.STATUS_USED)
        self.assertEqual(coupon.used_order_id, order.pk)
        self.assertEqual(order.skus.get().price, Decimal("120.00"))
        self.assertEqual(
            set(order.promotions.values_list("kind", flat=True)),
            {Promotion.KIND_FREE_SHIPPING, Promotion.KIND_NEW_CUSTOMER},
        )
        self.sku.refresh_from_db()
        self.assertEqual(self.sku.stock, 19)

    def test_expected_amount_mismatch_rejects_order(self):
        items = [{"sku": self.sku, "count": 1}]
        with self.assertRaises(ValueError):
            with transaction.atomic():
                _write_order(
                    self.user,
                    self.address,
                    OrderInfo.PAY_ALIPAY,
                    items,
                    Decimal("0.01"),
                )
        self.assertFalse(OrderInfo.objects.filter(user=self.user).exists())
        self.sku.refresh_from_db()
        self.assertEqual(self.sku.stock, 20)

    def test_cancel_order_returns_unexpired_coupon(self):
        coupon, _ = grant_coupon(self.user, self.newcomer, "new-customer:cancel-test")
        items = [{"sku": self.sku, "count": 1}]
        quote = build_quote(self.user, items, coupon_id=coupon.pk)
        with transaction.atomic():
            order, _ = _write_order(
                self.user,
                self.address,
                OrderInfo.PAY_ALIPAY,
                items,
                quote.total_pay,
                coupon_id=coupon.pk,
            )

        outcome = cancel_order(order.order_id)
        self.assertEqual(outcome, "cancelled")
        coupon.refresh_from_db()
        self.assertEqual(coupon.status, UserCoupon.STATUS_AVAILABLE)
        self.assertIsNone(coupon.used_order_id)
        self.sku.refresh_from_db()
        self.assertEqual(self.sku.stock, 20)

    @override_settings(PROMOTION_REFERRAL_ENABLED=True)
    def test_referral_reward_is_idempotent_on_duplicate_payment_callback(self):
        inviter = User.objects.create_user(
            username="inviter-order",
            password="pass1234",
            email="inviter-order@example.com",
            is_active=True,
        )
        invitee = User.objects.create_user(
            username="invitee-order",
            password="pass1234",
            email="invitee-order@example.com",
            is_active=True,
        )
        invitee_address = Address.objects.create(
            user=invitee,
            receiver="被邀请人",
            addr="江苏省南京市邀请路 2 号",
            phone="13700000000",
        )
        Referral.objects.create(inviter=inviter, invitee=invitee, code="referral-code")
        Promotion.objects.create(
            slug="referral-reward-test",
            name="邀请奖励 10 元券",
            kind=Promotion.KIND_REFERRAL_REWARD,
            is_active=True,
            discount_amount=Decimal("10.00"),
            stackable=True,
        )
        order = OrderInfo.objects.create(
            order_id="REFERRAL-PAY-1",
            user=invitee,
            addr=invitee_address,
            total_amount=Decimal("100.00"),
            transit_price=Decimal("10.00"),
            original_total_amount=Decimal("110.00"),
            item_discount=Decimal("0.00"),
            coupon_discount=Decimal("0.00"),
            shipping_discount=Decimal("0.00"),
            total_discount=Decimal("0.00"),
        )

        ok, reason = payment.mark_paid(order.order_id, "TRADE-REFERRAL-1", Decimal("110.00"))
        self.assertTrue(ok, reason)
        referral = Referral.objects.get(invitee=invitee)
        referral.refresh_from_db()
        self.assertEqual(referral.status, Referral.STATUS_REWARDED)
        self.assertIsNotNone(referral.reward_coupon_id)
        self.assertEqual(
            UserCoupon.objects.filter(
                user=inviter, grant_key=f"referral:{referral.pk}"
            ).count(),
            1,
        )

        ok, reason = payment.mark_paid(order.order_id, "TRADE-REFERRAL-1", Decimal("110.00"))
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "already_paid")
        self.assertEqual(UserCoupon.objects.filter(user=inviter).count(), 1)

    def test_payment_amount_mismatch_keeps_order_unpaid(self):
        order = OrderInfo.objects.create(
            order_id="AMOUNT-TAMPER-1",
            user=self.user,
            addr=self.address,
            total_amount=Decimal("120.00"),
            transit_price=Decimal("10.00"),
            original_total_amount=Decimal("130.00"),
            item_discount=Decimal("0.00"),
            coupon_discount=Decimal("0.00"),
            shipping_discount=Decimal("0.00"),
            total_discount=Decimal("0.00"),
        )
        ok, reason = payment.mark_paid(order.order_id, "TRADE-TAMPER-1", Decimal("0.01"))
        self.assertFalse(ok)
        self.assertEqual(reason, "amount_mismatch")
        order.refresh_from_db()
        self.assertEqual(order.pay_status, OrderInfo.STATUS_UNPAID)

    def test_historical_order_keeps_promotion_snapshot_after_campaign_ends(self):
        now = timezone.now()
        promotion = Promotion.objects.create(
            slug="weekend-snapshot",
            name="周末五折快照",
            kind=Promotion.KIND_ITEM_DISCOUNT,
            is_active=True,
            discount_rate=Decimal("0.5000"),
            start_at=now - timedelta(minutes=1),
            end_at=now + timedelta(hours=1),
            stackable=False,
        )
        from promotion.models import PromotionScope

        PromotionScope.objects.create(promotion=promotion, category=self.goods_type)
        items = [{"sku": self.sku, "count": 1}]
        quote = build_quote(self.user, items)
        self.assertEqual(quote.total_pay, Decimal("70.00"))
        with transaction.atomic():
            order, _ = _write_order(
                self.user,
                self.address,
                OrderInfo.PAY_ALIPAY,
                items,
                quote.total_pay,
            )

        promotion.end_at = timezone.now() - timedelta(seconds=1)
        promotion.save(update_fields=["end_at", "update_time"])
        order.refresh_from_db()
        line = order.skus.get()
        self.assertEqual(line.price, Decimal("60.00"))
        self.assertEqual(line.original_price, Decimal("120.00"))
        self.assertEqual(line.promotion_name, "周末五折快照")
        snapshot = order.promotions.get(kind=Promotion.KIND_ITEM_DISCOUNT)
        self.assertEqual(snapshot.discount_amount, Decimal("60.00"))
        self.assertEqual(snapshot.snapshot_name, "周末五折快照")

        after = build_quote(self.user, items)
        self.assertEqual(after.lines[0].unit_price, Decimal("120.00"))

    @override_settings(PROMOTION_PRICING_ENABLED=False)
    def test_global_pricing_switch_rolls_back_to_original_price(self):
        now = timezone.now()
        promotion = Promotion.objects.create(
            slug="weekend-disabled",
            name="停用开关测试",
            kind=Promotion.KIND_ITEM_DISCOUNT,
            is_active=True,
            discount_rate=Decimal("0.5000"),
            start_at=now - timedelta(minutes=1),
            end_at=now + timedelta(hours=1),
            stackable=False,
        )
        from promotion.models import PromotionScope

        PromotionScope.objects.create(promotion=promotion, category=self.goods_type)
        quote = build_quote(self.user, [{"sku": self.sku, "count": 1}])
        self.assertEqual(quote.item_discount, Decimal("0.00"))
        self.assertEqual(quote.shipping_discount, Decimal("0.00"))
        self.assertEqual(quote.total_pay, Decimal("130.00"))

    def test_http_add_place_commit_flow(self):
        try:
            get_redis_connection("default").ping()
        except Exception:
            self.skipTest("本机 Redis 不可用，跳过 HTTP 购物车链路")

        self.client.force_login(self.user)
        add_response = self.client.post(
            reverse("cart:add", args=[self.sku.pk]),
            {"count": "1"},
        )
        self.assertEqual(add_response.status_code, 200)
        self.assertEqual(add_response.json().get("code"), 0)

        place_response = self.client.get(reverse("order:place"))
        self.assertEqual(place_response.status_code, 200)
        html = place_response.content.decode("utf-8")

        def hidden(name):
            match = re.search(rf'name="{name}" value="([^"]*)"', html)
            return match.group(1) if match else ""

        payload = {
            "addr_id": str(self.address.pk),
            "pay_method": str(OrderInfo.PAY_ALIPAY),
            "submit_token": hidden("submit_token"),
            "expected_count": hidden("expected_count"),
            "expected_amount": hidden("expected_amount"),
            "expected_fingerprint": hidden("expected_fingerprint"),
            "coupon_id": hidden("coupon_id"),
        }
        self.assertTrue(payload["submit_token"])
        self.assertEqual(payload["expected_amount"], "100.00")

        commit_response = self.client.post(reverse("order:commit"), payload)
        self.assertEqual(commit_response.status_code, 302)
        order = OrderInfo.objects.get(user=self.user)
        self.assertEqual(order.total_pay, Decimal("100.00"))
        self.assertEqual(order.coupon_discount, Decimal("20.00"))
        self.assertEqual(order.shipping_discount, Decimal("10.00"))
        self.assertTrue(UserCoupon.objects.filter(user=self.user, status=UserCoupon.STATUS_USED).exists())

    def test_old_order_total_pay_falls_back_without_discount_fields(self):
        order = OrderInfo.objects.create(
            order_id="LEGACY-ORDER",
            user=self.user,
            addr=self.address,
            total_amount=Decimal("50.00"),
            transit_price=Decimal("10.00"),
            original_total_amount=None,
            item_discount=None,
            coupon_discount=None,
            shipping_discount=None,
            total_discount=None,
        )
        self.assertEqual(order.total_pay, Decimal("60.00"))

    # ---- P0 安全加固回归：下单幂等令牌 + mock 收银台 DEBUG 闸门 ----

    def _make_unpaid_order(self, order_id="MOCK-GATE-1"):
        return OrderInfo.objects.create(
            order_id=order_id,
            user=self.user,
            addr=self.address,
            total_amount=Decimal("120.00"),
            transit_price=Decimal("10.00"),
            original_total_amount=Decimal("130.00"),
            item_discount=Decimal("0.00"),
            coupon_discount=Decimal("0.00"),
            shipping_discount=Decimal("0.00"),
            total_discount=Decimal("0.00"),
        )

    def test_submit_token_consumed_exactly_once(self):
        from .views import _consume_submit_token, _issue_submit_token

        token = _issue_submit_token(self.user.pk)
        self.assertTrue(_consume_submit_token(self.user.pk, token))
        self.assertFalse(_consume_submit_token(self.user.pk, token))

    def test_submit_token_missing_is_rejected(self):
        from .views import _consume_submit_token

        self.assertFalse(_consume_submit_token(self.user.pk, ""))
        self.assertFalse(_consume_submit_token(self.user.pk, None))

    @override_settings(PAYMENT_PROVIDER="mock", DEBUG=False)
    def test_mock_cashier_disabled_without_debug(self):
        order = self._make_unpaid_order()
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse("order:mock_cashier", args=[order.order_id])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("order:mock_pay_success", args=[order.order_id])).status_code,
            404,
        )

    @override_settings(PAYMENT_PROVIDER="mock", DEBUG=False)
    def test_create_payment_rejects_mock_without_debug(self):
        order = self._make_unpaid_order()
        with self.assertRaises(payment.PaymentError):
            payment.create_payment(order)


class OrderCommentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="comment-owner",
            password="pass1234",
            email="comment-owner@example.com",
            is_active=True,
        )
        self.other_user = User.objects.create_user(
            username="comment-other",
            password="pass1234",
            email="comment-other@example.com",
            is_active=True,
        )
        self.staff = User.objects.create_user(
            username="comment-staff",
            password="pass1234",
            email="comment-staff@example.com",
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.address = Address.objects.create(
            user=self.user,
            receiver="评论用户",
            addr="江苏省南京市评论路 1 号",
            phone="13900000001",
        )
        self.goods_type = GoodsType.objects.create(name="评论分类", logo="comment")
        self.spu = GoodsSPU.objects.create(name="评论商品")
        self.sku = GoodsSKU.objects.create(
            type=self.goods_type,
            spu=self.spu,
            name="评论测试商品",
            price=Decimal("99.00"),
            stock=20,
            sales=0,
            status=GoodsSKU.STATUS_ONLINE,
        )
        self.order = self._make_order("COMMENT-ORDER-1")
        self.line = self._make_line(self.order)

    def _make_order(self, order_id, *, user=None, pay_status=OrderInfo.STATUS_PAID):
        return OrderInfo.objects.create(
            order_id=order_id,
            user=user or self.user,
            addr=self.address,
            total_amount=Decimal("99.00"),
            transit_price=Decimal("0.00"),
            original_total_amount=Decimal("99.00"),
            item_discount=Decimal("0.00"),
            coupon_discount=Decimal("0.00"),
            shipping_discount=Decimal("0.00"),
            total_discount=Decimal("0.00"),
            pay_status=pay_status,
            paid_amount=(
                Decimal("99.00")
                if pay_status == OrderInfo.STATUS_PAID
                else None
            ),
            pay_time=timezone.now() if pay_status == OrderInfo.STATUS_PAID else None,
        )

    def _make_line(self, order):
        return OrderGoods.objects.create(
            order=order,
            sku=self.sku,
            count=1,
            price=Decimal("99.00"),
            sku_name=self.sku.name,
        )

    def _review_url(self, line=None):
        return reverse("order:comment", args=[(line or self.line).pk])

    def test_owner_can_submit_comment_and_it_starts_pending(self):
        self.client.force_login(self.user)

        response = self.client.post(self._review_url(), {"content": "  很好  "})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.endswith("#comments"))
        review = OrderComment.objects.get(order_goods=self.line)
        self.assertEqual(review.content, "很好")
        self.assertEqual(review.status, OrderComment.STATUS_PENDING)
        self.assertIsNone(review.audited_by)

    def test_unpaid_order_cannot_be_commented(self):
        self.order.pay_status = OrderInfo.STATUS_UNPAID
        self.order.pay_time = None
        self.order.paid_amount = None
        self.order.save(update_fields=["pay_status", "pay_time", "paid_amount"])
        self.client.force_login(self.user)

        response = self.client.post(self._review_url(), {"content": "未支付评论"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OrderComment.objects.filter(order_goods=self.line).exists())

    def test_other_user_cannot_comment(self):
        self.client.force_login(self.other_user)

        response = self.client.post(self._review_url(), {"content": "越权评论"})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(OrderComment.objects.filter(order_goods=self.line).exists())

    def test_duplicate_comment_is_rejected(self):
        OrderComment.objects.create(
            order_goods=self.line,
            content="第一次评论",
            status=OrderComment.STATUS_APPROVED,
        )
        self.client.force_login(self.user)

        response = self.client.post(self._review_url(), {"content": "重复评论"})

        self.assertEqual(response.status_code, 302)
        review = OrderComment.objects.get(order_goods=self.line)
        self.assertEqual(review.content, "第一次评论")
        self.assertEqual(OrderComment.objects.count(), 1)

    def test_rejected_comment_can_be_resubmitted(self):
        review = OrderComment.objects.create(
            order_goods=self.line,
            content="旧内容",
            status=OrderComment.STATUS_REJECTED,
            audit_remark="请补充细节",
            audited_by=self.staff,
            audited_at=timezone.now(),
        )
        self.client.force_login(self.user)

        response = self.client.post(self._review_url(), {"content": "新内容"})

        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertEqual(review.content, "新内容")
        self.assertEqual(review.status, OrderComment.STATUS_PENDING)
        self.assertEqual(review.audit_remark, "")
        self.assertIsNone(review.audited_by)
        self.assertIsNone(review.audited_at)

    def test_blank_and_too_long_content_are_rejected(self):
        with self.assertRaises(CommentError):
            submit_order_comment(self.user, self.line.pk, "   ")
        with self.assertRaises(CommentError):
            submit_order_comment(
                self.user,
                self.line.pk,
                "长" * (OrderComment.MAX_CONTENT_LENGTH + 1),
            )
        self.assertFalse(OrderComment.objects.filter(order_goods=self.line).exists())

    def test_audit_requires_staff_and_records_auditor(self):
        review = OrderComment.objects.create(
            order_goods=self.line,
            content="待审核",
        )
        with self.assertRaises(PermissionError):
            audit_order_comment(review, auditor=self.user, approved=True)

        audit_order_comment(
            review,
            auditor=self.staff,
            approved=True,
            remark="内容合规",
        )
        review.refresh_from_db()
        self.assertEqual(review.status, OrderComment.STATUS_APPROVED)
        self.assertEqual(review.audited_by, self.staff)
        self.assertIsNotNone(review.audited_at)
        self.assertEqual(review.audit_remark, "内容合规")

    def test_goods_detail_only_shows_approved_comments(self):
        OrderComment.objects.create(
            order_goods=self.line,
            content="审核通过的评价",
            status=OrderComment.STATUS_APPROVED,
        )
        pending_order = self._make_order("COMMENT-ORDER-PENDING")
        pending_line = self._make_line(pending_order)
        OrderComment.objects.create(
            order_goods=pending_line,
            content="仍在审核的评价",
            status=OrderComment.STATUS_PENDING,
        )

        response = self.client.get(reverse("goods:detail", args=[self.sku.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "审核通过的评价")
        self.assertNotContains(response, "仍在审核的评价")
        self.assertEqual(response.context["review_count"], 1)

    def test_comment_content_is_escaped_on_goods_detail(self):
        OrderComment.objects.create(
            order_goods=self.line,
            content="<script>alert(1)</script>",
            status=OrderComment.STATUS_APPROVED,
        )

        response = self.client.get(reverse("goods:detail", args=[self.sku.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_order_detail_shows_pending_comment_without_edit_form(self):
        OrderComment.objects.create(
            order_goods=self.line,
            content="我的待审核评论",
            status=OrderComment.STATUS_PENDING,
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("order:detail", args=[self.order.order_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "我的待审核评论")
        self.assertContains(response, "待审核")
        self.assertNotContains(response, self._review_url())

    @override_settings(ORDER_COMMENT_ENABLED=False)
    def test_comment_submission_can_be_disabled(self):
        self.client.force_login(self.user)

        response = self.client.post(self._review_url(), {"content": "关闭期间评论"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OrderComment.objects.filter(order_goods=self.line).exists())

    def test_admin_audit_actions_approve_and_reject(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from .admin import OrderCommentAdmin

        review = OrderComment.objects.create(
            order_goods=self.line,
            content="后台审核测试",
        )
        request = RequestFactory().post("/admin/order/ordercomment/")
        request.user = self.staff
        model_admin = OrderCommentAdmin(OrderComment, AdminSite())
        model_admin.message_user = lambda *args, **kwargs: None

        model_admin.approve_comments(
            request,
            OrderComment.objects.filter(pk=review.pk),
        )
        review.refresh_from_db()
        self.assertEqual(review.status, OrderComment.STATUS_APPROVED)

        model_admin.reject_comments(
            request,
            OrderComment.objects.filter(pk=review.pk),
        )
        review.refresh_from_db()
        self.assertEqual(review.status, OrderComment.STATUS_REJECTED)
        self.assertEqual(review.audit_remark, "不符合评论规范")
