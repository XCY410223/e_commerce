"""重试因 Redis 故障而滞留的购物车清理任务。"""

from django.core.management.base import BaseCommand

from utils.cart import retry_pending_cart_cleanups


class Command(BaseCommand):
    help = "重试数据库中待处理的购物车清理任务"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="本次最多处理的任务数，默认 100",
        )

    def handle(self, *args, **options):
        processed = retry_pending_cart_cleanups(limit=options["limit"])
        self.stdout.write(self.style.SUCCESS(f"本次成功处理 {processed} 条购物车清理任务"))
