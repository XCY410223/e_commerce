from django.contrib import admin

from .models import OrderPromotion, Promotion, PromotionScope, Referral, UserCoupon


class PromotionScopeInline(admin.TabularInline):
    model = PromotionScope
    extra = 0
    fields = ("include", "category", "sku")
    autocomplete_fields = ("category", "sku")


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "kind",
        "is_active",
        "start_at",
        "end_at",
        "priority",
        "discount_amount",
        "discount_rate",
        "shipping_threshold",
    )
    list_filter = ("kind", "is_active", "stackable")
    search_fields = ("name", "slug", "description")
    list_editable = ("is_active", "priority")
    readonly_fields = ("create_time", "update_time")
    inlines = [PromotionScopeInline]
    actions = ("enable_promotions", "disable_promotions")
    fieldsets = (
        (
            "基础信息",
            {
                "fields": (
                    "slug",
                    "name",
                    "kind",
                    "is_active",
                    "start_at",
                    "end_at",
                    "priority",
                    "description",
                )
            },
        ),
        (
            "优惠规则",
            {
                "fields": (
                    "stackable",
                    "min_amount",
                    "discount_amount",
                    "discount_rate",
                    "max_discount",
                    "shipping_threshold",
                    "limit_per_user",
                    "coupon_valid_days",
                )
            },
        ),
        ("审计", {"fields": ("create_time", "update_time")}),
    )

    @admin.action(description="启用所选活动")
    def enable_promotions(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description="停用所选活动（新订单立即停止命中）")
    def disable_promotions(self, request, queryset):
        queryset.update(is_active=False)


@admin.register(UserCoupon)
class UserCouponAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "user",
        "title",
        "discount_amount",
        "min_amount",
        "status",
        "expires_at",
        "used_order",
        "create_time",
    )
    list_filter = ("status", "promotion")
    search_fields = ("code", "user__username", "title", "grant_key")
    readonly_fields = (
        "code",
        "grant_key",
        "user",
        "promotion",
        "title",
        "discount_amount",
        "min_amount",
        "status",
        "expires_at",
        "used_order",
        "used_at",
        "create_time",
        "update_time",
    )


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = (
        "inviter",
        "invitee",
        "status",
        "reward_coupon",
        "completed_at",
        "create_time",
    )
    list_filter = ("status",)
    search_fields = ("inviter__username", "invitee__username", "code")
    readonly_fields = (
        "inviter",
        "invitee",
        "code",
        "reward_coupon",
        "completed_at",
        "create_time",
        "update_time",
    )


@admin.register(OrderPromotion)
class OrderPromotionAdmin(admin.ModelAdmin):
    list_display = ("order", "snapshot_name", "kind", "discount_amount", "create_time")
    list_filter = ("kind",)
    search_fields = ("order__order_id", "snapshot_name")
    readonly_fields = (
        "order",
        "promotion",
        "kind",
        "snapshot_name",
        "discount_amount",
        "metadata",
        "create_time",
        "update_time",
    )
