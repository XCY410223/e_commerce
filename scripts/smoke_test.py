"""全链路冒烟测试：不依赖浏览器，纯 HTTP 走一遍注册→登录→加购→下单。

前置：先启动开发服务器
    .venv\\Scripts\\python.exe manage.py runserver 127.0.0.1:8000
再另开一个终端执行
    .venv\\Scripts\\python.exe scripts\\smoke_test.py

会把每个步骤的 HTTP 状态码与关键断言打印出来，非 2xx/3xx 直接判失败。
"""

import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db.models import F  # noqa: E402
from django.urls import reverse  # noqa: E402
from django_redis import get_redis_connection  # noqa: E402

from goods.models import GoodsSKU  # noqa: E402
from order.models import OrderInfo  # noqa: E402
from user.models import User  # noqa: E402
from user.tokens import make_activation_token  # noqa: E402

HOST = os.getenv("SMOKE_HOST", "http://127.0.0.1:8000")
TEST_USER = "smoke_tester"
TEST_PASS = "smoke123456"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [("User-Agent", "e-commerce-smoke/1.0")]

results = []


def csrf():
    for c in jar:
        if c.name == "csrftoken":
            return c.value
    return ""


def request(url, data=None, method=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(HOST + url, data=body, method=method)
    if data:
        req.add_header("X-CSRFToken", csrf())
    try:
        resp = opener.open(req, timeout=15)
        return resp.status, resp.read().decode("utf-8", "replace"), resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), url


def step(name, url, expect=200, data=None, contains=None, method=None):
    code, html, final = request(url, data, method)
    ok = code == expect
    if ok and contains and contains not in html:
        ok = False
        html_snippet = f"（响应中未找到「{contains}」）"
    else:
        html_snippet = ""
    results.append((ok, name, f"{code} {html_snippet}"))
    return html


def cleanup_test_user():
    """删除测试账号。OrderInfo.user 是 PROTECT，必须先清订单再清用户。"""
    OrderInfo = django.apps.apps.get_model("order", "OrderInfo")
    OrderGoods = django.apps.apps.get_model("order", "OrderGoods")
    orders = OrderInfo.all_objects.filter(user__username=TEST_USER)
    for item in OrderGoods.all_objects.filter(order__in=orders).values("sku_id", "count"):
        GoodsSKU.all_objects.filter(pk=item["sku_id"]).update(
            stock=F("stock") + item["count"],
            sales=F("sales") - item["count"],
        )
    orders.hard_delete()
    User.all_objects.filter(username=TEST_USER).hard_delete()


