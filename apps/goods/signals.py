"""商品数据变化后的缓存失效信号。"""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from db.base_model import soft_delete_completed

from .cache import invalidate_goods_nav_cache, invalidate_goods_page_cache
from .models import (
    GoodsImage,
    GoodsSKU,
    GoodsType,
    IndexGoodsBanner,
    IndexPromotionBanner,
    IndexTypeGoodsBanner,
)

_CACHE_MODELS = (
    GoodsType,
    GoodsSKU,
    GoodsImage,
    IndexGoodsBanner,
    IndexPromotionBanner,
    IndexTypeGoodsBanner,
)


def _cache_write(sender, using=None, **kwargs):
    transaction.on_commit(invalidate_goods_page_cache, using=using)
    if sender is GoodsType:
        transaction.on_commit(invalidate_goods_nav_cache, using=using)


for _model in _CACHE_MODELS:
    post_save.connect(
        _cache_write,
        sender=_model,
        dispatch_uid=f"goods-cache-save-{_model._meta.label_lower}",
    )
    post_delete.connect(
        _cache_write,
        sender=_model,
        dispatch_uid=f"goods-cache-delete-{_model._meta.label_lower}",
    )


@receiver(soft_delete_completed, dispatch_uid="goods-cache-soft-delete")
def _cache_soft_delete(sender, models=None, using=None, **kwargs):
    transaction.on_commit(invalidate_goods_page_cache, using=using)
    if models and "goods.GoodsType" in models:
        transaction.on_commit(invalidate_goods_nav_cache, using=using)
