# Generated for the standalone Django port.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0004_company_default_finance_book"),
        ("stock", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="enable_perpetual_inventory",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="company",
            name="default_inventory_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="default_inventory_for_companies",
                to="accounting.account",
            ),
        ),
        migrations.AddField(
            model_name="company",
            name="default_warehouse",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="default_for_companies",
                to="stock.warehouse",
            ),
        ),
        migrations.AddField(
            model_name="company",
            name="default_in_transit_warehouse",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="default_in_transit_for_companies",
                to="stock.warehouse",
            ),
        ),
    ]
