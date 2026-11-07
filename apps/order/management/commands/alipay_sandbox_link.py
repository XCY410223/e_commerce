"""生成支付宝沙盒收银台链接。

沙盒异步通知必须由支付宝服务器从公网访问，因此本命令默认拒绝 localhost、
回环地址和私网 IP，并校验当前网关确实是支付宝沙盒网关。
"""

import ipaddress
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from order import payment
from order.models import OrderInfo


SANDBOX_GATEWAY_PREFIX = "https://openapi-sandbox.dl.alipaydev.com/"


def validate_public_callback_url(label, value):
    """校验回调地址是公网 HTTP(S) 地址。"""
    parsed = urlparse(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CommandError(f"{label} 必须是完整的 http(s) 公网地址：{value!r}")
    if parsed.username or parsed.password:
        raise CommandError(f"{label} 不能包含用户名或密码")

    host = (parsed.hostname or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise CommandError(f"{label} 不能使用本机地址：{value}")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise CommandError(f"{label} 必须是可公网解析的域名或公网 IP：{value}")
        return value

    if not ip.is_global:
        raise CommandError(f"{label} 不能使用内网、回环或保留 IP：{value}")
    return value


class Command(BaseCommand):
    help = "生成指定未支付订单的支付宝沙盒支付链接"

    def add_arguments(self, parser):
        parser.add_argument("order_id", help="订单号（OrderInfo.order_id）")
        parser.add_argument(
            "--public-base",
            default="",
            help="公网站点根地址，例如 https://demo.example.com；自动拼接回调路径",
        )
        parser.add_argument("--notify-url", default="", help="公网异步通知地址")
        parser.add_argument("--return-url", default="", help="公网同步返回地址")
        parser.add_argument("--url-only", action="store_true", help="只输出支付链接，便于复制")

    def handle(self, *args, **options):
        if settings.PAYMENT_PROVIDER != "alipay":
            raise CommandError("请先在 .env 设置 PAYMENT_PROVIDER=alipay")
        if not settings.PAYMENT_ALIPAY_DEBUG:
            raise CommandError("请先在 .env 设置 PAYMENT_ALIPAY_DEBUG=1")
        if not settings.PAYMENT_ALIPAY_GATEWAY.startswith(SANDBOX_GATEWAY_PREFIX):
            raise CommandError(
                "当前不是支付宝沙盒网关；请将 PAYMENT_ALIPAY_GATEWAY 留空并设置 PAYMENT_ALIPAY_DEBUG=1"
            )

        order = OrderInfo.objects.filter(order_id=options["order_id"]).first()
        if order is None:
            raise CommandError(f"订单不存在：{options['order_id']}")
        if order.pay_status == OrderInfo.STATUS_PAID:
            raise CommandError("订单已支付，不能重复生成支付链接")
        if order.pay_status == OrderInfo.STATUS_CANCELLED:
            raise CommandError("订单已取消，不能生成支付链接")

        public_base = options["public_base"].strip().rstrip("/")
        notify_url = options["notify_url"] or (
            f"{public_base}/order/pay/notify/alipay/" if public_base else settings.PAYMENT_ALIPAY_NOTIFY_URL
        )
        return_url = options["return_url"] or (
            f"{public_base}/order/pay/return/alipay/" if public_base else settings.PAYMENT_ALIPAY_RETURN_URL
        )
        validate_public_callback_url("notify_url", notify_url)
        validate_public_callback_url("return_url", return_url)

        try:
            intent = payment.create_payment(
                order,
                notify_url=notify_url,
                return_url=return_url,
            )
        except payment.PaymentError as exc:
            raise CommandError(str(exc)) from exc

        if options["url_only"]:
            self.stdout.write(intent.url)
            return

        self.stdout.write(f"订单号：{order.order_id}")
        self.stdout.write(f"沙盒网关：{settings.PAYMENT_ALIPAY_GATEWAY}")
        self.stdout.write(f"notify_url：{notify_url}")
        self.stdout.write(f"return_url：{return_url}")
        self.stdout.write("支付链接：")
        self.stdout.write(intent.url)
