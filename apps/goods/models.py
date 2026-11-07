"""商品模块模型。

对应《数据库设计》中的：商品种类表、商品SPU表、商品SKU表、商品图片表、
首页轮播商品表、首页促销活动表、首页分类商品展示表。

设计要点：主图统一存放在商品图片表，销量保留冗余字段以减少列表页聚合查询。
"""

from django.db import models, transaction
from django.urls import reverse

from db.base_model import AllObjectsManager, BaseModel, SoftDeleteManager, SoftDeleteQuerySet


class GoodsSKUQuerySet(SoftDeleteQuerySet):
    """商品 SKU 的常用查询集合。"""

    def online(self):
        """只返回已上架商品，可继续链式过滤和排序。"""
        return self.filter(status=self.model.STATUS_ONLINE)


class GoodsSKUManager(SoftDeleteManager.from_queryset(GoodsSKUQuerySet)):
    pass


class GoodsType(BaseModel):
    """商品种类表。"""

    name = models.CharField(max_length=20, verbose_name="种类名称")
    logo = models.CharField(max_length=100, blank=True, default="", verbose_name="标识")
    image = models.ImageField(upload_to="type/", blank=True, null=True, verbose_name="种类图片")

    class Meta:
        db_table = "df_goods_type"
        verbose_name = "商品种类"
        verbose_name_plural = verbose_name
        ordering = ["id"]

    def __str__(self):
        return self.name


class GoodsSPU(BaseModel):
    """商品SPU表（标准化产品单元）。"""

    name = models.CharField(max_length=20, verbose_name="SPU名称")
    detail = models.TextField(blank=True, default="", verbose_name="商品详情")

    class Meta:
        db_table = "df_goods_spu"
        verbose_name = "商品SPU"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.name


class GoodsSKU(BaseModel):
    """商品SKU表（最小库存单元）。"""

    objects = GoodsSKUManager()
    all_objects = AllObjectsManager()

    STATUS_OFFLINE = 0
    STATUS_ONLINE = 1

    type = models.ForeignKey(
        GoodsType, on_delete=models.CASCADE, related_name="skus", verbose_name="商品种类"
    )
    spu = models.ForeignKey(
        GoodsSPU, on_delete=models.CASCADE, related_name="skus", verbose_name="商品SPU"
    )
    name = models.CharField(max_length=100, verbose_name="商品名称")
    spec = models.CharField(max_length=20, blank=True, default="", verbose_name="规格")
    desc = models.CharField(max_length=256, blank=True, default="", verbose_name="商品简介")
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="单价")
    unite = models.CharField(max_length=20, default="500g", verbose_name="计量单位")
    stock = models.IntegerField(default=0, verbose_name="库存")
    sales = models.IntegerField(default=0, verbose_name="销量")  # 冗余字段
    status = models.SmallIntegerField(
        choices=((STATUS_OFFLINE, "下线"), (STATUS_ONLINE, "上线")),
        default=STATUS_ONLINE,
        verbose_name="状态",
    )

    class Meta:
        db_table = "df_goods_sku"
        verbose_name = "商品SKU"
        verbose_name_plural = verbose_name
        ordering = ["-create_time"]
        indexes = [
            models.Index(fields=["type", "status"], name="idx_sku_type_status"),
            models.Index(fields=["spu"], name="idx_sku_spu"),
        ]

    def __str__(self):
        return self.name

    @property
    def primary_image(self):
        """返回主图；没有标记主图时回退到排序最靠前的图片。"""
        prefetched = getattr(self, "_prefetched_objects_cache", {}).get("images")
        if prefetched is not None:
            alive = [image for image in prefetched if not image.is_delete]
            first = next((image for image in alive if image.is_main), None)
            first = first or (alive[0] if alive else None)
        else:
            first = (
                self.images.filter(is_delete=False)
                .order_by("-is_main", "pk")
                .only("image", "is_main")
                .first()
            )
        return first.image if first else None


