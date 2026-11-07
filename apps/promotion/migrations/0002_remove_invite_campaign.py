"""下线“邀请好友得优惠券”活动，保留历史邀请与奖励数据用于审计。"""

from django.db import migrations
from django.utils import timezone


def disable_invite_campaign(apps, schema_editor):
    Banner = apps.get_model("goods", "IndexPromotionBanner")
    Promotion = apps.get_model("promotion", "Promotion")
    now = timezone.now()

    # 首页入口软删除，避免用户继续进入已下线活动页；历史记录仍在库中。
    Banner.objects.filter(slug="invite-friends").update(
        is_active=False,
        is_delete=True,
        promotion=None,
        update_time=now,
    )
    # 规则停用并软删除。UserCoupon 对规则是 PROTECT，这里不物理删除，
    # 已发券和历史订单快照仍可追溯。
    Promotion.objects.filter(slug="invite-reward").update(
        is_active=False,
        is_delete=True,
        update_time=now,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("promotion", "0001_initial"),
        ("goods", "0007_indexpromotionbanner_promotion"),
    ]

    operations = [
        migrations.RunPython(disable_invite_campaign, migrations.RunPython.noop),
    ]
