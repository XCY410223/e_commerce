from django.apps import AppConfig


class GoodsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "goods"
    verbose_name = "商品模块"

    def ready(self):
        from . import signals  # noqa: F401
