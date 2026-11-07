"""为已有项目补齐首期活动规则，并把首页活动卡片关联到算价规则。"""

from django.core.management.base import BaseCommand
from django.db import transaction

from goods.models import GoodsType, IndexPromotionBanner
from promotion.cache import bump_pricing_version
from promotion.defaults import default_promotion_rules
from promotion.models import Promotion, PromotionScope


class Command(BaseCommand):
    help = "创建缺失的首期促销规则并关联首页活动卡片（可重复执行）"

    @transaction.atomic
    def handle(self, *args, **options):
        promotions = {}
        for data in default_promotion_rules():
            slug = data["slug"]
            promotion, created = Promotion.objects.get_or_create(
                slug=slug, defaults=data
            )
            promotions[slug] = promotion
            self.stdout.write(
                f"{'创建' if created else '保留'}活动规则 {promotion.name} ({slug})"
            )

        seafood = GoodsType.objects.filter(name="海鲜水产").first()
        if seafood and promotions.get("weekend-sale"):
            PromotionScope.objects.get_or_create(
                promotion=promotions["weekend-sale"],
                category=seafood,
                sku=None,
                include=True,
            )

        for banner in IndexPromotionBanner.objects.all():
            promotion = promotions.get(banner.slug)
            if promotion and banner.promotion_id != promotion.pk:
                banner.promotion = promotion
                banner.save(update_fields=["promotion", "update_time"])

        self.stdout.write(self.style.SUCCESS("促销规则初始化完成"))
        bump_pricing_version()
