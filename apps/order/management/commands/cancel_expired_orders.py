"""手动处理超时未支付订单。"""

from django.core.management.base import BaseCommand

from order.services import cancel_expired_orders


class Command(BaseCommand):
    help = "取消超时未支付订单并回补库存"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100, help="本次最多处理多少单")
        parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help="超时秒数；默认读取 ORDER_UNPAID_TIMEOUT_SECONDS",
        )
        parser.add_argument("--dry-run", action="store_true", help="只统计候选订单，不修改数据")

    def handle(self, *args, **options):
        result = cancel_expired_orders(
            limit=options["limit"],
            timeout_seconds=options["timeout"],
            dry_run=options["dry_run"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                "候选 {checked} 单，取消 {cancelled} 单，已支付 {paid} 单，"
                "已取消 {already_cancelled} 单，失败 {failed} 单".format(**result)
            )
        )