def main():
    sku = GoodsSKU.objects.filter(status=GoodsSKU.STATUS_ONLINE).order_by("id").first()
    if sku is None:
        print("没有上架的 SKU，请先执行 scripts/seed.py")
        return 1

    cleanup_test_user()

    step("首页", "/", contains="天天生鲜")
    step("促销活动页", "/promo/new-customer/", contains="新客立减 20 元")
    step("分类列表", f"/list/{sku.type.id}/", contains=sku.type.name)
    step("商品详情", f"/detail/{sku.id}/", contains=sku.name)
    step("搜索页", "/search/?q=" + urllib.parse.quote("水果"))
    step("注册页", "/user/register/")
    step(
        "提交注册",
        "/user/register/",
        expect=200,  # 重定向由 opener 自动跟随
        data={
            "username": TEST_USER,
            "email": "smoke_tester@example.com",
            "password": TEST_PASS,
            "password2": TEST_PASS,
        },
        contains="激活邮件已发送",
    )
    test_user = User.objects.filter(username=TEST_USER).first()
    if test_user is None:
        results.append((False, "获取注册用户", "注册后未在数据库中找到测试账号"))
    else:
        activation_url = reverse(
            "user:activate",
            kwargs={"token": make_activation_token(test_user)},
        )
        step("激活账号", activation_url, contains="账号激活成功")
    step("登录页", "/user/login/")

    code, body, final = request("/user/center/")
    results.append(
        (
            code == 200 and final.endswith("/user/login/?next=/user/center/"),
            "登录装饰器跳转",
            final,
        )
    )

    # 游客也能加购：未登录不再返回 401，而是下发 guest_cart Cookie
    code, body, _ = request(
        f"/cart/add/{sku.id}/?_next=/detail/{sku.id}/",
        {"count": "1"},
    )
    guest_token = ""
    try:
        payload = json.loads(body)
        guest_token = next((c.value for c in jar if c.name == "guest_cart"), "")
        guest_ok = code == 200 and payload.get("code") == 0 and bool(guest_token)
    except json.JSONDecodeError:
        guest_ok, payload = False, {}
    if guest_token:  # 清掉游客购物车，后续步骤仍按“空购物车”开始
        get_redis_connection("default").delete(f"cart:guest:{guest_token}")
    results.append(
        (
            guest_ok,
            "游客加购(免登录)",
            f"{code} {payload.get('msg', '')}",
        )
    )

    step(
        "提交登录",
        "/user/login/?next=/user/center/",
        data={
            "username": TEST_USER,
            "password": TEST_PASS,
            "next": reverse("user:center"),
        },
        contains="账户信息",
    )
    step("浏览商品(记录历史)", f"/detail/{sku.id}/", contains=sku.name)
    step("个人中心", reverse("user:center"), contains="账户信息")
    step("个人中心浏览历史", reverse("user:center"), contains=sku.name)
    step("收货地址页", "/user/address/", contains="新增地址")
    address_html = step(
        "新增地址",
        "/user/address/",
        data={"action": "create", "receiver": "测试员", "addr": "测试地址 1 号", "phone": "13900001111", "zip_code": ""},
        contains="测试地址 1 号",
    )
    address_match = re.search(r'name="addr_id" value="(\d+)"', address_html)
    if not address_match:
        results.append((False, "获取地址ID", "新增地址后未找到 addr_id"))
    else:
        address_id = address_match.group(1)
        step(
            "编辑地址页",
            f"/user/address/?edit={address_id}",
            contains="编辑地址",
        )
        step(
            "保存地址修改",
            "/user/address/",
            data={
                "action": "update",
                "addr_id": address_id,
                "receiver": "测试员",
                "addr": "测试地址 2 号",
                "phone": "13900001111",
                "zip_code": "",
            },
            contains="测试地址 2 号",
        )
    step("空购物车", "/cart/", contains="购物车")

    code, body, _ = request(f"/cart/add/{sku.id}/", {"count": "2"})
    try:
        payload = json.loads(body)
        add_ok = code == 200 and payload.get("code") == 0
    except json.JSONDecodeError:
        add_ok, payload = False, {}
    results.append((add_ok, "加入购物车(AJAX)", f"{code} cart_count={payload.get('cart_count')}"))

    step("购物车列表", "/cart/", contains=sku.name)
    code, place_html, _ = request("/order/place/")
    results.append((code == 200 and "提交订单" in place_html, "确认订单页", str(code)))

    addr_id = re.search(r'name="addr_id" value="(\d+)"', place_html)
    submit_token = re.search(r'name="submit_token" value="([^"]+)"', place_html)
    expected_count = re.search(r'name="expected_count" value="(\d+)"', place_html)
    expected_amount = re.search(r'name="expected_amount" value="([^"]+)"', place_html)
    coupon_id = re.search(r'name="coupon_id" value="(\d+)"', place_html)
    expected_fingerprint = re.search(
        r'name="expected_fingerprint" value="([0-9a-f]+)"', place_html
    )
    if not addr_id:
        results.append((False, "获取地址ID", "未能从确认页解析 addr_id"))
    else:
        payload = {
            "addr_id": addr_id.group(1),
            "pay_method": "1",
            "submit_token": submit_token.group(1) if submit_token else "",
            "expected_count": expected_count.group(1) if expected_count else "",
            "expected_amount": expected_amount.group(1) if expected_amount else "",
            "coupon_id": coupon_id.group(1) if coupon_id else "0",
            "expected_fingerprint": (
                expected_fingerprint.group(1) if expected_fingerprint else ""
            ),
        }
        before_submit = OrderInfo.objects.filter(user__username=TEST_USER).count()
        step(
            "提交订单",
            "/order/commit/",
            data=payload,
            contains="天天生鲜",
        )
        created_count = OrderInfo.objects.filter(user__username=TEST_USER).count()
        results.append(
            (
                created_count == before_submit + 1,
                "订单已写入",
                f"订单数 {before_submit}->{created_count}",
            )
        )
        before = OrderInfo.objects.filter(user__username=TEST_USER).count()
        code, _, final = request("/order/commit/", payload)
        after = OrderInfo.objects.filter(user__username=TEST_USER).count()
        results.append(
            (
                after == before and final.endswith("/order/list/"),
                "重复提交拦截",
                f"{code} 订单数 {before}->{after}",
            )
        )
        step("订单列表", reverse("order:list"), contains="我的订单")

        # 支付结果链路：发起支付 → 本地模拟收银台 → 状态查询
        test_order = (
            OrderInfo.objects.filter(user__username=TEST_USER)
            .order_by("-create_time")
            .first()
        )
        if test_order is None:
            results.append((False, "获取待支付订单", "订单列表中没有找到测试订单"))
        else:
            step(
                "订单详情",
                f"/order/detail/{test_order.order_id}/",
                contains=sku.name,
            )
            code, pay_html, final = request(f"/order/pay/{test_order.order_id}/")
            results.append(
                (
                    code == 200 and "模拟收银台" in pay_html and final.endswith(
                        f"/order/pay/mock/{test_order.order_id}/"
                    ),
                    "发起支付",
                    f"{code} {final}",
                )
            )
            code, _, final = request(
                f"/order/pay/mock/{test_order.order_id}/success/", {"confirm": "1"}
            )
            test_order.refresh_from_db()
            results.append(
                (
                    code == 200
                    and test_order.pay_status == OrderInfo.STATUS_PAID
                    and final.endswith("/order/list/"),
                    "模拟支付成功",
                    f"{code} 状态 {test_order.pay_status}",
                )
            )
            code, body, _ = request(f"/order/pay/status/{test_order.order_id}/")
            try:
                status_payload = json.loads(body)
            except json.JSONDecodeError:
                status_payload = {}
            results.append(
                (
                    code == 200 and status_payload.get("pay_status") == OrderInfo.STATUS_PAID,
                    "支付状态查询",
                    f"{code} {status_payload.get('pay_status')}",
                )
            )

    step("后台入口", "/admin/login/", contains="Django")

    cleanup_test_user()

    print("=" * 62)
    width = max(len(n) for _, n, _ in results)
    failed = 0
    for ok, name, detail in results:
        flag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{flag}] {name.ljust(width)}  {detail}")
    print("=" * 62)
    print(f"共 {len(results)} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
