"""项目根路由。

注意：
1. Django 5.0 起 `django.urls.url()` 已被移除，统一使用 `path()` / `re_path()`；
2. 兜底路由 `""` 必须放在最后，否则会吞掉后续所有规则。
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("tinymce/", include("tinymce.urls")),
    path("user/", include("user.urls", namespace="user")),
    path("cart/", include("cart.urls", namespace="cart")),
    path("order/", include("order.urls", namespace="order")),
    path("", include("goods.urls", namespace="goods")),  # 商品模块兜底，置末位
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
