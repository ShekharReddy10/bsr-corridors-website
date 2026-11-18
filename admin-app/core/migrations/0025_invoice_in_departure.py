"""The check-out (departure) message includes the guest's invoice link."""

from django.db import migrations

ADD = "Your invoice ({invoice_no}) for this stay is available here:\n{invoice_link}\n\n"
AFTER = "You have been checked out of Room {room}.\n\n"


def forwards(apps, schema_editor):
    MessageTemplate = apps.get_model("core", "MessageTemplate")
    for t in MessageTemplate.objects.filter(on_check_out=True):
        if "{invoice_link}" in t.body:
            continue
        t.body = t.body.replace(AFTER, AFTER + ADD, 1) if AFTER in t.body else f"{t.body.rstrip()}\n\n{ADD.strip()}"
        t.save(update_fields=["body"])


def backwards(apps, schema_editor):
    MessageTemplate = apps.get_model("core", "MessageTemplate")
    for t in MessageTemplate.objects.filter(body__contains="{invoice_link}"):
        t.body = t.body.replace(ADD, "").replace(f"\n\n{ADD.strip()}", "")
        t.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("core", "0024_formal_messages")]
    operations = [migrations.RunPython(forwards, backwards)]
