"""重新生成演示数据中的占位图。

用于修复早期在无中文字体环境下生成的商品图、轮播图和分类图。
默认会覆盖现有演示图片并重建静态页；执行前请确认这些图片可以替换。
"""

from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from goods.models import (
    GoodsImage,
    GoodsSKU,
    GoodsType,
    IndexGoodsBanner,
    IndexPromotionBanner,
)
from scripts.seed import make_image


class Command(BaseCommand):
    help = "重新生成演示占位图并上传到当前存储后端，然后重建静态页"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只统计将更新的图片，不写数据库和存储",
        )
        parser.add_argument(
            "--keep-old",
            action="store_true",
            help="保留旧图片文件，默认删除已替换的旧文件",
        )
        parser.add_argument(
            "--skip-static",
            action="store_true",
            help="更新图片后不重建静态页",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.keep_old = options["keep_old"]
        counts = {
            "types": 0,
            "goods": 0,
            "banners": 0,
            "promotions": 0,
        }

        with transaction.atomic():
            for seed, goods_type in enumerate(GoodsType.objects.order_by("pk")):
                self._replace(
                    goods_type,
                    "image",
                    goods_type.name,
                    (300, 300),
                    seed,
                )
                counts["types"] += 1

            for sku in GoodsSKU.objects.prefetch_related("images").order_by("pk"):
                for index, image in enumerate(sku.images.all()):
                    if image.is_main:
                        label = sku.name
                        size = (600, 600)
                    else:
                        label = f"{sku.name}\n细节图 {index}"
                        size = (400, 400)
                    self._replace(image, "image", label, size, index)
                    counts["goods"] += 1

            for seed, banner in enumerate(
                IndexGoodsBanner.objects.select_related("sku").order_by("index", "pk")
            ):
                self._replace(
                    banner,
                    "image",
                    f"轮播\n{banner.sku.name}",
                    (900, 320),
                    seed,
                )
                counts["banners"] += 1

            for seed, promotion in enumerate(
                IndexPromotionBanner.objects.order_by("index", "pk")
            ):
                self._replace(
                    promotion,
                    "image",
                    promotion.name,
                    (400, 160),
                    seed,
                )
                counts["promotions"] += 1

        if self.dry_run:
            self.stdout.write(
                "dry-run：将更新 "
                f"分类图 {counts['types']}、商品图 {counts['goods']}、"
                f"轮播图 {counts['banners']}、促销图 {counts['promotions']}"
            )
            return

        if not options["skip_static"]:
            from goods.static_pages import build_static_pages

            result = build_static_pages()
            self.stdout.write(f"静态页重建：{result}")

        self.stdout.write(
            self.style.SUCCESS(
                "演示图片已更新："
                f"分类图 {counts['types']}、商品图 {counts['goods']}、"
                f"轮播图 {counts['banners']}、促销图 {counts['promotions']}"
            )
        )

    def _replace(self, instance, field_name, label, size, seed):
        field = getattr(instance, field_name)
        old_name = field.name
        if self.dry_run:
            return

        data = make_image(label, size=size, seed=seed)
        field.save(f"placeholder_{seed}.jpg", ContentFile(data), save=False)
        instance.save()

        if old_name and not self.keep_old and old_name != field.name:
            try:
                field.storage.delete(old_name)
            except Exception as exc:  # 删除旧文件失败不应阻断图片替换
                self.stderr.write(
                    self.style.WARNING(f"旧图片删除失败（已忽略）：{old_name}：{exc}")
                )
