from django.contrib import admin, messages

from .models import OrderComment, OrderGoods, OrderInfo
from .services import CommentError, audit_order_comment


class OrderGoodsInline(admin.TabularInline):
    model = OrderGoods
    extra = 0
    readonly_fields = (
        "sku",
        "sku_name",
        "count",
        "original_price",
        "price",
        "discount_amount",
        "promotion_name",
    )


@admin.register(OrderInfo)
class OrderInfoAdmin(admin.ModelAdmin):
    list_display = (
        "order_id",
        "user",
        "total_count",
        "total_amount",
        "item_discount",
        "coupon_discount",
        "shipping_discount",
        "total_discount",
        "transit_price",
        "pay_status",
        "trade_no",
        "paid_amount",
        "pay_time",
        "cancel_time",
        "create_time",
    )
    list_filter = ("pay_status", "pay_method")
    search_fields = ("order_id", "user__username")
    readonly_fields = (
        "order_id",
        "user",
        "addr",
        "total_count",
        "total_amount",
        "original_total_amount",
        "item_discount",
        "coupon_discount",
        "shipping_discount",
        "total_discount",
        "transit_price",
        "create_time",
        "update_time",
    )
    inlines = [OrderGoodsInline]


@admin.register(OrderComment)
class OrderCommentAdmin(admin.ModelAdmin):
    """评论审核：通过后才会出现在商品详情页。"""

    list_display = (
        "id",
        "order_goods",
        "content_preview",
        "status_label",
        "audited_by",
        "audited_at",
        "create_time",
    )
    list_filter = ("status", "audited_at", "create_time")
    search_fields = (
        "content",
        "order_goods__order__order_id",
        "order_goods__sku_name",
        "order_goods__order__user__username",
    )
    list_select_related = ("order_goods__order__user",)
    readonly_fields = (
        "order_goods",
        "content",
        "status",
        "audited_by",
        "audited_at",
        "create_time",
        "update_time",
    )
    fields = (
        "order_goods",
        "content",
        "status",
        "audit_remark",
        "audited_by",
        "audited_at",
        "create_time",
        "update_time",
    )
    actions = ("approve_comments", "reject_comments")

    @admin.display(description="评论内容")
    def content_preview(self, obj):
        content = obj.content or ""
        return f"{content[:40]}..." if len(content) > 40 else content

    @admin.display(description="审核状态", ordering="status")
    def status_label(self, obj):
        return obj.get_status_display()

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="通过选中评论")
    def approve_comments(self, request, queryset):
        self._audit_comments(request, queryset, approved=True)

    @admin.action(description="驳回选中评论")
    def reject_comments(self, request, queryset):
        self._audit_comments(request, queryset, approved=False)

    def _audit_comments(self, request, queryset, *, approved):
        audited = 0
        failed = 0
        for comment in queryset.select_related("order_goods__order"):
            remark = comment.audit_remark
            if not approved and not remark:
                remark = "不符合评论规范"
            try:
                audit_order_comment(
                    comment,
                    auditor=request.user,
                    approved=approved,
                    remark=remark,
                )
            except (CommentError, PermissionError):
                failed += 1
            else:
                audited += 1

        if audited:
            self.message_user(
                request,
                f"已审核 {audited} 条评论",
                level=messages.SUCCESS,
            )
        if failed:
            self.message_user(
                request,
                f"{failed} 条评论审核失败，请检查审核备注长度",
                level=messages.ERROR,
            )
