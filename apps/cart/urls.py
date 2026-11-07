from django.urls import path

from . import views

app_name = "cart"

urlpatterns = [
    path("", views.cart_index, name="index"),
    path("add/<int:sku_id>/", views.add, name="add"),
    path("update/<int:sku_id>/", views.update, name="update"),
    path("delete/<int:sku_id>/", views.delete, name="delete"),
]
