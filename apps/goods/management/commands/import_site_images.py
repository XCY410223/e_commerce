"""把首页轮播、促销卡片和品类图标导入默认存储（FastDFS）。

生成的文件名约定：
    banner_{index}_{sku_id}_{描述}.jpg  -> IndexGoodsBanner(index, sku_id)
    promo_{index}_{描述}.jpg            -> IndexPromotionBanner(index)
    logo_{品类名称}.jpg                 -> GoodsType(name)

命令可重复执行，已在 FastDFS 的图片默认跳过；数据库更新后会由商品信号
自动清理首页和分类页缓存。
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from goods.models import GoodsType, IndexGoodsBanner, IndexPromotionBanner

BANNER_RE = re.compile(r"^banner_(?P<index>\d+)_(?P<sku_id>\d+)_(?P<label>.+)\.(?:jpe?g|png|webp)$", re.I)
PROMO_RE = re.compile(r"^promo_(?P<index>\d+)_(?P<label>.+)\.(?:jpe?g|png|webp)$", re.I)
LOGO_RE = re.compile(r"^logo_(?P<name>.+)\.(?:jpe?g|png|webp)$", re.I)
REMOTE_RE = re.compile(r"^group[0-9]+/")


def is_remote(name: str) -> bool:
    return bool(REMOTE_RE.match(name or ""))


class Command(BaseCommand):
    help = "把首页轮播、促销图和品类图上传到 FastDFS 并写入数据库"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir",
            default=str(Path.home() / "Pictures" / "e-commerce"),
            help="图片目录，默认 ~/Pictures/e-commerce",
        )
        parser.add_argument(
            "--kind",
            choices=("all", "banner", "promo", "logo"),
            default="all",
            help="只处理某一类图片",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="即使图片已在 FastDFS 中也重新上传",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只校验文件和数据库记录，不上传也不写库",
        )

    def handle(self, *args, **options):
        folder = Path(options["dir"]).expanduser()
        if not folder.is_dir():
            raise CommandError(f"图片目录不存在：{folder}")
        kind = options["kind"]

        jobs = []
        for path in sorted(folder.iterdir()):
            if kind in {"all", "banner"}:
                match = BANNER_RE.match(path.name)
                if match:
                    jobs.append(
                        (
                            "banner",
                            path,
                            IndexGoodsBanner,
                            {
                                "index": int(match.group("index")),
                                "sku_id": int(match.group("sku_id")),
                            },
                            f"banner_{match.group('index')}_{match.group('sku_id')}",
                        )
                    )
                    continue
            if kind in {"all", "promo"}:
                match = PROMO_RE.match(path.name)
                if match:
                    jobs.append(
                        (
                            "promo",
                            path,
                            IndexPromotionBanner,
                            {"index": int(match.group("index"))},
                            f"promo_{match.group('index')}",
                        )
                    )
                    continue
            if kind in {"all", "logo"}:
                match = LOGO_RE.match(path.name)
                if match:
                    jobs.append(
                        (
                            "logo",
                            path,
                            GoodsType,
                            {"name": match.group("name")},
                            f"logo_{match.group('name')}",
                        )
                    )

        if not jobs:
            raise CommandError("没有找到 banner_/promo_/logo_ 图片")

        stats = {"done": 0, "uploaded": 0, "skipped": 0, "failed": 0}
        for kind_name, path, model, lookup, stem in jobs:
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception as exc:
                stats["failed"] += 1
                self.stderr.write(self.style.ERROR(f"[FAIL] {path.name}：图片无法读取（{exc}）"))
                continue

            try:
                target = model.all_objects.filter(**lookup).first()
                if target is None:
                    stats["failed"] += 1
                    self.stderr.write(
                        self.style.ERROR(f"[FAIL] {path.name}：找不到对应记录 {lookup}")
                    )
                    continue

                if is_remote(target.image.name) and not options["overwrite"]:
                    stats["skipped"] += 1
                    self.stdout.write(f"[SKIP] {path.name} -> {target.image.name}")
                    continue
                stats["uploaded"] += 1
                if not options["dry_run"]:
                    with transaction.atomic():
                        locked = model.objects.select_for_update().filter(pk=target.pk).first()
                        if locked is None:
                            raise RuntimeError("记录在导入过程中被删除")
                        locked.image.save(
                            f"{stem}.jpg", ContentFile(path.read_bytes()), save=False
                        )
                        locked.save(update_fields=["image"])
                stats["done"] += 1
                self.stdout.write(f"[OK] {path.name} -> {kind_name}")
            except Exception as exc:
                stats["failed"] += 1
                self.stderr.write(self.style.ERROR(f"[FAIL] {path.name}：{exc}"))

        prefix = "预检" if options["dry_run"] else "导入"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}完成：处理 {stats['done']} 张，上传 {stats['uploaded']} 张，"
                f"跳过 {stats['skipped']} 张，失败 {stats['failed']} 张"
            )
        )
