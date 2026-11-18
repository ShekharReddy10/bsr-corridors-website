"""The WhatsApp message that carries a UPI payment link ({payment_link}, for {amount})."""

from django.db import migrations

BODY = (
    "Dear {guest},\n\n"
    "Kindly make the payment of {amount} towards your stay in Room {room} at {hotel} using the secure UPI "
    "payment link below:\n{payment_link}\n\n"
    "Once the payment is complete, please tap “I have paid” on the same page to confirm.\n\n"
    "Thank you.")


def forwards(apps, schema_editor):
    MessageTemplate = apps.get_model("core", "MessageTemplate")
    if not MessageTemplate.objects.filter(name__iexact="Payment link").exists():
        last = MessageTemplate.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
        MessageTemplate.objects.create(name="Payment link", body=BODY, sort_order=last + 1)


def backwards(apps, schema_editor):
    apps.get_model("core", "MessageTemplate").objects.filter(name="Payment link", body=BODY).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0028_payment_requests")]
    operations = [migrations.RunPython(forwards, backwards)]
