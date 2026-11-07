"""促销运行状态巡检：供发布核验、监控定时任务和故障排查复用。"""

import json
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, F
from django.utils import timezone

from goods.models import IndexPromotionBanner
from order.models import OrderInfo
from promotion.models import OrderPromotion, Promotion, Referral, UserCoupon


class Command(BaseCommand):
    help = "输出促销开关、规则、首页入口、券、邀请和近 N 小时订单金额巡检结果"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24, help="订单与优惠快照统计窗口，默认 24")
        parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于接入监控")
        parser.add_argument(
            "--strict",
            action="store_true",
            help="发现入口未关联、入口指向失效规则或订单金额不一致时以非零状态退出",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        hours = max(1, int(options["hours"]))
        since = now - timedelta(hours=hours)

        rules = list(Promotion.objects.order_by("priority", "id"))
        active_rules = [rule for rule in rules if rule.is_effective(now)]
        banners = list(
            IndexPromotionBanner.objects.select_related("promotion").order_by("index", "id")
        )
        unlinked_banners = [banner for banner in banners if banner.promotion_id is None]
        # 未来才开始的周末活动仍可提前挂首页入口，不算异常；
        # 只有入口仍在展示、规则却已停用/过期/软删除时才需要告警。
        stale_banners = [
            banner
            for banner in banners
            if banner.is_active
            and banner.promotion_id is not None
            and (
                banner.promotion.is_delete
                or not banner.promotion.is_active
                or (
                    banner.promotion.end_at is not None
                    and banner.promotion.end_at < now
                )
            )
        ]

        coupon_counts = {
            status: 0 for status, _ in UserCoupon.STATUS_CHOICES
        }
        for row in UserCoupon.objects.values("status").annotate(total=Count("id")):
            coupon_counts[row["status"]] = row["total"]
        expired_available = UserCoupon.objects.filter(
            status=UserCoupon.STATUS_AVAILABLE,
            expires_at__lte=now,
        ).count()

        referral_counts = {
            status: 0 for status, _ in Referral.STATUS_CHOICES
        }
        for row in Referral.objects.values("status").annotate(total=Count("id")):
            referral_counts[row["status"]] = row["total"]

        recent_orders = OrderInfo.objects.filter(create_time__gte=since)
        amount_mismatches = list(
            recent_orders.filter(
                original_total_amount__isnull=False,
                total_discount__isnull=False,
            )
            .annotate(computed_total=F("original_total_amount") - F("total_discount"))
            .exclude(computed_total=F("total_amount") + F("transit_price"))
            .values_list("order_id", flat=True)[:20]
        )
        mismatch_count = (
            recent_orders.filter(
                original_total_amount__isnull=False,
                total_discount__isnull=False,
            )
            .annotate(computed_total=F("original_total_amount") - F("total_discount"))
            .exclude(computed_total=F("total_amount") + F("transit_price"))
            .count()
        )

        anomalies = []
        if unlinked_banners:
            anomalies.append(
                "首页活动入口未关联算价规则：" + ", ".join(b.slug for b in unlinked_banners)
            )
        if stale_banners:
            anomalies.append(
                "首页活动入口指向未生效规则：" + ", ".join(b.slug for b in stale_banners)
            )
        if mismatch_count:
            anomalies.append(f"近 {hours} 小时订单金额快照不一致：{mismatch_count} 单")

        payload = {
            "checked_at": now.isoformat(),
            "window_hours": hours,
            "switches": {
                "pricing": bool(getattr(settings, "PROMOTION_PRICING_ENABLED", True)),
                "coupon": bool(getattr(settings, "PROMOTION_COUPON_ENABLED", True)),
                "referral": bool(getattr(settings, "PROMOTION_REFERRAL_ENABLED", True)),
                "standard_shipping": str(getattr(settings, "STANDARD_TRANSIT_PRICE", "10.00")),
            },
            "rules": {
                "total": len(rules),
                "active": len(active_rules),
                "items": [
                    {
                        "slug": rule.slug,
                        "name": rule.name,
                        "kind": rule.kind,
                        "active": rule.is_effective(now),
                        "start_at": rule.start_at.isoformat() if rule.start_at else None,
                        "end_at": rule.end_at.isoformat() if rule.end_at else None,
                    }
                    for rule in rules
                ],
            },
            "banners": {
                "total": len(banners),
                "unlinked": [banner.slug for banner in unlinked_banners],
                "stale": [banner.slug for banner in stale_banners],
            },
            "coupons": {
                "by_status": coupon_counts,
                "available_but_expired": expired_available,
            },
            "referrals": {"by_status": referral_counts},
            "orders": {
                "window_total": recent_orders.count(),
                "promotion_snapshots": OrderPromotion.objects.filter(
                    order__create_time__gte=since
                ).count(),
                "amount_mismatches": mismatch_count,
                "mismatch_order_ids": amount_mismatches,
            },
            "anomalies": anomalies,
        }

        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            switches = payload["switches"]
            self.stdout.write(f"促销运行状态 @ {payload['checked_at']}")
            self.stdout.write(
                "开关 "
                f"pricing={'ON' if switches['pricing'] else 'OFF'} "
                f"coupon={'ON' if switches['coupon'] else 'OFF'} "
                f"referral={'ON' if switches['referral'] else 'OFF'} "
                f"shipping={switches['standard_shipping']}"
            )
            self.stdout.write(
                f"规则 总数={len(rules)} 生效={len(active_rules)}；"
                f"首页入口={len(banners)} 未关联={len(unlinked_banners)} 失效={len(stale_banners)}"
            )
            for rule in rules:
                state = "ON" if rule.is_effective(now) else "OFF"
                self.stdout.write(f"  [{state}] {rule.slug} {rule.name} ({rule.kind})")
            self.stdout.write(
                "优惠券 "
                + " ".join(f"{key}={value}" for key, value in coupon_counts.items())
                + f" available_but_expired={expired_available}"
            )
            self.stdout.write(
                "邀请 "
                + " ".join(f"{key}={value}" for key, value in referral_counts.items())
            )
            self.stdout.write(
                f"近 {hours} 小时 订单={payload['orders']['window_total']} "
                f"优惠快照={payload['orders']['promotion_snapshots']} "
                f"金额不一致={mismatch_count}"
            )
            if anomalies:
                self.stdout.write(self.style.WARNING("异常：" + "；".join(anomalies)))
            else:
                self.stdout.write(self.style.SUCCESS("未发现配置或金额快照异常"))

        if options["strict"] and anomalies:
            raise CommandError("促销巡检发现异常，请按输出排查")
