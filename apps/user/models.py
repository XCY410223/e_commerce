"""用户模块模型。

对应《数据库设计》中的「用户表」「地址表」。
"""

from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.db import models

from db.base_model import AllObjectsManager, BaseModel, SoftDeleteManager, SoftDeleteQuerySet


class AddressQuerySet(SoftDeleteQuerySet):
    """收货地址的常用查询集合。"""

    def for_user(self, user):
        """只返回指定用户的收货地址。"""
        return self.filter(user=user)


class AddressManager(SoftDeleteManager.from_queryset(AddressQuerySet)):
    """地址管理器：提供返回单条默认地址的方法。"""

    def default_for(self, user):
        """返回默认地址；没有默认地址时返回最近添加的一条。"""
        return self.for_user(user).order_by("-is_default", "-create_time").first()


class UserQuerySet(SoftDeleteQuerySet):
    pass


class UserManager(DjangoUserManager.from_queryset(UserQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(is_delete=False)


class User(AbstractUser, BaseModel):
    """用户表。

    设计文档中的字段映射：
        用户名 -> username
        密码   -> password（AbstractUser 已做哈希）
        邮箱   -> email
        激活标识 -> is_active
        权限标识 -> is_superuser / is_staff
    """

    objects = UserManager()
    all_objects = AllObjectsManager()

    # 邮箱参与找回密码等账号流程，必须在数据库层唯一。MySQL 当前
    # utf8mb4_unicode_ci 排序规则会同时拦截大小写不同的重复邮箱。
    email = models.EmailField(max_length=254, unique=True, verbose_name="邮箱")

    class Meta:
        db_table = "df_user"
        verbose_name = "用户"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.username


class Address(BaseModel):
    """地址表。"""

    objects = AddressManager()
    all_objects = AllObjectsManager()

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="addresses", verbose_name="所属用户"
    )
    receiver = models.CharField(max_length=20, verbose_name="收件人")
    addr = models.CharField(max_length=256, verbose_name="收件地址")
    zip_code = models.CharField(max_length=6, blank=True, default="", verbose_name="邮编")
    phone = models.CharField(max_length=11, verbose_name="联系方式")
    is_default = models.BooleanField(default=False, verbose_name="是否默认")

    class Meta:
        db_table = "df_address"
        verbose_name = "收货地址"
        verbose_name_plural = verbose_name
        ordering = ["-is_default", "-create_time"]

    def __str__(self):
        return f"{self.receiver} - {self.addr}"
