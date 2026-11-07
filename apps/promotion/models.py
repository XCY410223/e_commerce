"""促销活动、用户权益和订单优惠快照模型。

展示配置仍由 goods.IndexPromotionBanner 管理；本模块只保存可参与计算的规则、
用户券和邀请关系，避免活动文案直接参与价格计算。
"""

import secrets

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from db.base_model import AllObjectsManager, BaseModel, SoftDeleteManager


class PromotionQuerySet(models.QuerySet):
    """活动查询集合，默认只返回未软删除的记录。"""

    def effective(self, at=None):
        at = at or timezone.now()
        return self.filter(is_delete=False, is_active=True).filter(
            models.Q(start_at__isnull=True) | models.Q(start_at__lte=at),
            models.Q(end_at__isnull=True) | models.Q(end_at__gte=at),
        )


class PromotionManager(SoftDeleteManager.from_queryset(PromotionQuerySet)):
    pass


class Promotion(BaseModel):
    """活动规则表。

    discount_rate 表示“成交价占原价的比例”，例如 0.50 表示五折；这样比
    “折扣百分比”更不容易在算价代码里产生方向歧义。
    """

    objects = PromotionManager()
    all_objects = AllObjectsManager()

    KIND_NEW_CUSTOMER = "new_customer"
    KIND_FREE_SHIPPING = "free_shipping"
    KIND_ITEM_DISCOUNT = "item_discount"
    KIND_REFERRAL_REWARD = "referral_reward"
    KIND_CHOICES = (
        (KIND_NEW_CUSTOMER, "新客立减"),
        (KIND_FREE_SHIPPING, "满额免运费"),
        (KIND_ITEM_DISCOUNT, "商品限时折扣"),
        (KIND_REFERRAL_REWARD, "邀请奖励券"),
    )

    slug = models.SlugField(max_length=50, unique=True, verbose_name="活动标识")
    name = models.CharField(max_length=50, verbose_name="活动名称")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, verbose_name="活动类型")
    is_active = models.BooleanField(default=False, verbose_name="是否启用")
    start_at = models.DateTimeField(blank=True, null=True, verbose_name="开始时间")
    end_at = models.DateTimeField(blank=True, null=True, verbose_name="结束时间")
    priority = models.SmallIntegerField(default=100, verbose_name="优先级")
    stackable = models.BooleanField(default=False, verbose_name="可与其他优惠叠加")
    min_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, verbose_name="最低消费"
    )
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, verbose_name="固定优惠金额"
    )
    discount_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=1,
        verbose_name="成交价比例",
        help_text="0.50 表示五折；仅商品折扣活动使用",
    )
    max_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="单笔最大优惠",
    )
    shipping_threshold = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="免运费门槛",
    )
    limit_per_user = models.PositiveIntegerField(default=1, verbose_name="每用户限次")
    coupon_valid_days = models.PositiveIntegerField(default=30, verbose_name="券有效期天数")
    description = models.TextField(blank=True, default="", verbose_name="内部说明")

    class Meta:
        db_table = "df_promotion"
        verbose_name = "促销活动"
        verbose_name_plural = verbose_name
        ordering = ["priority", "id"]
        indexes = [
            models.Index(
                fields=["kind", "is_active", "start_at", "end_at"],
                name="idx_promo_kind_time",
            ),
            models.Index(fields=["is_active", "start_at", "end_at"], name="idx_promo_time"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.start_at and self.end_at and self.start_at > self.end_at:
            raise ValidationError({"end_at": "结束时间不能早于开始时间"})
        if self.discount_rate <= 0:
            raise ValidationError({"discount_rate": "成交价比例必须大于 0"})
        if self.discount_rate > 1:
            raise ValidationError({"discount_rate": "成交价比例不能大于 1"})
        if self.discount_amount < 0 or self.min_amount < 0:
            raise ValidationError("优惠金额和最低消费不能为负数")
        if self.max_discount is not None and self.max_discount < 0:
            raise ValidationError({"max_discount": "最大优惠不能为负数"})
        if self.shipping_threshold is not None and self.shipping_threshold < 0:
            raise ValidationError({"shipping_threshold": "免运费门槛不能为负数"})

    def is_effective(self, at=None):
        """活动在指定时刻是否生效；软删除、停用和过期都会返回 False。"""
        at = at or timezone.now()
        if self.is_delete or not self.is_active:
            return False
        if self.start_at and at < self.start_at:
            return False
        if self.end_at and at > self.end_at:
            return False
        return True

    @property
    def is_coupon(self):
        return self.kind in {self.KIND_NEW_CUSTOMER, self.KIND_REFERRAL_REWARD}


class PromotionScope(BaseModel):
    """活动参与范围：按分类或 SKU 包含/排除。"""

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    promotion = models.ForeignKey(
        Promotion, on_delete=models.CASCADE, related_name="scopes", verbose_name="所属活动"
    )
    category = models.ForeignKey(
        "goods.GoodsType",
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="promotion_scopes",
        verbose_name="商品分类",
    )
    sku = models.ForeignKey(
        "goods.GoodsSKU",
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="promotion_scopes",
        verbose_name="商品SKU",
    )
    include = models.BooleanField(default=True, verbose_name="包含/排除")

    class Meta:
        db_table = "df_promotion_scope"
        verbose_name = "活动范围"
        verbose_name_plural = verbose_name
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["promotion", "category", "sku"], name="uniq_promo_scope"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(category__isnull=False, sku__isnull=True)
                    | models.Q(category__isnull=True, sku__isnull=False)
                ),
                name="chk_promo_scope_one_target",
            ),
        ]
        indexes = [models.Index(fields=["promotion", "include"], name="idx_scope_promo")]

    def __str__(self):
        target = self.category or self.sku or "全部"
        return f"{self.promotion.name} - {'包含' if self.include else '排除'} {target}"

    def clean(self):
        if bool(self.category_id) == bool(self.sku_id):
            raise ValidationError("参与范围必须且只能指定分类或 SKU 其中一项")


