from django.urls import path

from . import views

app_name = "goods"

urlpatterns = [
    path("", views.index, name="index"),
    path("promo/<slug:slug>/", views.promotion_detail, name="promotion"),
    path("search/", views.search, name="search"),
    path("list/<int:type_id>/", views.GoodsListView.as_view(), name="list"),
    path("detail/<int:sku_id>/", views.GoodsDetailView.as_view(), name="detail"),
]
