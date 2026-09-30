from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("stock", "0009_stockentrydetail_is_value_adjustment_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockledgerentry",
            name="is_value_reset",
            field=models.BooleanField(default=False),
        ),
    ]
