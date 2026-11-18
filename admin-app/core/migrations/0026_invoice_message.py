"""Invoice link in the "Check-out details" message, and an "Invoice" message (the button next to the invoice)."""

from django.db import migrations

LINK = "Your invoice ({invoice_no}):\n{invoice_link}\n\n"
AFTER = "Balance due: {balance}\n\n"
INVOICE = (
    "Dear {guest},\n\n"
    "Please find below the invoice ({invoice_no}) for your stay in Room {room} at {hotel}:\n"
    "{invoice_link}\n\n"
    "Total amount: {total}\n"
    "Amount paid: {paid}\n"
    "Balance due: {balance}\n\n"
    "Thank you for choosing {hotel}.")


def forwards(apps, schema_editor):
    MessageTemplate = apps.get_model("core", "MessageTemplate")
    t = MessageTemplate.objects.filter(name__iexact="Check-out details").first()
    if t and "{invoice_link}" not in t.body:
        t.body = t.body.replace(AFTER, AFTER + LINK, 1) if AFTER in t.body else f"{t.body.rstrip()}\n\n{LINK.strip()}"
        t.save(update_fields=["body"])
    if not MessageTemplate.objects.filter(name__iexact="Invoice").exists():
        last = MessageTemplate.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
        MessageTemplate.objects.create(name="Invoice", body=INVOICE, sort_order=last + 1)


class Migration(migrations.Migration):
    dependencies = [("core", "0025_invoice_in_departure")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
