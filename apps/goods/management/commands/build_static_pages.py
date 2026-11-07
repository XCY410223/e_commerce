"""手工生成首页和分类列表静态页。"""

from django.core.management.base import BaseCommand

from goods.static_pages import build_static_pages


class Command(BaseCommand):
    help = "生成首页与全部分类/排序/分页静态页"

    def handle(self, *args, **options):
        result = build_static_pages()
        if not result["published"]:
            self.stderr.write(
                f"静态页未发布：{result.get('reason', 'unknown')}"
            )
            return
        skipped = result.get("skipped_pages", 0)
        detail = f"，超出页数上限跳过 {skipped} 页" if skipped else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"已生成 {result['pages']} 个静态页{detail}，"
                f"耗时 {result['duration_ms']} ms：{result['root']}"
            )
        )
