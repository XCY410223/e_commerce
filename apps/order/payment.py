"""支付结果获取与订单支付状态更新。

设计原则：
1. 异步通知是权威来源，必须验签、核对金额并幂等落库；
2. 同步 return_url 只负责把用户带回站点，不能直接相信 URL 参数；
3. 主动查询作为通知丢失时的兜底，也供支付结果页轮询；
4. 所有状态变更集中在 mark_paid()，用订单行锁避免重复通知并发写。

默认使用 mock provider，方便本地跑通完整链路；切换 alipay 时安装
python-alipay-sdk，并在 .env 中填写应用 ID、私钥和支付宝公钥。
"""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import OrderInfo

try:  # 可选依赖：只有 PAYMENT_PROVIDER=alipay 时才需要
    from alipay import AliPay
except ImportError:  # pragma: no cover - 默认 mock 环境不会走到这里
    AliPay = None


logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


@dataclass(frozen=True)
class PaymentIntent:
    """发起支付后前端需要跳转的地址。"""

    url: str
    provider: str


@dataclass(frozen=True)
class PaymentResult:
    """不同支付渠道归一化后的支付结果。"""

    paid: bool
    out_trade_no: str = ""
    trade_no: str = ""
    amount: Decimal | None = None
    paid_at: datetime | None = None
    raw: dict = field(default_factory=dict)


class PaymentError(Exception):
    """可直接展示给用户的支付业务异常。"""


class PaymentNotConfigured(PaymentError):
    """支付渠道未安装依赖或缺少配置。"""


