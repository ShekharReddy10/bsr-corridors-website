"""Guests checked out before check-out freed the room by itself (f9d521c) may still hold nights after they left —
e.g. checked out on their arrival day. Free those rooms now: from the moment the guest checked out."""

from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    Stay = apps.get_model("core", "Stay")
    stays = Stay.objects.using(schema_editor.connection.alias)
    for s in stays.filter(status="checked_out", room_released_at__isnull=True, check_out__isnull=False):
        left = s.checked_out_at or timezone.now()
        if s.check_out > timezone.localtime(left).date():
            stays.filter(pk=s.pk).update(room_released_at=left)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0033_file_kinds'),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
