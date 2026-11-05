from django.db import migrations, models

TOMORROW = (
    "Hi {guest}, a reminder that your check-out from Room {room} is tomorrow, {check_out}, by {check_out_time}.\n\n"
    "Balance due: {balance}. Need a late check-out or an extra night? Just reply here."
)
REVIEW = (
    "Thank you for staying at {hotel}, {guest}! 🙏\n\n"
    "If you enjoyed your stay, a quick Google review would mean a lot to us: {review_link}\n\n"
    "Hope to host you again soon."
)


def set_shortcuts(apps, schema_editor):
    MessageTemplate = apps.get_model("core", "MessageTemplate")
    MessageTemplate.objects.filter(on_departing=True).update(shortcut="departing")
    reminder = MessageTemplate.objects.filter(name__iexact="Payment reminder").first()
    if reminder:
        reminder.shortcut = "balance"
        reminder.save(update_fields=["shortcut"])
    last = (MessageTemplate.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0) + 1
    MessageTemplate.objects.create(name="Check-out reminder (tomorrow)", body=TOMORROW, shortcut="tomorrow", sort_order=last)
    MessageTemplate.objects.create(name="Review request", body=REVIEW, shortcut="checked_out", sort_order=last + 1)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_payments"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagetemplate",
            name="shortcut",
            field=models.CharField(blank=True, choices=[("departing", "Departing today (next to Check out)"), ("tomorrow", "Departing tomorrow"), ("balance", "Balance due"), ("checked_out", "Checked out today")], help_text="Shows this message as a button next to each guest in that list (one message per list)", max_length=20, verbose_name="One-tap button on the Today page"),
        ),
        migrations.RunPython(set_shortcuts, migrations.RunPython.noop),
        migrations.RemoveField(model_name="messagetemplate", name="on_departing"),
    ]
