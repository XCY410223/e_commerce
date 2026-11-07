from django.db import migrations

INDEX_NAME = "ft_goods_sku_name_desc"
TABLE_NAME = "df_goods_sku"
OLD_COLUMNS = ("name", "desc")
NEW_COLUMNS = ("name", "desc", "spec")


def _index_columns(cursor):
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        ORDER BY seq_in_index
        """,
        [TABLE_NAME, INDEX_NAME],
    )
    return tuple(row[0] for row in cursor.fetchall())


def _drop_index(cursor):
    cursor.execute(f"ALTER TABLE `{TABLE_NAME}` DROP INDEX `{INDEX_NAME}`")


def _add_index(cursor, columns):
    column_sql = ", ".join(f"`{name}`" for name in columns)
    cursor.execute(
        f"ALTER TABLE `{TABLE_NAME}` ADD FULLTEXT INDEX `{INDEX_NAME}` "
        f"({column_sql}) WITH PARSER ngram"
    )


def add_spec_to_fulltext(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        columns = _index_columns(cursor)
        if columns == NEW_COLUMNS:
            return
        if columns:
            _drop_index(cursor)
        _add_index(cursor, NEW_COLUMNS)


def remove_spec_from_fulltext(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        columns = _index_columns(cursor)
        if columns == OLD_COLUMNS:
            return
        if columns:
            _drop_index(cursor)
        _add_index(cursor, OLD_COLUMNS)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("goods", "0004_goodssku_fulltext"),
    ]

    operations = [
        migrations.RunPython(add_spec_to_fulltext, remove_spec_from_fulltext),
    ]
