import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models
from django.utils import timezone


def opening_payments(apps, schema_editor):
    """Money already recorded on stays becomes one payment each, dated the day the stay was entered."""
    Stay = apps.get_model("core", "Stay")
    Payment = apps.get_model("core", "Payment")
    Payment.objects.bulk_create([
        Payment(stay=s, date=timezone.localtime(s.created_at).date(), amount=s.amount_paid, mode=s.payment_mode,
                collected_by=s.host, note="Paid before payment history was kept")
        for s in Stay.objects.exclude(amount_paid=0)
    ])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_departing_button"),
    ]

    operations = [
        migrations.CreateModel(
            name="Payment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(db_index=True, default=django.utils.timezone.localdate)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10, verbose_name="Amount (₹)")),
                ("mode", models.CharField(blank=True, choices=[("cash", "Cash"), ("upi", "UPI"), ("card", "Card"), ("bank", "Bank transfer"), ("other", "Other")], max_length=20)),
                ("collected_by", models.CharField(blank=True, max_length=40, verbose_name="Collected by")),
                ("note", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("stay", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="core.stay")),
            ],
            options={"db_table": "payments", "ordering": ["date", "pk"]},
        ),
        migrations.RunPython(opening_payments, migrations.RunPython.noop),
    ]
