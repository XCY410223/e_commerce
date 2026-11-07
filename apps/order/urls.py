from django.urls import path

from . import views

app_name = "order"

urlpatterns = [
    path("list/", views.OrderListView.as_view(), name="list"),
    path("detail/<str:order_id>/", views.order_detail, name="detail"),
    path(
        "comment/<int:order_goods_id>/",
        views.order_comment_submit,
        name="comment",
    ),
    path("place/", views.place, name="place"),
    path("commit/", views.commit, name="commit"),
    path("pay/status/<str:order_id>/", views.pay_status, name="pay_status"),
    path("pay/notify/<str:channel>/", views.pay_notify, name="pay_notify"),
    path("pay/return/<str:channel>/", views.pay_return, name="pay_return"),
    path("pay/mock/<str:order_id>/success/", views.mock_pay_success, name="mock_pay_success"),
    path("pay/mock/<str:order_id>/", views.mock_cashier, name="mock_cashier"),
    path("pay/<str:order_id>/", views.pay, name="pay"),
]
