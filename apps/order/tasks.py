"""订单相关的 Celery 异步任务。"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from . import payment
from .models import OrderInfo
from .services import cancel_expired_orders as cancel_expired_orders_service

logger = logging.getLogger(__name__)


@shared_task(name="order.cancel_expired_orders")
def cancel_expired_orders(limit=None):
    """定时取消超时未支付订单，并回补库存、扣回销量。"""
    result = cancel_expired_orders_service(
        limit=limit or getattr(settings, "ORDER_CANCEL_BATCH_SIZE", 100)
    )
    if result["cancelled"] or result["failed"]:
        logger.info("超时订单处理完成 %s", result)
    return result


@shared_task(name="order.query_unpaid_orders")
def query_unpaid_orders(limit=100):
    """主动查询未支付订单，补偿丢失的异步通知。

    只处理创建一段时间后的订单，避免用户刚打开收银台就被频繁查询；同时限制
    最大追溯天数，避免历史订单无限占用渠道查询额度。
    """
    if (settings.PAYMENT_PROVIDER or "mock").lower() == "mock":
        return {"checked": 0, "paid": 0, "skipped": "mock_provider"}

    now = timezone.now()
    cutoff = now - timedelta(seconds=max(0, settings.PAYMENT_QUERY_AFTER_SECONDS))
    oldest = now - timedelta(days=max(1, settings.PAYMENT_QUERY_MAX_AGE_DAYS))
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 100

    orders = list(
        OrderInfo.objects.filter(
            pay_status=OrderInfo.STATUS_UNPAID,
            create_time__lte=cutoff,
            create_time__gte=oldest,
        ).order_by("create_time")[:limit]
    )

    checked = 0
    paid = 0
    for order in orders:
        checked += 1
        try:
            if payment.query_and_apply(order):
                paid += 1
        except Exception:
            logger.exception("订单支付结果对账失败 order_id=%s", order.order_id)

    return {"checked": checked, "paid": paid}
