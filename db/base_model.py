from django.db import models, router, transaction
from django.db.models.deletion import ProtectedError
from django.dispatch import Signal
from django.utils import timezone

soft_delete_completed = Signal()


class SoftDeleteQuerySet(models.QuerySet):
    """默认排除软删除记录，并提供软删除、恢复和物理删除能力。"""

    def alive(self):
        return self.filter(is_delete=False)

    def deleted(self):
        return self.filter(is_delete=True)

    def soft_delete(self):
        collector = _SoftDeleteCollector(using=self.db)
        collector.collect(self)
        return collector.delete()

    def restore(self):
        return self.update(is_delete=False, update_time=timezone.now())

    def hard_delete(self):
        return super().delete()

    def delete(self):
        return self.soft_delete()


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """业务默认 Manager：查询时自动过滤 is_delete=True 的记录。"""

    def get_queryset(self):
        return super().get_queryset().filter(is_delete=False)


class AllObjectsManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """包含软删除记录的 Manager，供后台恢复和运维查询使用。"""


class _SoftDeleteCollector:
    """按 CASCADE / PROTECT 规则收集并执行软删除。"""

    def __init__(self, using):
        self.using = using
        self.data = {}

    def collect(self, queryset):
        self._collect_model(
            queryset.model,
            set(queryset.values_list("pk", flat=True)),
        )

    def _collect_model(self, model, pks):
        if not pks or not hasattr(model, "all_objects"):
            return

        pending = set(pks) - self.data.setdefault(model, set())
        if not pending:
            return
        self.data[model].update(pending)

        for relation in model._meta.related_objects:
            if relation.many_to_many:
                continue

            related_model = relation.related_model
            if not hasattr(related_model, "all_objects"):
                continue

            field_name = relation.field.name
            related_qs = (
                related_model.all_objects.using(self.using)
                .filter(is_delete=False)
                .filter(**{f"{field_name}__in": pending})
            )
            on_delete = getattr(relation, "on_delete", None)

            if on_delete is models.CASCADE:
                self._collect_model(
                    related_model,
                    set(related_qs.values_list("pk", flat=True)),
                )
            elif on_delete is models.PROTECT and related_qs.exists():
                raise ProtectedError(
                    f"无法软删除{model._meta.verbose_name}：仍有关联的{related_model._meta.verbose_name}",
                    list(related_qs[:100]),
                )

    def delete(self):
        now = timezone.now()
        total = 0
        counts = {}
        with transaction.atomic(using=self.using):
            for model, pks in self.data.items():
                count = (
                    model.all_objects.using(self.using)
                    .filter(pk__in=pks, is_delete=False)
                    .update(is_delete=True, update_time=now)
                )
                if count:
                    total += count
                    counts[model._meta.label] = count
        if total:
            soft_delete_completed.send(
                sender=type(self),
                models=counts,
                using=self.using,
            )
        return total, counts


class BaseModel(models.Model):
    """模型抽象基类，统一提供审计字段和软删除查询能力。"""

    create_time = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    update_time = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    is_delete = models.BooleanField(default=False, verbose_name="删除标记")

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    def soft_delete(self, using=None):
        using = using or self._state.db or router.db_for_write(type(self), instance=self)
        return type(self).all_objects.using(using).filter(pk=self.pk).soft_delete()

    def restore(self, using=None):
        using = using or self._state.db or router.db_for_write(type(self), instance=self)
        self.is_delete = False
        return type(self).all_objects.using(using).filter(pk=self.pk).update(
            is_delete=False,
            update_time=timezone.now(),
        )

    def hard_delete(self, using=None, keep_parents=False):
        return super().delete(using=using, keep_parents=keep_parents)

    def delete(self, using=None, keep_parents=False):
        return self.soft_delete(using=using)
