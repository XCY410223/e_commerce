from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("order", "0004_ordergoods_sku_name_orderinfo_address_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderinfo",
            name="cancel_time",
            field=models.DateTimeField(blank=True, null=True, verbose_name="取消时间"),
        ),
        migrations.AlterField(
            model_name="orderinfo",
            name="pay_status",
            field=models.SmallIntegerField(
                choices=[(0, "未支付"), (1, "已支付"), (2, "已取消")],
                default=0,
                verbose_name="支付状态",
            ),
        ),
        migrations.AddIndex(
            model_name="orderinfo",
            index=models.Index(
                fields=["pay_status", "create_time"], name="idx_order_pay_time"
            ),
        ),
    ]
