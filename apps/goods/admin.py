from django.contrib import admin
from django.db import models
from tinymce.widgets import TinyMCE

from .models import (
    GoodsImage,
    GoodsSKU,
    GoodsSPU,
    GoodsType,
    IndexGoodsBanner,
    IndexPromotionBanner,
    IndexTypeGoodsBanner,
)


class GoodsImageInline(admin.TabularInline):
    model = GoodsImage
    extra = 1
    fields = ("image", "is_main")


@admin.register(GoodsType)
class GoodsTypeAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "logo")
    search_fields = ("name",)


@admin.register(GoodsSPU)
class GoodsSPUAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)
    formfield_overrides = {models.TextField: {"widget": TinyMCE()}}


@admin.register(GoodsSKU)
class GoodsSKUAdmin(admin.ModelAdmin):
    list_display = (
        "id", "name", "spec", "spu", "type", "price", "stock", "sales", "status"
    )
    list_filter = ("status", "type", "spu")
    search_fields = ("name", "spec", "desc", "spu__name")
    list_editable = ("spec", "price", "stock", "status")
    inlines = [GoodsImageInline]


@admin.register(IndexGoodsBanner)
class IndexGoodsBannerAdmin(admin.ModelAdmin):
    list_display = ("id", "sku", "index")
    list_editable = ("index",)


@admin.register(IndexPromotionBanner)
class IndexPromotionBannerAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "promotion", "category", "index", "is_active")
    list_filter = ("is_active", "category")
    list_editable = ("index", "is_active")
    search_fields = ("name", "slug", "summary")
    autocomplete_fields = ("promotion",)


@admin.register(IndexTypeGoodsBanner)
class IndexTypeGoodsBannerAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "sku", "display_type", "index")
    list_filter = ("type", "display_type")
    list_editable = ("index",)
