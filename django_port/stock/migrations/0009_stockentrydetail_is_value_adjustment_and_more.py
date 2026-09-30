from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("stock", "0008_stockreconciliation_total_value_difference_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockentrydetail",
            name="is_value_adjustment",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="stockreconciliationitem",
            name="direct_value_adjustment",
            field=models.BooleanField(default=False),
        ),
    ]
