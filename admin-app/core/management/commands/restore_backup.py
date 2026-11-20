"""Restore a backup downloaded from Google Drive (.json.gz, or an older .json).

    python manage.py restore_backup backup_06-10-2026_02-00AM.json.gz --yes

Needs the same FIELD_ENCRYPTION_KEY that was used when the backup was made.
Existing rows with the same IDs are overwritten; nothing else is deleted. Logins and settings are matched by username
and key instead (the admin login, created on every deploy, may have a different ID). All or nothing: if any record
fails, nothing is restored.
"""

import gzip

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.management.color import no_style
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from ...models import AppSetting


def read(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    return (gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data).decode()


class Command(BaseCommand):
    help = "Restore everything from a JSON backup file (.json.gz)."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--yes", action="store_true", help="Confirm the restore.")

    def handle(self, path, yes, **options):
        if not yes:
            raise CommandError("This overwrites matching records. Re-run with --yes to confirm.")
        User = get_user_model()
        users = dict(User.objects.values_list("username", "pk"))
        settings_ = dict(AppSetting.objects.values_list("key", "pk"))
        count = 0
        with transaction.atomic():
            for obj in serializers.deserialize("json", read(path)):
                o = obj.object
                if isinstance(o, User) and o.username in users:
                    o.pk = users[o.username]
                elif isinstance(o, AppSetting) and o.key in settings_:
                    o.pk = settings_[o.key]
                obj.save()
                count += 1
            # Postgres: move each table's ID counter past the restored rows, or the next new record clashes with one.
            models = [*apps.get_app_config("core").get_models(), *apps.get_app_config("auth").get_models()]
            with connection.cursor() as cur:
                for sql in connection.ops.sequence_reset_sql(no_style(), models):
                    cur.execute(sql)
        self.stdout.write(self.style.SUCCESS(f"Restore complete ({count} records)."))
