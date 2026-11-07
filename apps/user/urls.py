from django.urls import path

from . import views

app_name = "user"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("activate/<str:token>/", views.activate, name="activate"),
    path("login/", views.UserLoginView.as_view(), name="login"),
    path("logout/", views.UserLogoutView.as_view(), name="logout"),
    path("address/", views.address_list, name="address"),
    path("center/", views.user_center, name="center"),
]
