"""计划书约定首期活动的默认配置，供演示数据和初始化命令复用。"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import Promotion


def next_weekend_window(now=None):
    """返回最近一个周末窗口：周六 00:00 至周日 23:59:59。"""
    now = now or timezone.localtime()
    weekday = now.weekday()  # 周一 0，周日 6
    if weekday <= 4:
        saturday = (now + timedelta(days=5 - weekday)).date()
    elif weekday == 5:
        saturday = now.date()
    else:
        saturday = (now - timedelta(days=1)).date()
    sunday = saturday + timedelta(days=1)
    tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(saturday, time.min), tz),
        timezone.make_aware(datetime.combine(sunday, time(23, 59, 59)), tz),
    )


def default_promotion_rules(now=None):
    weekend_start, weekend_end = next_weekend_window(now)
    return [
        {
            "slug": "new-customer",
            "name": "新客立减 20 元",
            "kind": Promotion.KIND_NEW_CUSTOMER,
            "is_active": True,
            "priority": 10,
            "stackable": False,
            "discount_amount": "20.00",
            "min_amount": "0.00",
            "coupon_valid_days": 30,
            "start_at": None,
            "end_at": None,
        },
        {
            "slug": "free-shipping",
            "name": "生鲜满 99 免运费",
            "kind": Promotion.KIND_FREE_SHIPPING,
            "is_active": True,
            "priority": 20,
            "stackable": True,
            "discount_amount": "10.00",
            "shipping_threshold": "99.00",
            "start_at": None,
            "end_at": None,
        },
        {
            "slug": "weekend-sale",
            "name": "周末限时 5 折",
            "kind": Promotion.KIND_ITEM_DISCOUNT,
            "is_active": True,
            "priority": 30,
            "stackable": False,
            "discount_rate": "0.5000",
            "start_at": weekend_start,
            "end_at": weekend_end,
        },
    ]
