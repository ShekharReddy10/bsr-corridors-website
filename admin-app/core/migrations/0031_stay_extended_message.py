"""The WhatsApp message that opens after a stay is extended: the extra nights, their amount, any earlier balance and
the total to pay."""

from django.db import migrations

BODY = (
    "Dear {guest},\n\n"
    "Your stay in Room {room} at {hotel} has been extended.\n\n"
    "Extended stay: {ext_from} to {ext_to} ({ext_nights} night(s))\n"
    "Amount for the extended stay: {ext_amount}\n"
    "Previous balance: {prev_balance}\n"
    "Total amount to be paid: {total_due}\n\n"
    "Kindly make the payment at your earliest convenience.\n\n"
    "Thank you.")


def forwards(apps, schema_editor):
    MessageTemplate = apps.get_model("core", "MessageTemplate")
    if not MessageTemplate.objects.filter(name__iexact="Stay extended").exists():
        last = MessageTemplate.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
        MessageTemplate.objects.create(name="Stay extended", body=BODY, sort_order=last + 1)


def backwards(apps, schema_editor):
    apps.get_model("core", "MessageTemplate").objects.filter(name="Stay extended", body=BODY).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0030_short_links")]
    operations = [migrations.RunPython(forwards, backwards)]
