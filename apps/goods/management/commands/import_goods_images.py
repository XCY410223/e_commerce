"""把本地商品图导入默认存储（FastDFS）并绑定到对应 SKU。

文件名必须为 ``{SKU_ID}_{商品名}.jpg``，例如 ``89_烟台红富士苹果500g.jpg``。
默认只替换每个 SKU 的主图；加 ``--with-details`` 时，会用原图裁剪出两张
细节图，替换现有的细节占位图。命令可重复执行，已经位于 FastDFS 的图片
默认跳过，避免重复上传。
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from goods.models import GoodsImage, GoodsSKU

FILE_RE = re.compile(r"^(?P<sku_id>\d+)_(?P<name>.+)\.(?:jpe?g|png|webp)$", re.I)
REMOTE_RE = re.compile(r"^group[0-9]+/")
DETAIL_ZOOMS = (0.9, 0.78)


def normalize(value: str) -> str:
    """去掉空格和常见标点，避免文件名与 SKU 名称因排版差异误判。"""
    return re.sub(r"[\s\-_（）()×*·.]+", "", value or "").lower()


def is_remote(name: str) -> bool:
    """FastDFS 文件 ID 以 groupN/ 开头，本地旧路径则以 goods/ 等开头。"""
    return bool(REMOTE_RE.match(name or ""))


def make_detail(source: Image.Image, zoom: float) -> bytes:
    """从原图中心裁剪并放大到 1000×1000，作为细节图。"""
    width, height = source.size
    side = max(1, int(min(width, height) * zoom))
    left = (width - side) // 2
    top = (height - side) // 2
    cropped = source.crop((left, top, left + side, top + side)).resize(
        (1000, 1000), Image.LANCZOS
    )
    buffer = io.BytesIO()
    cropped.save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue()


class Command(BaseCommand):
    help = "把本地商品图上传到 FastDFS 并挂到对应 SKU 的主图/细节图"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir",
            default=str(Path.home() / "Pictures" / "e-commerce"),
            help="图片目录，默认 ~/Pictures/e-commerce",
        )
        parser.add_argument("--only", default="", help="只处理逗号分隔的 SKU ID")
        parser.add_argument(
            "--with-details",
            action="store_true",
            help="同时用原图裁剪生成并替换两张细节图",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="即使图片已在 FastDFS 中也重新上传",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只校验文件和 SKU，不上传也不写数据库",
        )

    def handle(self, *args, **options):
        folder = Path(options["dir"]).expanduser()
        if not folder.is_dir():
            raise CommandError(f"图片目录不存在：{folder}")

        only = {
            int(item)
            for item in options["only"].split(",")
            if item.strip().isdigit()
        }
        entries = []
        for path in sorted(folder.iterdir()):
            match = FILE_RE.match(path.name)
            if not match or path.name.startswith("00_"):
                continue
            sku_id = int(match.group("sku_id"))
            if only and sku_id not in only:
                continue
            entries.append((sku_id, match.group("name"), path))
        if not entries:
            raise CommandError("没有找到符合 {SKU_ID}_{商品名}.jpg 规则的图片")

        stats = {"sku": 0, "uploaded": 0, "skipped": 0, "failed": 0}
        failures = []
        for sku_id, name, path in entries:
            sku = GoodsSKU.all_objects.filter(pk=sku_id).first()
            if sku is None:
                stats["failed"] += 1
                failures.append(f"{path.name}：SKU {sku_id} 不存在")
                continue
            if normalize(name) != normalize(sku.name):
                stats["failed"] += 1
                failures.append(f"{path.name}：文件名与 SKU 名称不一致（{sku.name}）")
                continue
            try:
                with Image.open(path) as source:
                    source.load()
                    if source.mode != "RGB":
                        source = source.convert("RGB")
                    main_bytes = path.read_bytes()
                    detail_bytes = [make_detail(source, zoom) for zoom in DETAIL_ZOOMS]
            except Exception as exc:
                stats["failed"] += 1
                failures.append(f"{path.name}：图片无法读取（{exc}）")
                continue

            stats["sku"] += 1
            try:
                uploaded, skipped = self._import_sku(
                    sku,
                    main_bytes,
                    detail_bytes,
                    with_details=options["with_details"],
                    overwrite=options["overwrite"],
                    dry_run=options["dry_run"],
                )
            except Exception as exc:
                stats["failed"] += 1
                failures.append(f"{path.name}：{exc}")
                self.stderr.write(self.style.ERROR(f"[FAIL] {path.name}：{exc}"))
                continue
            stats["uploaded"] += uploaded
            stats["skipped"] += skipped
            if stats["sku"] % 10 == 0:
                self.stdout.write(f"已处理 {stats['sku']}/{len(entries)} 个 SKU")

        prefix = "预检" if options["dry_run"] else "导入"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}完成：SKU {stats['sku']} 个，上传 {stats['uploaded']} 张，"
                f"跳过 {stats['skipped']} 张，失败 {stats['failed']} 个"
            )
        )
        for failure in failures:
            self.stdout.write(self.style.WARNING(f"  - {failure}"))

    def _import_sku(
        self,
        sku,
        main_bytes,
        detail_bytes,
        *,
        with_details,
        overwrite,
        dry_run,
    ):
        uploaded = skipped = 0
        with transaction.atomic():
            # 与后台保存图片保持相同的锁顺序：先锁 SKU，再改图片行。
            GoodsSKU.objects.select_for_update().filter(pk=sku.pk).first()

            main = (
                GoodsImage.objects.filter(sku=sku, is_main=True)
                .order_by("pk")
                .first()
            )
            if main is None:
                main = GoodsImage.objects.filter(sku=sku).order_by("pk").first()
                if main is None:
                    main = GoodsImage(sku=sku, is_main=True)
                else:
                    main.is_main = True

            if is_remote(main.image.name) and not overwrite:
                skipped += 1
            else:
                uploaded += 1
                if not dry_run:
                    main.image.save(
                        f"{sku.pk}_main.jpg", ContentFile(main_bytes), save=False
                    )
                    main.save()

            if not with_details:
                return uploaded, skipped

            details = list(
                GoodsImage.objects.filter(sku=sku, is_main=False)
                .order_by("pk")[: len(detail_bytes)]
            )
            for index, data in enumerate(detail_bytes, start=1):
                if index <= len(details):
                    row = details[index - 1]
                else:
                    row = GoodsImage(sku=sku, is_main=False)
                if is_remote(row.image.name) and not overwrite:
                    skipped += 1
                    continue
                uploaded += 1
                if not dry_run:
                    row.image.save(
                        f"{sku.pk}_detail{index}.jpg", ContentFile(data), save=False
                    )
                    row.save()
        return uploaded, skipped
