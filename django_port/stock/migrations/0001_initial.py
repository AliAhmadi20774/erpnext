# Generated for the standalone Django port.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounting", "0014_alter_accountclosingbalance_options_and_more"),
        ("organizations", "0004_company_default_finance_book"),
        ("parties", "0002_customer_customer_primary_address_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="WarehouseType",
            fields=[
                ("name", models.CharField(max_length=140, primary_key=True, serialize=False)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "db_table": "warehouse_type",
                "ordering": ("name",),
            },
        ),
        migrations.CreateModel(
            name="Warehouse",
            fields=[
                ("name", models.CharField(max_length=140, primary_key=True, serialize=False)),
                ("warehouse_name", models.CharField(max_length=140)),
                ("is_group", models.BooleanField(default=False)),
                ("disabled", models.BooleanField(default=False)),
                ("is_rejected_warehouse", models.BooleanField(default=False)),
                ("email_id", models.EmailField(blank=True, max_length=254)),
                ("phone_no", models.CharField(blank=True, max_length=140)),
                ("mobile_no", models.CharField(blank=True, max_length=140)),
                ("address_line_1", models.CharField(blank=True, max_length=240)),
                ("address_line_2", models.CharField(blank=True, max_length=240)),
                ("city", models.CharField(blank=True, max_length=140)),
                ("state", models.CharField(blank=True, max_length=140)),
                ("pin", models.CharField(blank=True, max_length=20)),
                ("lft", models.PositiveIntegerField(db_index=True, default=0, editable=False)),
                ("rgt", models.PositiveIntegerField(db_index=True, default=0, editable=False)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warehouses",
                        to="accounting.account",
                    ),
                ),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warehouses",
                        to="organizations.company",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warehouses",
                        to="parties.customer",
                    ),
                ),
                (
                    "default_in_transit_warehouse",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="default_for_warehouses",
                        to="stock.warehouse",
                    ),
                ),
                (
                    "parent_warehouse",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="children",
                        to="stock.warehouse",
                    ),
                ),
                (
                    "warehouse_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warehouses",
                        to="stock.warehousetype",
                    ),
                ),
            ],
            options={
                "db_table": "warehouse",
                "ordering": ("company", "lft", "name"),
                "indexes": [
                    models.Index(
                        fields=["company", "parent_warehouse"],
                        name="warehouse_company_parent_idx",
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="warehouse",
            constraint=models.UniqueConstraint(
                fields=("company", "warehouse_name"),
                name="unique_warehouse_name_per_company",
            ),
        ),
    ]
