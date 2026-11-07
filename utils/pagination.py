"""分页辅助。

统一处理两件事：
1. 生成翻页链接时需要保留的查询串（去掉 page）；
2. 生成带省略号的页码序列，避免页码无限铺开。
"""


def querystring_without_page(request, overrides=None):
    """保留 sort/q 等参数并剔除 page，供翻页链接拼接。"""
    params = request.GET.copy()
    params.pop("page", None)
    if overrides:
        for key, value in overrides.items():
            params[key] = value
    return params.urlencode()


def elided_page_range(page_obj, *, on_each_side=2, on_ends=1):
    """返回如 [1, '…', 4, 5, 6, '…', 20] 的页码序列。"""
    return page_obj.paginator.get_elided_page_range(
        page_obj.number,
        on_each_side=on_each_side,
        on_ends=on_ends,
    )


class PaginationContextMixin:
    """给 ListView 注入 page_range 与 querystring。"""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page_obj = context.get("page_obj")
        if page_obj is not None:
            context["page_range"] = elided_page_range(page_obj)
            context["querystring"] = querystring_without_page(self.request)
        return context