def _to_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = parse_datetime(str(value))
        if parsed is None:
            try:
                parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _request_payload(request):
    """同时兼容表单通知和 JSON 通知。"""
    if request.content_type == "application/json":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaymentError("支付通知不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise PaymentError("支付通知格式不正确")
        return {str(key): "" if value is None else str(value) for key, value in payload.items()}
    return {str(key): str(value) for key, value in request.POST.items()}


def _mock_canonical(payload):
    return "&".join(f"{key}={payload[key]}" for key in sorted(payload))


def build_mock_notify_payload(order, trade_no=None):
    """生成本地联调用的带签名 mock 通知，供测试脚本/开发工具调用。"""
    payload = {
        "out_trade_no": order.order_id,
        "trade_no": trade_no or f"MOCK{order.order_id}",
        "total_amount": f"{order.total_pay:.2f}",
        "status": "paid",
        "paid_at": timezone.now().isoformat(),
    }
    signature = hmac.new(
        settings.PAYMENT_MOCK_SECRET.encode("utf-8"),
        _mock_canonical(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    payload["sign"] = signature
    return payload


class BaseProvider:
    name = "base"
    notify_ack = "success"

    def create_payment(self, order, request=None, *, notify_url=None, return_url=None):
        raise NotImplementedError

    def verify_notify(self, request):
        raise NotImplementedError

    def query(self, order):
        return None


class MockProvider(BaseProvider):
    """本地模拟收银台，不依赖任何第三方支付账号。"""

    name = "mock"

    def create_payment(self, order, request=None, *, notify_url=None, return_url=None):
        return PaymentIntent(
            url=reverse("order:mock_cashier", kwargs={"order_id": order.order_id}),
            provider=self.name,
        )

    def verify_notify(self, request):
        payload = _request_payload(request)
        signature = payload.pop("sign", "")
        expected = hmac.new(
            settings.PAYMENT_MOCK_SECRET.encode("utf-8"),
            _mock_canonical(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise PaymentError("模拟支付通知签名校验失败")
        return PaymentResult(
            paid=str(payload.get("status", "")).lower() in {"paid", "success"},
            out_trade_no=str(payload.get("out_trade_no", "")),
            trade_no=str(payload.get("trade_no", "")),
            amount=_to_decimal(payload.get("total_amount")),
            paid_at=_to_datetime(payload.get("paid_at")) or timezone.now(),
            raw=payload,
        )


class AlipayProvider(BaseProvider):
    """支付宝支付适配器（电脑网站支付 / 手机网站支付）。

    用 PAYMENT_ALIPAY_PAY_METHOD 选择：page = alipay.trade.page.pay，wap = alipay.trade.wap.pay。
    python-alipay-sdk 是可选依赖；未安装时默认的 mock provider 仍可正常使用。
    """

    name = "alipay"

    def _client(self):
        if AliPay is None:
            raise PaymentNotConfigured(
                "未安装 python-alipay-sdk，无法使用支付宝支付；请先安装依赖或设置 PAYMENT_PROVIDER=mock"
            )
        missing = [
            label
            for label, value in (
                ("PAYMENT_ALIPAY_APP_ID", settings.PAYMENT_ALIPAY_APP_ID),
                ("PAYMENT_ALIPAY_PRIVATE_KEY", settings.PAYMENT_ALIPAY_PRIVATE_KEY),
                ("PAYMENT_ALIPAY_PUBLIC_KEY", settings.PAYMENT_ALIPAY_PUBLIC_KEY),
            )
            if not value
        ]
        if missing:
            raise PaymentNotConfigured(f"支付宝配置缺失：{', '.join(missing)}")
        return AliPay(
            appid=settings.PAYMENT_ALIPAY_APP_ID,
            app_notify_url=settings.PAYMENT_ALIPAY_NOTIFY_URL,
            app_private_key_string=settings.PAYMENT_ALIPAY_PRIVATE_KEY,
            alipay_public_key_string=settings.PAYMENT_ALIPAY_PUBLIC_KEY,
            sign_type="RSA2",
            debug=settings.PAYMENT_ALIPAY_DEBUG,
        )

    def create_payment(self, order, request=None, *, notify_url=None, return_url=None):
        if order.pay_method != OrderInfo.PAY_ALIPAY:
            raise PaymentError("当前订单选择的支付方式不是支付宝")
        method = settings.PAYMENT_ALIPAY_PAY_METHOD
        client = self._client()
        if method == "wap":
            build = client.api_alipay_trade_wap_pay
        elif method == "page":
            build = client.api_alipay_trade_page_pay
        else:
            raise PaymentNotConfigured(f"不支持的支付宝支付方式：{method}")
        try:
            order_string = build(
                out_trade_no=order.order_id,
                total_amount=f"{order.total_pay:.2f}",
                subject=f"天天生鲜订单 {order.order_id}",
                return_url=return_url or settings.PAYMENT_ALIPAY_RETURN_URL,
                notify_url=notify_url or settings.PAYMENT_ALIPAY_NOTIFY_URL,
            )
        except PaymentError:
            raise
        except Exception as exc:
            raise PaymentError(f"发起支付宝支付失败：{exc}") from exc
        gateway = settings.PAYMENT_ALIPAY_GATEWAY.rstrip("?")
        return PaymentIntent(url=f"{gateway}?{order_string}", provider=self.name)

    def verify_notify(self, request):
        return self._verify_params(request.POST.dict())

    def verify_return(self, request):
        return self._verify_params(request.GET.dict())

    def _verify_params(self, params):
        data = {str(key): str(value) for key, value in params.items()}
        signature = data.pop("sign", "")
        if not signature:
            raise PaymentError("支付通知缺少签名")
        try:
            verified = self._client().verify(data, signature)
        except PaymentError:
            raise
        except Exception as exc:
            raise PaymentError(f"支付宝验签失败：{exc}") from exc
        if not verified:
            raise PaymentError("支付宝签名校验失败")
        if data.get("app_id") and data["app_id"] != settings.PAYMENT_ALIPAY_APP_ID:
            raise PaymentError("支付宝 app_id 不匹配")
        if (
            settings.PAYMENT_ALIPAY_SELLER_ID
            and data.get("seller_id")
            and data["seller_id"] != settings.PAYMENT_ALIPAY_SELLER_ID
        ):
            raise PaymentError("支付宝 seller_id 不匹配")
        return self._result_from_params(data)

    def _result_from_params(self, data):
        trade_status = str(data.get("trade_status", ""))
        paid = trade_status in {"TRADE_SUCCESS", "TRADE_FINISHED"}
        return PaymentResult(
            paid=paid,
            out_trade_no=str(data.get("out_trade_no", "")),
            trade_no=str(data.get("trade_no", "")),
            amount=_to_decimal(data.get("total_amount")),
            paid_at=_to_datetime(data.get("gmt_payment")) or (timezone.now() if paid else None),
            raw=data,
        )

    def query(self, order):
        try:
            result = self._client().api_alipay_trade_query(out_trade_no=order.order_id)
        except PaymentError:
            raise
        except Exception as exc:
            raise PaymentError(f"查询支付宝订单失败：{exc}") from exc
        if not result or str(result.get("code")) != "10000":
            return None
        return self._result_from_params(result)


_PROVIDERS = {
    MockProvider.name: MockProvider,
    AlipayProvider.name: AlipayProvider,
}

# 真实支付渠道只能更新支付方式匹配的订单，防止通知/同步返回串单。
_PROVIDER_PAY_METHODS = {
    AlipayProvider.name: OrderInfo.PAY_ALIPAY,
}


def get_provider(channel=None):
    configured = (settings.PAYMENT_PROVIDER or "mock").strip().lower()
    name = (channel or configured).strip().lower()
    if channel and name != configured:
        raise PaymentNotConfigured(f"支付渠道 {name} 与当前配置 {configured} 不一致")
    provider_class = _PROVIDERS.get(name)
    if provider_class is None:
        raise PaymentNotConfigured(f"不支持的支付渠道：{name}")
    return provider_class()


def mock_payment_enabled():
    """mock 收银台仅在开发环境（DEBUG）且配置 mock provider 时可用。

    生产环境 DEBUG=False，即使误配 PAYMENT_PROVIDER=mock 也会在这里被拒绝，
    避免任何登录用户通过模拟按钮把自己的订单改成已支付。
    """
    return settings.DEBUG and (settings.PAYMENT_PROVIDER or "mock").strip().lower() == "mock"


def create_payment(order, request=None, *, notify_url=None, return_url=None):
    if order.pay_status == OrderInfo.STATUS_PAID:
        raise PaymentError("订单已支付")
    if order.pay_status == OrderInfo.STATUS_CANCELLED:
        raise PaymentError("订单已取消，不能继续支付")
    provider = get_provider()
    if provider.name == "mock" and not mock_payment_enabled():
        raise PaymentError("模拟支付仅在开发环境（DEBUG）可用，请检查支付配置")
    if provider.name == "alipay" and order.pay_method != OrderInfo.PAY_ALIPAY:
        raise PaymentError("当前订单选择的支付方式尚未接入")
    return provider.create_payment(
        order,
        request,
        notify_url=notify_url,
        return_url=return_url,
    )


def mark_paid(
    order_id,
    trade_no,
    paid_amount,
    paid_at=None,
    raw=None,
    expected_channel=None,
):
    """把订单标记为已支付，返回 (是否成功, 原因)。

    重复通知、主动查询和同步返回可能同时到达，因此必须锁订单行并做幂等判断。
    支付只改状态，不重复扣库存/加销量；库存在下单事务里已经处理。
    """
    trade_no = str(trade_no or "").strip()
    if not trade_no:
        return False, "trade_no_missing"
    amount = _to_decimal(paid_amount)
    if amount is None:
        return False, "amount_invalid"
    paid_at = _to_datetime(paid_at) or timezone.now()

    try:
        with transaction.atomic():
            order = OrderInfo.objects.select_for_update().filter(order_id=order_id).first()
            if order is None:
                logger.warning("支付结果找不到订单 order_id=%s trade_no=%s", order_id, trade_no)
                return False, "order_not_found"

            expected_pay_method = _PROVIDER_PAY_METHODS.get(expected_channel)
            if (
                expected_pay_method is not None
                and order.pay_method != expected_pay_method
            ):
                logger.error(
                    "支付渠道与订单支付方式不匹配 order_id=%s channel=%s order_method=%s",
                    order_id,
                    expected_channel,
                    order.pay_method,
                )
                return False, "pay_method_mismatch"

            if order.pay_status == OrderInfo.STATUS_CANCELLED:
                logger.critical(
                    "已取消订单收到支付结果，需人工核对退款 order_id=%s channel=%s trade_no=%s amount=%s",
                    order_id,
                    expected_channel,
                    trade_no,
                    amount,
                )
                return False, "order_cancelled"

            if order.pay_status == OrderInfo.STATUS_PAID:
                if order.trade_no and order.trade_no != trade_no:
                    logger.error(
                        "订单已支付但流水号不一致 order_id=%s old=%s new=%s",
                        order_id,
                        order.trade_no,
                        trade_no,
                    )
                    return False, "trade_no_conflict"
                if amount != order.total_pay:
                    logger.error(
                        "已支付订单收到金额不一致的通知 order_id=%s expected=%s paid=%s trade_no=%s",
                        order_id,
                        order.total_pay,
                        amount,
                        trade_no,
                    )
                    return False, "amount_mismatch"
                update_fields = []
                if not order.trade_no:
                    order.trade_no = trade_no
                    update_fields.append("trade_no")
                if order.paid_amount is None:
                    order.paid_amount = amount
                    update_fields.append("paid_amount")
                if order.pay_time is None:
                    order.pay_time = paid_at
                    update_fields.append("pay_time")
                if update_fields:
                    order.save(update_fields=update_fields + ["update_time"])
                from promotion.services import on_order_paid

                on_order_paid(order)
                return True, "already_paid"

            if amount != order.total_pay:
                logger.error(
                    "支付金额不一致 order_id=%s expected=%s paid=%s trade_no=%s",
                    order_id,
                    order.total_pay,
                    amount,
                    trade_no,
                )
                return False, "amount_mismatch"

            order.pay_status = OrderInfo.STATUS_PAID
            order.trade_no = trade_no
            order.paid_amount = amount
            order.pay_time = paid_at
            order.save(
                update_fields=[
                    "pay_status",
                    "trade_no",
                    "paid_amount",
                    "pay_time",
                    "update_time",
                ]
            )
            from promotion.services import on_order_paid

            on_order_paid(order)
            logger.info("订单支付成功 order_id=%s trade_no=%s amount=%s", order_id, trade_no, amount)
            return True, "paid"
    except IntegrityError:
        logger.exception("支付流水号写入冲突 order_id=%s trade_no=%s", order_id, trade_no)
        return False, "trade_no_conflict"


def apply_result(result, expected_channel=None):
    """把渠道返回的 PaymentResult 落到订单上。"""
    if not result.paid:
        return True, "not_paid"
    return mark_paid(
        order_id=result.out_trade_no,
        trade_no=result.trade_no,
        paid_amount=result.amount,
        paid_at=result.paid_at,
        raw=result.raw,
        expected_channel=expected_channel,
    )


def refresh_order_status(order, force=False):
    """主动查询一次渠道并更新订单，返回刷新后的订单。

    force=False 时用缓存做短节流，避免前端轮询把渠道查询接口打爆。
    """
    if order.pay_status != OrderInfo.STATUS_UNPAID:
        return order

    try:
        provider = get_provider()
    except PaymentError as exc:
        logger.warning("支付渠道未配置，跳过主动查询 order_id=%s: %s", order.order_id, exc)
        return order
    if provider.name == "alipay" and order.pay_method != OrderInfo.PAY_ALIPAY:
        return order
    if provider.name == "mock":
        return order

    if not force:
        try:
            acquired = cache.add(
                f"payment:query:{order.order_id}",
                1,
                timeout=max(1, settings.PAYMENT_QUERY_THROTTLE_SECONDS),
            )
        except Exception:
            acquired = True
        if acquired is False:
            return order

    try:
        result = provider.query(order)
    except PaymentError as exc:
        logger.warning("主动查询支付结果失败 order_id=%s: %s", order.order_id, exc)
        return order
    except Exception:
        logger.exception("主动查询支付结果异常 order_id=%s", order.order_id)
        return order

    if result and result.paid:
        ok, reason = apply_result(result, expected_channel=provider.name)
        if not ok and reason != "already_paid":
            logger.warning(
                "主动查询到已支付但落库失败 order_id=%s reason=%s", order.order_id, reason
            )
    order.refresh_from_db()
    return order


def query_and_apply(order):
    """供 Celery 定时对账使用：强制查询并返回订单是否已支付。"""
    order = refresh_order_status(order, force=True)
    return order.pay_status == OrderInfo.STATUS_PAID
