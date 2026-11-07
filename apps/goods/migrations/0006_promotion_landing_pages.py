from django.db import migrations, models


PROMOTIONS = {
    "新客立减 20 元": {
        "slug": "new-customer",
        "summary": "新用户注册后即可参与新人优惠活动，轻松开启第一单。",
        "rules": (
            "1. 活动仅限首次注册的新用户参与。\n"
            "2. 每个账号限参与一次，不可与其他新人活动叠加。\n"
            "3. 具体优惠以下单结算页展示为准。"
        ),
        "category": None,
        "cta_text": "立即注册",
        "cta_url": "/user/register/",
    },
    "生鲜满 99 免运费": {
        "slug": "free-shipping",
        "summary": "精选生鲜单笔满 99 元，享受基础配送费减免。",
        "rules": (
            "1. 单笔订单实付满 99 元可参与免运费活动。\n"
            "2. 偏远地区、大件商品及特殊配送区域不参与。\n"
            "3. 具体运费以结算页展示为准。"
        ),
        "category": "新鲜水果",
        "cta_text": "去逛新鲜水果",
        "cta_url": "",
    },
    "周末限时 5 折": {
        "slug": "weekend-sale",
        "summary": "周末限定好物，参与商品低至 5 折。",
        "rules": (
            "1. 活动仅限周末期间参与，过期自动结束。\n"
            "2. 折扣商品数量有限，售完即止。\n"
            "3. 具体价格以商品详情页和结算页展示为准。"
        ),
        "category": "海鲜水产",
        "cta_text": "查看活动商品",
        "cta_url": "",
    },
    "邀请好友得优惠券": {
        "slug": "invite-friends",
        "summary": "邀请好友注册天天生鲜，一起分享新鲜好生活。",
        "rules": (
            "1. 邀请好友注册并完成首单后，可参与邀请奖励活动。\n"
            "2. 邀请奖励以活动通知和会员中心展示为准。\n"
            "3. 同一好友仅计入一次邀请记录。"
        ),
        "category": None,
        "cta_text": "进入会员中心",
        "cta_url": "/user/center/",
    },
}


def populate_promotions(apps, schema_editor):
    Promotion = apps.get_model("goods", "IndexPromotionBanner")
    GoodsType = apps.get_model("goods", "GoodsType")
    types_by_name = {item.name: item for item in GoodsType.objects.all()}
    used_slugs = set()

    for promotion in Promotion.objects.all().order_by("pk"):
        content = PROMOTIONS.get(promotion.name)
        if content:
            slug = content["slug"]
            if slug in used_slugs:
                slug = f"{slug}-{promotion.pk}"
            promotion.slug = slug
            promotion.summary = content["summary"]
            promotion.rules = content["rules"]
            promotion.category = types_by_name.get(content["category"])
            promotion.cta_text = content["cta_text"]
            promotion.cta_url = content["cta_url"]
        else:
            slug = f"promo-{promotion.pk}"
            while slug in used_slugs:
                slug = f"promo-{promotion.pk}-{len(used_slugs)}"
            promotion.slug = slug
            promotion.summary = promotion.summary or ""
            promotion.rules = promotion.rules or ""
            promotion.cta_text = promotion.cta_text or "立即选购"
            promotion.cta_url = promotion.cta_url or ""
            promotion.category = promotion.category
        promotion.url = ""
        promotion.is_active = True
        promotion.save(
            update_fields=[
                "slug",
                "summary",
                "rules",
                "category",
                "cta_text",
                "cta_url",
                "url",
                "is_active",
            ]
        )
        used_slugs.add(promotion.slug)


class Migration(migrations.Migration):
    dependencies = [
        ("goods", "0005_fulltext_add_spec"),
    ]

    operations = [
        migrations.AddField(
            model_name="indexpromotionbanner",
            name="slug",
            field=models.SlugField(
                blank=True,
                help_text="用于站内活动页地址，例如 new-customer",
                max_length=50,
                null=True,
                verbose_name="活动标识",
            ),
        ),
        migrations.AddField(
            model_name="indexpromotionbanner",
            name="summary",
            field=models.CharField(
                blank=True,
                default="",
                max_length=120,
                verbose_name="活动简介",
            ),
        ),
        migrations.AddField(
            model_name="indexpromotionbanner",
            name="rules",
            field=models.TextField(blank=True, default="", verbose_name="活动规则"),
        ),
        migrations.AddField(
            model_name="indexpromotionbanner",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="promotions",
                to="goods.goodstype",
                verbose_name="参与分类",
            ),
        ),
        migrations.AddField(
            model_name="indexpromotionbanner",
            name="cta_text",
            field=models.CharField(
                default="立即选购",
                max_length=20,
                verbose_name="按钮文字",
            ),
        ),
        migrations.AddField(
            model_name="indexpromotionbanner",
            name="cta_url",
            field=models.CharField(
                blank=True,
                default="",
                help_text="支持站内相对路径，例如 /list/1/；留空时优先使用参与分类，否则返回首页",
                max_length=500,
                verbose_name="按钮链接",
            ),
        ),
        migrations.AddField(
            model_name="indexpromotionbanner",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="是否启用"),
        ),
        migrations.AlterField(
            model_name="indexpromotionbanner",
            name="url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="仅用于外部活动页；首页活动卡片默认进入站内活动页",
                verbose_name="外部链接（可选）",
            ),
        ),
        migrations.RunPython(populate_promotions, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="indexpromotionbanner",
            name="slug",
            field=models.SlugField(
                help_text="用于站内活动页地址，例如 new-customer",
                max_length=50,
                unique=True,
                verbose_name="活动标识",
            ),
        ),
    ]
