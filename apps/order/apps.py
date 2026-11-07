from django.apps import AppConfig
from django.core.checks import Error, register


@register()
def check_payment_provider(app_configs, **kwargs):
    """生产环境禁止使用 mock 支付渠道，避免误配后订单可被自提为已支付。"""
    from django.conf import settings

    provider = (settings.PAYMENT_PROVIDER or "mock").strip().lower()
    if provider == "mock" and not settings.DEBUG:
        return [
            Error(
                "生产环境（DEBUG=False）仍配置 mock 支付渠道",
                hint="设置 PAYMENT_PROVIDER=alipay；mock 收银台仅供开发环境验收使用",
                id="order.E001",
            )
        ]
    return []


class OrderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "order"
    verbose_name = "订单模块"
