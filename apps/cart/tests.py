"""购物车 Redis 读写与 Lua 原子操作的单元测试。

覆盖最容易出错的四条路径：加购库存上限（Lua 原子校验）、修改数量的 CAS 冲突、
下单清理的幂等标记、游客购物车登录合并。Redis 不可用时相关用例会被跳过。
"""

from decimal import Decimal

from django.test import TestCase
from django_redis import get_redis_connection

from goods.models import GoodsSKU, GoodsSPU, GoodsType
from user.models import User
from utils.cart import (
    CartError,
    add_to_cart,
    add_to_cart_ref,
    clear_cart,
    clear_cart_after_order,
    guest_cart,
    guest_cart_key,
    merge_guest_cart,
    new_guest_token,
    read_cart,
    update_cart_item,
)


class CartTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="cart-user",
            password="pass1234",
            email="cart@example.com",
            is_active=True,
        )
        goods_type = GoodsType.objects.create(name="购物车分类", logo="cart")
        spu = GoodsSPU.objects.create(name="购物车商品")
        self.sku = GoodsSKU.objects.create(
            type=goods_type,
            spu=spu,
            name="购物车测试商品",
            price=Decimal("10.00"),
            stock=5,
            sales=0,
            status=GoodsSKU.STATUS_ONLINE,
        )
        clear_cart(self.user.pk)

    def tearDown(self):
        try:
            clear_cart(self.user.pk)
        except Exception:
            pass

    def _require_redis(self):
        try:
            get_redis_connection("default").ping()
        except Exception:
            self.skipTest("本机 Redis 不可用，跳过购物车用例")

    def _redis(self):
        return get_redis_connection("default")

    def test_add_to_cart_respects_stock_limit(self):
        self._require_redis()
        self.assertEqual(add_to_cart(self.user.pk, self.sku.pk, 3), 3)
        with self.assertRaises(CartError) as cm:
            add_to_cart(self.user.pk, self.sku.pk, 3)  # 已有 3，再加 3 = 6 > 5
        self.assertEqual(cm.exception.code, "stock_limit")

    def test_add_single_shot_over_stock_rejected(self):
        self._require_redis()
        with self.assertRaises(CartError) as cm:
            add_to_cart(self.user.pk, self.sku.pk, 6)  # 6 > 库存 5
        self.assertEqual(cm.exception.code, "stock_limit")

    def test_update_cart_cas_conflict(self):
        self._require_redis()
        add_to_cart(self.user.pk, self.sku.pk, 2)
        # 页面看到的是 1 件，Redis 里实际已是 2 件 → 后写不得覆盖
        with self.assertRaises(CartError) as cm:
            update_cart_item(self.user.pk, self.sku.pk, 5, expected=1)
        self.assertEqual(cm.exception.code, "cart_conflict")

    def test_clear_cart_after_order_is_idempotent(self):
        self._require_redis()
        order_id = "ORDER-IDEM-CART-1"
        self._redis().delete(f"cart:cleanup:{order_id}")
        add_to_cart(self.user.pk, self.sku.pk, 5)

        ordered = [{"sku": self.sku, "count": 2}]
        self.assertTrue(clear_cart_after_order(self.user.pk, ordered, order_id))
        _, count, _ = read_cart(self.user.pk)
        self.assertEqual(count, 3)

        # 同一 order_id 再清一次：幂等标记存在，不能再扣减
        self.assertTrue(clear_cart_after_order(self.user.pk, ordered, order_id))
        _, count, _ = read_cart(self.user.pk)
        self.assertEqual(count, 3)

    def test_merge_guest_cart_into_user(self):
        self._require_redis()
        token = new_guest_token()
        add_to_cart_ref(guest_cart(token), self.sku.pk, 3)

        self.assertTrue(merge_guest_cart(self.user.pk, token))
        _, count, _ = read_cart(self.user.pk)
        self.assertEqual(count, 3)
        # 合并成功后游客键被删除
        self.assertEqual(self._redis().exists(guest_cart_key(token)), 0)
