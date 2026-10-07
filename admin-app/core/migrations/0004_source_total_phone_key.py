"""Booking source + reference, a single total amount per stay, and one guest per phone number."""

from django.db import migrations, models


def fill_phone_keys(apps, schema_editor):
    Guest = apps.get_model("core", "Guest")
    seen = set()
    for g in Guest.objects.order_by("-updated_at"):
        key = "".join(c for c in g.phone if c.isdigit())[-10:]
        # Older duplicates (same number) get a suffix so the unique constraint can be added;
        # they keep their stays and can be merged by editing.
        while key in seen:
            key = f"{key}-{g.pk}"
        seen.add(key)
        g.phone_key = key
        g.save(update_fields=["phone_key"])


def stay_totals(apps, schema_editor):
    """Old stays stored a nightly rate; turn it into the total for the stay."""
    Stay = apps.get_model("core", "Stay")
    for s in Stay.objects.all():
        s.total_amount = (s.total_amount or 0) * (s.check_out - s.check_in).days
        s.save(update_fields=["total_amount"])


class Migration(migrations.Migration):
    dependencies = [("core", "0003_rename_tables")]

    operations = [
        migrations.RemoveField(model_name="roomtype", name="default_rate"),
        migrations.AlterField(
            model_name="guest",
            name="phone",
            field=models.CharField(help_text="With country code, e.g. +91 98765 43210", max_length=20),
        ),
        migrations.AddField(
            model_name="guest",
            name="phone_key",
            field=models.CharField(editable=False, max_length=15, null=True),
        ),
        migrations.RunPython(fill_phone_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="guest",
            name="phone_key",
            field=models.CharField(editable=False, max_length=15, unique=True),
        ),
        migrations.RenameField(model_name="stay", old_name="nightly_rate", new_name="total_amount"),
        migrations.RunPython(stay_totals, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="stay",
            name="total_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name="Total amount (₹)"),
        ),
        migrations.AddField(
            model_name="stay",
            name="source",
            field=models.CharField(
                choices=[
                    ("walk_in", "Walk-in"),
                    ("direct", "Direct (call / WhatsApp)"),
                    ("airbnb", "Airbnb"),
                    ("booking_com", "Booking.com"),
                    ("makemytrip", "MakeMyTrip"),
                    ("other", "Other"),
                ],
                db_index=True,
                default="walk_in",
                max_length=20,
                verbose_name="Booked via",
            ),
        ),
        migrations.AddField(
            model_name="stay",
            name="source_ref",
            field=models.CharField(
                blank=True, help_text="e.g. Airbnb / Booking.com confirmation code", max_length=60,
                verbose_name="Booking reference",
            ),
        ),
    ]
