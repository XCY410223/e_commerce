from django.db import migrations

INDEX_NAME = "ft_goods_sku_name_desc"
TABLE_NAME = "df_goods_sku"


def _index_exists(cursor):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        """,
        [TABLE_NAME, INDEX_NAME],
    )
    return cursor.fetchone()[0] > 0


def create_fulltext_index(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        if not _index_exists(cursor):
            cursor.execute(
                f"ALTER TABLE `{TABLE_NAME}` ADD FULLTEXT INDEX `{INDEX_NAME}` "
                "(`name`, `desc`) WITH PARSER ngram"
            )


def drop_fulltext_index(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        if _index_exists(cursor):
            cursor.execute(
                f"ALTER TABLE `{TABLE_NAME}` DROP INDEX `{INDEX_NAME}`"
            )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("goods", "0003_goodssku_spec"),
    ]

    operations = [
        migrations.RunPython(create_fulltext_index, drop_fulltext_index),
    ]
