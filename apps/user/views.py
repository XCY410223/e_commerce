"""用户模块视图。

已完成：注册（itsdangerous 邮件激活）、登录、退出、个人中心、收货地址增删改。
待补：密码找回。
"""

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods
from itsdangerous import BadSignature, SignatureExpired

from celery_tasks.tasks import send_activation_email
from goods.history import read_history
from order.models import OrderInfo
from promotion.models import Referral, UserCoupon
from promotion.services import (
    available_coupons,
    bind_referral,
    decorate_skus_with_prices,
    ensure_new_customer_coupon,
    load_referral_inviter,
    make_referral_code,
    referral_available,
    referral_summary,
)
from utils.cart import GUEST_CART_COOKIE, merge_guest_cart

from .models import Address, User
from .tokens import load_activation_token

logger = logging.getLogger(__name__)

REMEMBER_USERNAME_MAX_AGE = 7 * 24 * 60 * 60  # 记住用户名 7 天
USERNAME_MAX_LENGTH = 150
EMAIL_MAX_LENGTH = 254
ADDRESS_FIELD_LIMITS = {
    "receiver": (20, "收件人"),
    "addr": (256, "收货地址"),
    "phone": (11, "联系电话"),
    "zip_code": (6, "邮编"),
}


@require_http_methods(["GET", "POST"])
def register(request):
    referral_code = request.POST.get("ref", "") or request.GET.get("ref", "")
    referral_inviter = load_referral_inviter(referral_code)
    if referral_inviter is None:
        referral_code = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        password2 = request.POST.get("password2", "")
        email = request.POST.get("email", "").strip()

        ctx = {"username": username, "email": email, "ref": referral_code}
        if not username or not password or not email:
            return render(request, "user/register.html", {**ctx, "error": "用户名、密码和邮箱不能为空"})
        if password != password2:
            return render(request, "user/register.html", {**ctx, "error": "两次输入的密码不一致"})
        if len(password) < 6:
            return render(request, "user/register.html", {**ctx, "error": "密码长度不能少于 6 位"})
        if len(username) > USERNAME_MAX_LENGTH:
            return render(
                request,
                "user/register.html",
                {**ctx, "error": f"用户名不能超过 {USERNAME_MAX_LENGTH} 个字符"},
            )
        try:
            UnicodeUsernameValidator()(username)
        except ValidationError:
            return render(
                request,
                "user/register.html",
                {**ctx, "error": "用户名只能包含字母、数字和 @/./+/-/_ 字符"},
            )
        if len(email) > EMAIL_MAX_LENGTH:
            return render(
                request,
                "user/register.html",
                {**ctx, "error": f"邮箱不能超过 {EMAIL_MAX_LENGTH} 个字符"},
            )
        try:
            validate_email(email)
        except ValidationError:
            return render(request, "user/register.html", {**ctx, "error": "邮箱格式不正确"})
        if User.all_objects.filter(username=username).exists():
            return render(request, "user/register.html", {**ctx, "error": "该用户名已被注册"})
        if User.all_objects.filter(email__iexact=email).exists():
            return render(request, "user/register.html", {**ctx, "error": "该邮箱已被注册"})

        try:
            user = User.objects.create_user(
                username=username,
                password=password,
                email=email,
                is_active=False,
            )
        except IntegrityError:
            # 并发注册时两个请求可能同时通过上面的 exists() 检查；数据库
            # 唯一约束是最终防线，这里转换成可读提示而不是 500。
            logger.warning("并发注册触发唯一约束 username=%s", username)
            if User.all_objects.filter(username=username).exists():
                error = "该用户名已被注册"
            elif User.all_objects.filter(email__iexact=email).exists():
                error = "该邮箱已被注册"
            else:
                error = "注册失败，请稍后重试"
            return render(request, "user/register.html", {**ctx, "error": error})
        if referral_inviter is not None:
            bind_referral(referral_inviter, user, referral_code)
        if _enqueue_activation_email(user.pk):
            messages.success(request, "注册成功，激活邮件已发送，请查收后激活账号")
        else:
            messages.warning(request, "账号已创建，但激活邮件发送任务提交失败，请联系管理员")
        return redirect(reverse("user:login"))

    return render(request, "user/register.html", {"ref": referral_code})


