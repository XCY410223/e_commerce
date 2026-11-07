"""订单状态服务：超时取消与库存回补。

取消流程必须在一个事务内锁定订单和整批 SKU，并按订单明细回补库存、扣回销量。
订单状态是幂等闸门：重复任务、重复回调或并发取消最终只会成功回补一次。
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from goods.models import GoodsSKU

from .models import OrderComment, OrderGoods, OrderInfo

logger = logging.getLogger(__name__)

CANCEL_CANCELLED = "cancelled"
CANCEL_ALREADY_CANCELLED = "already_cancelled"
CANCEL_PAID = "paid"
CANCEL_NOT_FOUND = "not_found"
CANCEL_NOT_CANCELLABLE = "not_cancellable"

COMMENT_NOT_FOUND = "not_found"
COMMENT_EMPTY = "empty"
COMMENT_TOO_LONG = "too_long"
COMMENT_NOT_PAID = "not_paid"
COMMENT_ALREADY_EXISTS = "already_commented"
COMMENT_REMARK_TOO_LONG = "remark_too_long"


class CommentError(Exception):
    """评论提交或审核校验失败。"""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _normalize_comment_content(content):
    content = (content or "").strip()
    if not content:
        raise CommentError(COMMENT_EMPTY, "请输入评论内容")
    if len(content) > OrderComment.MAX_CONTENT_LENGTH:
        raise CommentError(
            COMMENT_TOO_LONG,
            f"评论内容不能超过 {OrderComment.MAX_CONTENT_LENGTH} 个字符",
        )
    return content


def submit_order_comment(user, order_goods_id, content):
    """提交或重新提交一条订单商品评论。

    只有订单所有者、已支付订单可以评论；每条订单商品只保留一条评论。
    审核通过后不允许修改；被驳回后允许修改并重新进入待审核状态。
    """
    content = _normalize_comment_content(content)

    with transaction.atomic():
        line = (
            OrderGoods.objects.select_for_update()
            .select_related("order")
            .filter(pk=order_goods_id, order__user=user)
            .first()
        )
        if line is None:
            raise CommentError(COMMENT_NOT_FOUND, "订单商品不存在")
        if line.order.pay_status != OrderInfo.STATUS_PAID:
            raise CommentError(COMMENT_NOT_PAID, "只有已支付订单可以评论")

        review = (
            OrderComment.all_objects.select_for_update()
            .filter(order_goods=line)
            .first()
        )
        if (
            review is not None
            and not review.is_delete
            and review.status != OrderComment.STATUS_REJECTED
        ):
            raise CommentError(
                COMMENT_ALREADY_EXISTS, "该商品已评论，不能重复提交"
            )

        if review is None:
            review = OrderComment.objects.create(
                order_goods=line,
                content=content,
            )
            created = True
        else:
            review.content = content
            review.status = OrderComment.STATUS_PENDING
            review.audit_remark = ""
            review.audited_by = None
            review.audited_at = None
            review.is_delete = False
            review.save()
            created = False

    logger.info(
        "订单评论已提交 comment_id=%s order_id=%s line_id=%s created=%s",
        review.pk,
        line.order.order_id,
        line.pk,
        created,
    )
    return review, created


def audit_order_comment(comment, *, auditor, approved, remark=""):
    """审核评论；仅后台工作人员可调用。"""
    if not getattr(auditor, "is_staff", False):
        raise PermissionError("只有后台工作人员可以审核评论")

    remark = (remark or "").strip()
    if len(remark) > OrderComment.MAX_AUDIT_REMARK_LENGTH:
        raise CommentError(
            COMMENT_REMARK_TOO_LONG,
            f"审核备注不能超过 {OrderComment.MAX_AUDIT_REMARK_LENGTH} 个字符",
        )

    with transaction.atomic():
        locked = (
            OrderComment.objects.select_for_update()
            .filter(pk=comment.pk)
            .first()
        )
        if locked is None:
            raise CommentError(COMMENT_NOT_FOUND, "评论不存在")
        locked.status = (
            OrderComment.STATUS_APPROVED
            if approved
            else OrderComment.STATUS_REJECTED
        )
        locked.audit_remark = remark
        locked.audited_by = auditor
        locked.audited_at = timezone.now()
        locked.save(
            update_fields=[
                "status",
                "audit_remark",
                "audited_by",
                "audited_at",
                "update_time",
            ]
        )
        comment = locked

    logger.info(
        "订单评论已审核 comment_id=%s status=%s auditor_id=%s",
        comment.pk,
        comment.status,
        getattr(auditor, "pk", None),
    )
    return comment


def visible_comments_for_sku(sku_id, limit=20):
    """返回商品详情页可展示的已审核评论。"""
    return list(
        OrderComment.objects.filter(
            status=OrderComment.STATUS_APPROVED,
            order_goods__sku_id=sku_id,
            order_goods__is_delete=False,
            order_goods__order__is_delete=False,
            order_goods__order__pay_status=OrderInfo.STATUS_PAID,
        )
        .select_related("order_goods__order__user")
        .order_by("-create_time")[:limit]
    )


def _cancel_locked_order(order, now=None):
    """取消已锁定的订单并回补库存；调用方必须处于事务中。"""
    if order.pay_status == OrderInfo.STATUS_PAID:
        return CANCEL_PAID
    if order.pay_status == OrderInfo.STATUS_CANCELLED:
        return CANCEL_ALREADY_CANCELLED
    if order.pay_status != OrderInfo.STATUS_UNPAID:
        return CANCEL_NOT_CANCELLABLE

    lines = list(
        OrderGoods.all_objects.filter(order=order)
        .order_by("sku_id")
        .values("sku_id", "count")
    )
    sku_ids = sorted({line["sku_id"] for line in lines})
    locked = {
        sku.id: sku
        for sku in GoodsSKU.all_objects.select_for_update()
        .filter(id__in=sku_ids)
        .order_by("id")
    }
    if len(locked) != len(sku_ids):
        missing = sorted(set(sku_ids) - set(locked))
        raise RuntimeError(f"取消订单时找不到商品 SKU：{missing}")

    restored_count = 0
    for line in lines:
        count = max(0, int(line["count"]))
        if not count:
            continue
        sku = locked[line["sku_id"]]
        sku.stock += count
        sku.sales = max(0, sku.sales - count)
        sku.save(update_fields=["stock", "sales", "update_time"])
        restored_count += count

    now = now or timezone.now()
    order.pay_status = OrderInfo.STATUS_CANCELLED
    order.cancel_time = now
    order.save(update_fields=["pay_status", "cancel_time", "update_time"])
    from promotion.services import release_coupon_for_order

    release_coupon_for_order(order, at=now)
    logger.info(
        "订单已取消并回补库存 order_id=%s items=%s", order.order_id, restored_count
    )
    return CANCEL_CANCELLED


def cancel_order(order_id, *, now=None):
    """取消指定未支付订单，返回结果标识；已支付订单不会被取消。"""
    with transaction.atomic():
        order = (
            OrderInfo.objects.select_for_update()
            .filter(order_id=order_id)
            .first()
        )
        if order is None:
            return CANCEL_NOT_FOUND
        return _cancel_locked_order(order, now=now)


def cancel_expired_orders(
    limit=None,
    timeout_seconds=None,
    *,
    dry_run=False,
    restrict_order_ids=None,
):
    """扫描并取消超时未支付订单，返回本次处理统计。"""
    if limit is None:
        limit = getattr(settings, "ORDER_CANCEL_BATCH_SIZE", 100)
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 100

    if timeout_seconds is None:
        timeout_seconds = getattr(settings, "ORDER_UNPAID_TIMEOUT_SECONDS", 1800)
    try:
        timeout_seconds = max(0, int(timeout_seconds))
    except (TypeError, ValueError):
        timeout_seconds = 1800

    cutoff = timezone.now() - timedelta(seconds=timeout_seconds)
    queryset = OrderInfo.objects.filter(
        pay_status=OrderInfo.STATUS_UNPAID,
        create_time__lte=cutoff,
    ).order_by("create_time")
    if restrict_order_ids is not None:
        queryset = queryset.filter(order_id__in=list(restrict_order_ids))

    order_ids = list(queryset.values_list("order_id", flat=True)[:limit])
    stats = {
        "checked": len(order_ids),
        "cancelled": 0,
        "paid": 0,
        "already_cancelled": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return stats

    for order_id in order_ids:
        try:
            outcome = cancel_order(order_id)
        except Exception:
            stats["failed"] += 1
            logger.exception("超时取消订单失败 order_id=%s", order_id)
            continue

        if outcome == CANCEL_CANCELLED:
            stats["cancelled"] += 1
        elif outcome == CANCEL_PAID:
            stats["paid"] += 1
        elif outcome == CANCEL_ALREADY_CANCELLED:
            stats["already_cancelled"] += 1
        else:
            stats["failed"] += 1
            logger.warning("超时订单未取消 order_id=%s outcome=%s", order_id, outcome)

    return stats
