"""促销规则变化后的页面缓存失效。"""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from db.base_model import soft_delete_completed

from .models import Promotion, PromotionScope


def _invalidate(*args, **kwargs):
    from promotion.cache import bump_pricing_version
    from goods.static_pages import (
        invalidate_static_pages,
        schedule_static_pages_refresh,
    )

    transaction.on_commit(bump_pricing_version)
    transaction.on_commit(invalidate_static_pages)
    transaction.on_commit(schedule_static_pages_refresh)


for _model in (Promotion, PromotionScope):
    post_save.connect(
        _invalidate,
        sender=_model,
        dispatch_uid=f"promotion-cache-save-{_model._meta.label_lower}",
    )
    post_delete.connect(
        _invalidate,
        sender=_model,
        dispatch_uid=f"promotion-cache-delete-{_model._meta.label_lower}",
    )


@receiver(soft_delete_completed, dispatch_uid="promotion-cache-soft-delete")
def _invalidate_soft_delete(sender, models=None, **kwargs):
    if models and any(label.startswith("promotion.") for label in models):
        _invalidate()
