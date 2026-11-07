from django.apps import AppConfig


class PromotionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "promotion"
    verbose_name = "促销活动模块"

    def ready(self):
        from . import signals  # noqa: F401