class UserCoupon(BaseModel):
    """用户优惠券，优惠参数在发券时做快照。"""

    STATUS_AVAILABLE = "available"
    STATUS_USED = "used"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = (
        (STATUS_AVAILABLE, "可用"),
        (STATUS_USED, "已使用"),
        (STATUS_EXPIRED, "已过期"),
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    user = models.ForeignKey(
        "user.User", on_delete=models.CASCADE, related_name="coupons", verbose_name="所属用户"
    )
    promotion = models.ForeignKey(
        Promotion, on_delete=models.PROTECT, related_name="coupons", verbose_name="来源活动"
    )
    code = models.CharField(max_length=32, unique=True, editable=False, verbose_name="券码")
    grant_key = models.CharField(
        max_length=128,
        unique=True,
        verbose_name="发放幂等键",
        help_text="同一业务来源只能发放一次，例如 new-customer:用户ID",
    )
    title = models.CharField(max_length=100, verbose_name="券名称快照")
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, verbose_name="优惠金额快照"
    )
    min_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, verbose_name="最低消费快照"
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_AVAILABLE, verbose_name="状态"
    )
    expires_at = models.DateTimeField(verbose_name="过期时间")
    used_order = models.ForeignKey(
        "order.OrderInfo",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="used_coupons",
        verbose_name="核销订单",
    )
    used_at = models.DateTimeField(blank=True, null=True, verbose_name="使用时间")

    class Meta:
        db_table = "df_user_coupon"
        verbose_name = "用户优惠券"
        verbose_name_plural = verbose_name
        ordering = ["-create_time"]
        indexes = [
            models.Index(fields=["user", "status", "expires_at"], name="idx_coupon_user_state"),
            models.Index(fields=["used_order"], name="idx_coupon_order"),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.title}"

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = secrets.token_hex(8).upper()
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_available(self):
        return (
            self.status == self.STATUS_AVAILABLE
            and not self.is_expired
            and self.promotion.is_effective()
        )

    @property
    def status_label(self):
        if self.status == self.STATUS_AVAILABLE and self.is_expired:
            return "已过期"
        return self.get_status_display()


class Referral(BaseModel):
    """邀请关系。被邀请人只能绑定一次，奖励只在其首单支付后发放。"""

    STATUS_PENDING = "pending"
    STATUS_REWARDED = "rewarded"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = (
        (STATUS_PENDING, "待首单"),
        (STATUS_REWARDED, "已发放"),
        (STATUS_REJECTED, "不合规"),
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    inviter = models.ForeignKey(
        "user.User",
        on_delete=models.CASCADE,
        related_name="sent_referrals",
        verbose_name="邀请人",
    )
    invitee = models.OneToOneField(
        "user.User",
        on_delete=models.CASCADE,
        related_name="received_referral",
        verbose_name="被邀请人",
    )
    code = models.CharField(max_length=255, verbose_name="邀请码快照")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, verbose_name="状态"
    )
    reward_coupon = models.OneToOneField(
        UserCoupon,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="referral_reward",
        verbose_name="奖励券",
    )
    completed_at = models.DateTimeField(blank=True, null=True, verbose_name="完成时间")
    reject_reason = models.CharField(
        max_length=100, blank=True, default="", verbose_name="驳回原因"
    )

    class Meta:
        db_table = "df_referral"
        verbose_name = "邀请关系"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["inviter", "invitee"], name="uniq_referral_pair"
            ),
        ]
        indexes = [
            models.Index(fields=["inviter", "status"], name="idx_referral_inviter"),
            models.Index(fields=["invitee", "status"], name="idx_referral_invitee"),
        ]

    def __str__(self):
        return f"{self.inviter.username} -> {self.invitee.username}"


class OrderPromotion(BaseModel):
    """订单实际命中的活动快照，活动后续改名或停用不影响历史订单解释。"""

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    order = models.ForeignKey(
        "order.OrderInfo",
        on_delete=models.CASCADE,
        related_name="promotions",
        verbose_name="所属订单",
    )
    promotion = models.ForeignKey(
        Promotion,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="order_snapshots",
        verbose_name="来源活动",
    )
    kind = models.CharField(max_length=20, verbose_name="活动类型")
    snapshot_name = models.CharField(max_length=100, verbose_name="活动名称快照")
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, verbose_name="优惠金额"
    )
    metadata = models.JSONField(default=dict, blank=True, verbose_name="计算明细")

    class Meta:
        db_table = "df_order_promotion"
        verbose_name = "订单活动快照"
        verbose_name_plural = verbose_name
        ordering = ["id"]
        indexes = [models.Index(fields=["order", "kind"], name="idx_order_promo")]

    def __str__(self):
        return f"{self.order.order_id} - {self.snapshot_name}"

    @property
    def kind_label(self):
        return dict(Promotion.KIND_CHOICES).get(self.kind, self.kind)
