"""分类列表的共享查询规则。

动态视图与静态页生成必须使用同一份过滤/排序逻辑，避免两套规则漂移导致
同一个 URL 在命中静态页和回退动态页时展示不同结果。
"""

from .models import GoodsSKU

LIST_PAGE_SIZE = 10
LIST_SORTS = ("default", "price", "sales")


def normalize_sort(sort):
    """返回受支持的排序键；未知值按默认排序处理。"""
    return sort if sort in LIST_SORTS else "default"


def list_queryset(goods_type, sort="default"):
    """分类下已上架商品的列表查询。

    每种排序都追加主键作为次级排序，保证分页边界稳定：MySQL 对相同排序值
    不保证返回顺序，缺少唯一次级键时翻页可能出现重复或漏项。
    """
    queryset = (
        GoodsSKU.objects.online()
        .filter(type=goods_type)
        .prefetch_related("images")
    )
    if sort == "price":
        return queryset.order_by("price", "id")
    if sort == "sales":
        return queryset.order_by("-sales", "id")
    return queryset.order_by("-create_time", "id")
