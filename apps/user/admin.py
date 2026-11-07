from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Address, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "is_active", "is_staff", "create_time")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("username", "email")
    ordering = ("-create_time",)


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("receiver", "phone", "addr", "is_default", "user")
    list_filter = ("is_default",)
    search_fields = ("receiver", "phone", "addr")
