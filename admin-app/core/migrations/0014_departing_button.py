from django.db import migrations, models


def mark_checkout_details(apps, schema_editor):
    MessageTemplate = apps.get_model("core", "MessageTemplate")
    t = MessageTemplate.objects.filter(name__iexact="Check-out details").first()
    if t:
        t.on_departing = True
        t.save(update_fields=["on_departing"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_monthly_settlements"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagetemplate",
            name="on_departing",
            field=models.BooleanField(default=False, help_text="Shows as a one-tap button next to Check out on the Today page (only one message can)", verbose_name="Button on “Departing today”"),
        ),
        migrations.RunPython(mark_checkout_details, migrations.RunPython.noop),
    ]
