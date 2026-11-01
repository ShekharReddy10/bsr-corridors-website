from django.db import migrations, models
from django.utils import timezone


def set_month(apps, schema_editor):
    Settlement = apps.get_model("core", "Settlement")
    for s in Settlement.objects.all():
        s.month = timezone.localtime(s.at).date().replace(day=1)
        s.save(update_fields=["month"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_departure_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="settlement",
            name="month",
            field=models.DateField(db_index=True, null=True, help_text="First day of the month this settlement is for"),
        ),
        migrations.RunPython(set_month, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="settlement",
            name="month",
            field=models.DateField(db_index=True, help_text="First day of the month this settlement is for"),
        ),
        migrations.AlterField(
            model_name="settlement",
            name="received_total",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AlterField(
            model_name="settlement",
            name="expenses_total",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AlterModelOptions(
            name="settlement",
            options={"ordering": ["-month", "-at", "-pk"]},
        ),
    ]
