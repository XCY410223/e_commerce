"""订单模块模型。

对应《数据库设计》中的「订单信息表」「订单商品表」。
总数目 / 总金额 / 成交单价 均为下单时快照，避免后续价格变动影响历史订单。
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import models

from db.base_model import AllObjectsManager, BaseModel, SoftDeleteManager, SoftDeleteQuerySet


class OrderInfoQuerySet(SoftDeleteQuerySet):
    """订单的常用查询集合。"""

    def for_user(self, user):
        """只返回指定用户的订单。"""
        return self.filter(user=user)


class OrderInfoManager(SoftDeleteManager.from_queryset(OrderInfoQuerySet)):
    pass


class OrderInfo(BaseModel):
    """订单信息表。"""

    objects = OrderInfoManager()
    all_objects = AllObjectsManager()

    PAY_ALIPAY = 1
    PAY_WECHAT = 2
    PAY_UNIONPAY = 3
    PAY_CHOICES = (
        (PAY_ALIPAY, "支付宝"),
        (PAY_WECHAT, "微信"),
        (PAY_UNIONPAY, "银联"),
    )

    STATUS_UNPAID = 0
    STATUS_PAID = 1
    STATUS_CANCELLED = 2
    STATUS_CHOICES = (
        (STATUS_UNPAID, "未支付"),
        (STATUS_PAID, "已支付"),
        (STATUS_CANCELLED, "已取消"),
    )

    order_id = models.CharField(max_length=64, unique=True, verbose_name="订单号")
    user = models.ForeignKey(
        "user.User", on_delete=models.PROTECT, related_name="orders", verbose_name="下单用户"
    )
    addr = models.ForeignKey(
        "user.Address", on_delete=models.PROTECT, related_name="orders", verbose_name="收货地址"
    )
    pay_method = models.SmallIntegerField(choices=PAY_CHOICES, default=PAY_ALIPAY, verbose_name="支付方式")
    total_count = models.IntegerField(default=1, verbose_name="商品总件数")
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="商品总金额")
    transit_price = models.DecimalField(max_digits=10, decimal_places=2, default=10, verbose_name="运费")
    original_total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="优惠前应付总额",
    )
    item_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="商品活动优惠",
    )
    coupon_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="优惠券优惠",
    )
    shipping_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="运费减免",
    )
    total_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="优惠合计",
    )
    receiver = models.CharField(max_length=20, blank=True, default="", verbose_name="收件人快照")
    phone = models.CharField(max_length=11, blank=True, default="", verbose_name="联系电话快照")
    address = models.CharField(max_length=256, blank=True, default="", verbose_name="收货地址快照")
    pay_status = models.SmallIntegerField(choices=STATUS_CHOICES, default=STATUS_UNPAID, verbose_name="支付状态")
    trade_no = models.CharField(
        max_length=100, blank=True, null=True, unique=True, verbose_name="支付流水号"
    )
    paid_amount = models.DecimalField(
        max_digits=10, decimal_places=2, blank=True, null=True, verbose_name="实付金额"
    )
    pay_time = models.DateTimeField(blank=True, null=True, verbose_name="支付完成时间")
    cancel_time = models.DateTimeField(blank=True, null=True, verbose_name="取消时间")

    class Meta:
        db_table = "df_order_info"
        verbose_name = "订单信息"
        verbose_name_plural = verbose_name
        ordering = ["-create_time"]
        indexes = [
            models.Index(fields=["user", "-create_time"], name="idx_order_user_time"),
            models.Index(fields=["pay_status", "create_time"], name="idx_order_pay_time"),
        ]

    def __str__(self):
        return self.order_id

    @property
    def total_pay(self):
        """最终应付；旧订单没有优惠字段时保持“商品金额 + 运费”的旧口径。"""
        original = self.original_total_amount
        if original is None:
            original = self.total_amount + self.transit_price
        discount = self.total_discount
        if discount is None:
            discount = (
                Decimal("0.00")
                + (self.item_discount or Decimal("0.00"))
                + (self.coupon_discount or Decimal("0.00"))
                + (self.shipping_discount or Decimal("0.00"))
            )
        return max(Decimal("0.00"), original - discount)

    @property
    def item_discount_amount(self):
        return self.item_discount or Decimal("0.00")

    @property
    def coupon_discount_amount(self):
        return self.coupon_discount or Decimal("0.00")

    @property
    def shipping_discount_amount(self):
        return self.shipping_discount or Decimal("0.00")

    @property
    def total_discount_amount(self):
        if self.total_discount is not None:
            return self.total_discount
        return (
            self.item_discount_amount
            + self.coupon_discount_amount
            + self.shipping_discount_amount
        )

    @property
    def is_paid(self):
        """是否已支付，模板中避免直接比较支付状态魔法值。"""
        return self.pay_status == self.STATUS_PAID

    @property
    def is_unpaid(self):
        """是否仍待支付；已取消订单不能继续发起支付。"""
        return self.pay_status == self.STATUS_UNPAID

    @property
    def is_cancelled(self):
        return self.pay_status == self.STATUS_CANCELLED

    @property
    def display_amount(self):
        """已支付优先展示实付金额，未支付展示应付总额。"""
        if self.is_paid and self.paid_amount is not None:
            return self.paid_amount
        return self.total_pay

    @property
    def amount_label(self):
        """与 display_amount 配对的金额说明。"""
        if self.is_paid:
            return "实付"
        if self.is_cancelled:
            return "订单金额"
        return "应付"

    @property
    def status_label(self):
        return self.get_pay_status_display()

    @property
    def status_css_class(self):
        if self.is_paid:
            return "status-paid"
        if self.is_cancelled:
            return "status-cancelled"
        return "status-unpaid"

    @property
    def display_receiver(self):
        if self.receiver:
            return self.receiver
        try:
            return self.addr.receiver
        except ObjectDoesNotExist:
            return ""

    @property
    def display_phone(self):
        if self.phone:
            return self.phone
        try:
            return self.addr.phone
        except ObjectDoesNotExist:
            return ""

    @property
    def display_address(self):
        if self.address:
            return self.address
        try:
            return self.addr.addr
        except ObjectDoesNotExist:
            return ""


class OrderGoods(BaseModel):
    """订单商品表。"""

    order = models.ForeignKey(
        OrderInfo, on_delete=models.CASCADE, related_name="skus", verbose_name="所属订单"
    )
    sku = models.ForeignKey(
        "goods.GoodsSKU", on_delete=models.PROTECT, related_name="order_goods", verbose_name="商品SKU"
    )
    count = models.IntegerField(default=1, verbose_name="商品数量")
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="成交单价")
    original_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="原价快照",
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="商品优惠金额",
    )
    promotion_name = models.CharField(
        max_length=100, blank=True, default="", verbose_name="活动名称快照"
    )
    sku_name = models.CharField(max_length=100, blank=True, default="", verbose_name="商品名称快照")
    # 兼容保留：历史数据已迁移至 OrderComment，下一版本再删除该列。
    comment = models.CharField(max_length=256, blank=True, null=True, verbose_name="评论")

    class Meta:
        db_table = "df_order_goods"
        verbose_name = "订单商品"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.order.order_id} - {self.sku.name}"

    @property
    def amount(self):
        """明细小计 = 成交单价 × 数量（单价为下单快照，不随后续调价变化）。"""
        return self.price * self.count

    @property
    def original_amount(self):
        return (self.original_price or self.price) * self.count

    @property
    def discount_amount_value(self):
        return self.discount_amount or Decimal("0.00")

    @property
    def display_name(self):
        if self.sku_name:
            return self.sku_name
        try:
            return self.sku.name
        except ObjectDoesNotExist:
            return "商品已删除"


class OrderComment(BaseModel):
    """订单商品评论：每条订单商品最多一条，审核通过后才对外展示。"""

    MAX_CONTENT_LENGTH = 500
    MAX_AUDIT_REMARK_LENGTH = 200

    STATUS_PENDING = 0
    STATUS_APPROVED = 1
    STATUS_REJECTED = 2
    STATUS_CHOICES = (
        (STATUS_PENDING, "待审核"),
        (STATUS_APPROVED, "已通过"),
        (STATUS_REJECTED, "已驳回"),
    )

    order_goods = models.OneToOneField(
        OrderGoods,
        on_delete=models.CASCADE,
        related_name="review",
        verbose_name="订单商品",
    )
    content = models.CharField(
        max_length=MAX_CONTENT_LENGTH, verbose_name="评论内容"
    )
    status = models.SmallIntegerField(
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        verbose_name="审核状态",
    )
    audit_remark = models.CharField(
        max_length=MAX_AUDIT_REMARK_LENGTH,
        blank=True,
        default="",
        verbose_name="审核备注",
    )
    audited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audited_order_comments",
        verbose_name="审核人",
    )
    audited_at = models.DateTimeField(blank=True, null=True, verbose_name="审核时间")

    class Meta:
        db_table = "df_order_comment"
        verbose_name = "订单评论"
        verbose_name_plural = verbose_name
        ordering = ["-create_time"]
        indexes = [
            models.Index(
                fields=["status", "-create_time"],
                name="idx_comment_status_time",
            ),
        ]

    def __str__(self):
        return f"{self.order_goods_id} - {self.get_status_display()}"

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def status_css_class(self):
        if self.status == self.STATUS_APPROVED:
            return "status-approved"
        if self.status == self.STATUS_REJECTED:
            return "status-rejected"
        return "status-pending"

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING

    @property
    def is_approved(self):
        return self.status == self.STATUS_APPROVED

    @property
    def is_rejected(self):
        return self.status == self.STATUS_REJECTED

    @property
    def display_user(self):
        """商品页只展示脱敏用户名，避免泄露完整账号。"""
        username = self.order_goods.order.user.username
        if len(username) <= 1:
            return username or "匿名用户"
        if len(username) == 2:
            return f"{username[0]}*"
        return f"{username[0]}{'*' * (len(username) - 2)}{username[-1]}"
