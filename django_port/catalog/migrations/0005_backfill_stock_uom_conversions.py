from decimal import Decimal

from django.db import migrations


def backfill_stock_uom_conversions(apps, schema_editor):
    Item = apps.get_model("catalog", "Item")
    ItemUOMConversion = apps.get_model("catalog", "ItemUOMConversion")
    database = schema_editor.connection.alias
    rows = [
        ItemUOMConversion(
            item_id=item.pk,
            uom_id=item.stock_uom_id,
            conversion_factor=Decimal("1"),
        )
        for item in Item.objects.using(database).all()
    ]
    ItemUOMConversion.objects.using(database).bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("catalog", "0004_itemuomconversion")]
    operations = [migrations.RunPython(backfill_stock_uom_conversions, migrations.RunPython.noop)]
