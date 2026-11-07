from django.contrib import admin

from .models import CartCleanupTask


@admin.register(CartCleanupTask)
class CartCleanupTaskAdmin(admin.ModelAdmin):
    list_display = ("order_id", "user_id", "attempts", "create_time", "update_time")
    search_fields = ("order_id", "user_id")
    readonly_fields = ("create_time", "update_time")