class UserLoginView(LoginView):
    """用户登录页与登录处理。"""

    template_name = "user/login.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        username = form.cleaned_data.get("username") or self.request.POST.get("username", "").strip()
        messages.success(self.request, f"欢迎回来，{username}！")
        self._merge_guest_cart(response)
        if self.request.POST.get("remember") == "on":
            response.set_cookie(
                "username",
                username,
                max_age=REMEMBER_USERNAME_MAX_AGE,
                httponly=True,
                samesite="Lax",
            )
        else:
            response.delete_cookie("username", samesite="Lax")
        return response

    def _merge_guest_cart(self, response):
        """把 Cookie 里的游客购物车并入账号购物车，成功后清掉 Cookie。"""
        token = self.request.COOKIES.get(GUEST_CART_COOKIE, "")
        if not token:
            return
        try:
            merged = merge_guest_cart(self.request.user.id, token)
        except Exception:
            logger.warning(
                "合并游客购物车失败 user_id=%s", self.request.user.id, exc_info=True
            )
            # 合并失败时保留 Cookie，下次登录可以重试，避免直接丢购物车。
            return
        if merged:
            messages.info(self.request, "游客购物车已并入你的账号购物车")
            response.delete_cookie(GUEST_CART_COOKIE, samesite="Lax")

    def form_invalid(self, form):
        username = self.request.POST.get("username", "").strip()
        password = self.request.POST.get("password", "")
        candidate = User.objects.filter(username=username).first()
        error = "用户名或密码错误"
        if candidate and not candidate.is_active and candidate.check_password(password):
            error = "账号尚未激活，请先查收激活邮件"
        return self.render_to_response(
            self.get_context_data(form=form, error=error)
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.method == "POST":
            context["username"] = self.request.POST.get("username", "")
            context["remember_username"] = self.request.POST.get("remember") == "on"
        else:
            remembered_username = self.request.COOKIES.get("username", "")
            context["username"] = remembered_username
            context["remember_username"] = bool(remembered_username)
        return context


class UserLogoutView(LogoutView):
    """用户退出，仅接受 POST 请求。"""

    next_page = "goods:index"


@require_GET
def activate(request, token):
    """校验 itsdangerous token 并激活用户账号。"""
    try:
        payload = load_activation_token(token)
        if payload.get("action") != "activate":
            raise ValueError("invalid activation action")
        user_id = int(payload["uid"])
    except SignatureExpired:
        messages.error(request, "激活链接已过期，请重新注册或联系管理员")
        return redirect("user:login")
    except (BadSignature, KeyError, TypeError, ValueError):
        messages.error(request, "激活链接无效，请确认链接是否完整")
        return redirect("user:login")

    user = User.objects.filter(pk=user_id).first()
    if user is None or user.email != payload.get("email"):
        messages.error(request, "激活链接无效，用户信息不匹配")
        return redirect("user:login")
    if user.is_active:
        messages.info(request, "账号已经激活，请直接登录")
        return redirect("user:login")

    user.is_active = True
    user.save(update_fields=["is_active", "update_time"])
    try:
        ensure_new_customer_coupon(user)
    except Exception:
        logger.exception("激活后发放新人券失败 user_id=%s", user.pk)
    messages.success(request, "账号激活成功，请登录")
    return redirect("user:login")


@login_required
def user_center(request):
    """个人中心：账户信息 + 默认地址 + 浏览历史 + 最近订单。"""
    history_skus = read_history(request.user.id)
    decorate_skus_with_prices(history_skus)
    try:
        ensure_new_customer_coupon(request.user)
    except Exception:
        logger.exception("个人中心补发新人券失败 user_id=%s", request.user.pk)
    coupons = UserCoupon.objects.filter(user=request.user).select_related("promotion")[:20]
    referral_on = referral_available()
    if referral_on:
        referral_code = make_referral_code(request.user)
        referral_url = request.build_absolute_uri(
            f"{reverse('user:register')}?{urlencode({'ref': referral_code})}"
        )
        referrals = (
            Referral.objects.filter(inviter=request.user)
            .select_related("invitee", "reward_coupon")
            .order_by("-create_time")[:10]
        )
    else:
        referral_code = ""
        referral_url = ""
        referrals = Referral.objects.none()
    return render(
        request,
        "user/center.html",
        {
            "default_address": Address.objects.default_for(request.user),
            "history_skus": history_skus,
            "recent_orders": OrderInfo.objects.for_user(request.user)[:5],
            "coupons": coupons,
            "available_coupons": available_coupons(request.user),
            "referral_code": referral_code,
            "referral_url": referral_url,
            "referral_available": referral_on,
            "referral_summary": referral_summary(request.user),
            "referrals": referrals,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def address_list(request):
    """收货地址管理：列表、新增、编辑、设为默认和删除。"""
    addresses = Address.objects.for_user(request.user)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            data = _posted_address(request)
            error = _validate_address(data)
            if error:
                return render(
                    request,
                    "user/address.html",
                    {"addresses": addresses, "error": error, "form_data": data},
                )
            with transaction.atomic():
                _lock_address_owner(request.user)
                Address.objects.create(
                    user=request.user,
                    is_default=not Address.objects.for_user(request.user).exists(),
                    **data,
                )
            messages.success(request, "地址已保存")
            return redirect(reverse("user:address"))

        data = None
        if action == "update":
            data = _posted_address(request)
            error = _validate_address(data)
            if error:
                addr = _get_user_address(request, request.POST.get("addr_id"))
                return render(
                    request,
                    "user/address.html",
                    {
                        "addresses": addresses,
                        "error": error,
                        "edit_address": addr,
                        "form_data": data,
                    },
                )

        if action not in {"update", "set_default", "delete"}:
            messages.error(request, "无效的地址操作")
            return redirect(reverse("user:address"))

        # 所有地址写操作先锁用户行，和下单流程使用同一把数据库锁；
        # 这样“创建第一条地址”和“设为默认”不会并发留下多个默认地址。
        with transaction.atomic():
            _lock_address_owner(request.user)
            addr = _get_user_address(request, request.POST.get("addr_id"))
            if addr is None:
                messages.error(request, "地址不存在或已删除")
            elif action == "update":
                for field, value in data.items():
                    setattr(addr, field, value)
                addr.save(update_fields=[*data, "update_time"])
                messages.success(request, "地址已更新")
            elif action == "set_default":
                if addr.is_default:
                    messages.info(request, "该地址已经是默认地址")
                else:
                    Address.objects.for_user(request.user).update(is_default=False)
                    addr.is_default = True
                    addr.save(update_fields=["is_default", "update_time"])
                    messages.success(request, "已设为默认地址")
            else:
                was_default = addr.is_default
                # 订单通过 PROTECT 外键引用地址；这里直接软删除地址行，
                # 既能从用户列表移除，又保留历史订单关联不产生 500。
                Address.all_objects.filter(pk=addr.pk, is_delete=False).update(
                    is_delete=True,
                    update_time=timezone.now(),
                )
                if was_default:
                    replacement = Address.objects.for_user(request.user).first()
                    if replacement:
                        replacement.is_default = True
                        replacement.save(update_fields=["is_default", "update_time"])
                messages.success(request, "地址已删除")
        return redirect(reverse("user:address"))

    edit_address = None
    form_data = {}
    edit_id = request.GET.get("edit")
    if edit_id:
        edit_address = _get_user_address(request, edit_id)
        if edit_address is None:
            messages.error(request, "要编辑的地址不存在")
        else:
            form_data = {
                "receiver": edit_address.receiver,
                "addr": edit_address.addr,
                "phone": edit_address.phone,
                "zip_code": edit_address.zip_code,
            }

    return render(
        request,
        "user/address.html",
        {
            "addresses": addresses,
            "edit_address": edit_address,
            "form_data": form_data,
        },
    )


def _posted_address(request):
    """提取地址表单字段，避免在各分支重复读取 POST。"""
    return {
        field: request.POST.get(field, "").strip()
        for field in ("receiver", "addr", "phone", "zip_code")
    }


def _get_user_address(request, address_id):
    """只允许操作当前用户自己的地址。"""
    try:
        address_id = int(address_id)
    except (TypeError, ValueError):
        return None
    return Address.objects.for_user(request.user).filter(pk=address_id).first()


def _lock_address_owner(user):
    """按用户串行化地址写操作，和下单事务使用同一把行锁。"""
    User.objects.select_for_update().filter(pk=user.pk).first()


def _validate_address(data):
    """校验地址字段，成功返回 None，失败返回错误文案。"""
    if not (data["receiver"] and data["addr"] and data["phone"]):
        return "收件人、地址、联系电话均为必填"
    for field, (max_length, label) in ADDRESS_FIELD_LIMITS.items():
        if len(data[field]) > max_length:
            return f"{label}不能超过 {max_length} 个字符"
    if not data["phone"].isdigit() or len(data["phone"]) != 11:
        return "联系电话必须是 11 位数字"
    if data["zip_code"] and (
        not data["zip_code"].isdigit() or len(data["zip_code"]) != 6
    ):
        return "邮编必须是 6 位数字"
    return None


def _enqueue_activation_email(user_id):
    """把发送激活邮件投递到 Celery；消息队列不可用时不阻断注册。"""
    try:
        send_activation_email.delay(user_id)
    except Exception:
        logger.exception("激活邮件任务投递失败 user_id=%s", user_id)
        return False
    return True