class GoodsImage(BaseModel):
    """商品图片表（SKU 多图）。"""

    sku = models.ForeignKey(
        GoodsSKU, on_delete=models.CASCADE, related_name="images", verbose_name="所属SKU"
    )
    image = models.ImageField(upload_to="goods/", verbose_name="图片")
    is_main = models.BooleanField(default=False, verbose_name="主图")

    class Meta:
        db_table = "df_goods_image"
        verbose_name = "商品图片"
        verbose_name_plural = verbose_name
        ordering = ["-is_main", "pk"]

    def save(self, *args, **kwargs):
        # 主图切换和图片落库必须在同一事务内串行执行，否则两个并发请求
        # 可能各自看到“没有其他主图”，最终给同一 SKU 留下多张主图。
        using = kwargs.get("using") or self._state.db
        with transaction.atomic(using=using):
            GoodsSKU.objects.select_for_update().filter(pk=self.sku_id).first()
            super().save(*args, **kwargs)
            if self.is_main:
                type(self).objects.filter(sku_id=self.sku_id, is_main=True).exclude(
                    pk=self.pk
                ).update(is_main=False)


class IndexGoodsBanner(BaseModel):
    """首页轮播商品表。"""

    sku = models.ForeignKey(
        GoodsSKU, on_delete=models.CASCADE, related_name="banners", verbose_name="商品SKU"
    )
    image = models.ImageField(upload_to="banner/", verbose_name="图片")
    index = models.SmallIntegerField(default=0, verbose_name="展示顺序")

    class Meta:
        db_table = "df_index_goods_banner"
        verbose_name = "首页轮播商品"
        verbose_name_plural = verbose_name
        ordering = ["index"]


class IndexPromotionBanner(BaseModel):
    """首页促销活动表。"""

    promotion = models.ForeignKey(
        "promotion.Promotion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="index_banners",
        verbose_name="算价规则",
        help_text="关联后活动页可展示时间、类型和实时优惠说明；未关联时仅作展示",
    )
    slug = models.SlugField(
        max_length=50,
        unique=True,
        verbose_name="活动标识",
        help_text="用于站内活动页地址，例如 new-customer",
    )
    name = models.CharField(max_length=50, verbose_name="活动名称")
    summary = models.CharField(max_length=120, blank=True, default="", verbose_name="活动简介")
    rules = models.TextField(blank=True, default="", verbose_name="活动规则")
    category = models.ForeignKey(
        GoodsType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="promotions",
        verbose_name="参与分类",
    )
    cta_text = models.CharField(max_length=20, default="立即选购", verbose_name="按钮文字")
    cta_url = models.CharField(
        max_length=500,
        blank=True,
        default="",
        verbose_name="按钮链接",
        help_text="支持站内相对路径，例如 /list/1/；留空时优先使用参与分类，否则返回首页",
    )
    url = models.URLField(
        blank=True,
        default="",
        verbose_name="外部链接（可选）",
        help_text="仅用于外部活动页；首页活动卡片默认进入站内活动页",
    )
    image = models.ImageField(upload_to="banner/", verbose_name="图片")
    index = models.SmallIntegerField(default=0, verbose_name="展示顺序")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    class Meta:
        db_table = "df_index_promotion_banner"
        verbose_name = "首页促销活动"
        verbose_name_plural = verbose_name
        ordering = ["index"]

    def get_absolute_url(self):
        return reverse("goods:promotion", args=[self.slug])


class IndexTypeGoodsBanner(BaseModel):
    """首页分类商品展示表。"""

    DISPLAY_TYPE_SKU = 0
    DISPLAY_TYPE_SPU = 1

    type = models.ForeignKey(
        GoodsType, on_delete=models.CASCADE, related_name="type_banners", verbose_name="商品种类"
    )
    sku = models.ForeignKey(
        GoodsSKU, on_delete=models.CASCADE, related_name="type_banners", verbose_name="商品SKU"
    )
    display_type = models.SmallIntegerField(
        choices=((DISPLAY_TYPE_SKU, "SKU"), (DISPLAY_TYPE_SPU, "SPU")),
        default=DISPLAY_TYPE_SKU,
        verbose_name="展示类型",
    )
    index = models.SmallIntegerField(default=0, verbose_name="展示顺序")

    class Meta:
        db_table = "df_index_type_goods_banner"
        verbose_name = "首页分类商品展示"
        verbose_name_plural = verbose_name
        ordering = ["index"]
        indexes = [models.Index(fields=["type", "display_type", "index"], name="idx_type_banner")]
