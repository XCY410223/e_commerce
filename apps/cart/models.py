"""购物车相关的持久化补偿记录。"""

from django.db import models


class CartCleanupTask(models.Model):
    """Redis 清理失败后的数据库待办，保证订单成功不受缓存故障影响。"""

    order_id = models.CharField(max_length=64, unique=True, verbose_name="订单号")
    user_id = models.PositiveBigIntegerField(db_index=True, verbose_name="用户ID")
    items = models.JSONField(default=dict, verbose_name="已购买数量快照")
    attempts = models.PositiveIntegerField(default=0, verbose_name="重试次数")
    last_error = models.TextField(blank=True, default="", verbose_name="最近错误")
    create_time = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    update_time = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "df_cart_cleanup_task"
        verbose_name = "购物车清理任务"
        verbose_name_plural = verbose_name
        ordering = ["create_time"]
        indexes = [
            models.Index(fields=["user_id", "create_time"], name="idx_cart_cleanup_user"),
        ]

    def __str__(self):
        return f"{self.order_id} (user={self.user_id})"
