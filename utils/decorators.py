"""视图公共装饰器。"""

from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


def ajax_login_required(view_func):
    """AJAX 接口登录校验：未登录返回 JSON 401，而不是 302 登录页 HTML。"""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)

        next_url = (
            request.GET.get("_next")
            or request.POST.get("_next")
            or request.META.get("HTTP_REFERER")
            or request.get_full_path()
        )
        if not url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = request.get_full_path()

        login_url = redirect_to_login(
            next_url, resolve_url(settings.LOGIN_URL)
        ).url
        return JsonResponse(
            {
                "code": 401,
                "msg": "登录状态已失效，请先登录",
                "login_url": login_url,
            },
            status=401,
        )

    return wrapper
