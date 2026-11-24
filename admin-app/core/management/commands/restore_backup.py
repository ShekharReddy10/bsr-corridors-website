"""Restore a backup downloaded from Google Drive (.json.gz, or an older .json).

    python manage.py restore_backup backup_06-10-2026_02-00AM.json.gz --yes
    python manage.py restore_backup backup_….json.gz --item pgone --yes     (one cluster item, the others untouched)

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
from django.db import connections, transaction

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
        parser.add_argument("--item", default="", help="Cluster item code (default: BSR Corridors).")

    def handle(self, path, yes, item="", **options):
        if not yes:
            raise CommandError("This overwrites matching records. Re-run with --yes to confirm.")
        from cluster import current
        from cluster.models import Item
        target = Item.objects.filter(code=item).first() if item else Item.objects.filter(is_default=True).first()
        if item and (target is None or target.is_default):
            raise CommandError(f"No cluster item {item!r}.")
        if target is None:  # before the cluster existed
            return self._restore(path)
        with current.using(target):
            self._restore(path, target)

    def _restore(self, path, target=None):
        from cluster import current
        from cluster.models import Item, Membership
        User = get_user_model()
        users = dict(User.objects.values_list("username", "pk"))
        settings_ = dict(AppSetting.objects.values_list("key", "pk"))
        items = dict(Item.objects.values_list("code", "pk"))
        user_map, item_map, count = {}, {}, 0
        alias = current.alias()
        with transaction.atomic(), transaction.atomic(using=alias):
            for obj in serializers.deserialize("json", read(path)):
                o = obj.object
                if isinstance(o, (User, Item, Membership)) and target is not None and not target.is_default:
                    continue  # logins and cluster tables are restored from BSR Corridors' backup only
                if isinstance(o, User) and o.username in users:
                    user_map[o.pk] = o.pk = users[o.username]
                elif isinstance(o, User):
                    user_map[o.pk] = o.pk
                elif isinstance(o, Item):
                    if o.code in items:
                        item_map[o.pk] = o.pk = items[o.code]
                    o.managed_by_id = user_map.get(o.managed_by_id, o.managed_by_id)
                elif isinstance(o, Membership):
                    o.user_id = user_map.get(o.user_id, o.user_id)
                    o.item_id = item_map.get(o.item_id, o.item_id)
                    Membership.objects.filter(user_id=o.user_id, item_id=o.item_id).exclude(pk=o.pk).delete()
                elif isinstance(o, AppSetting) and o.key in settings_:
                    o.pk = settings_[o.key]
                obj.save(using=alias if o._meta.app_label == "core" else "default")
                count += 1
            # Postgres: move each table's ID counter past the restored rows, or the next new record clashes with one.
            for conn_alias, models in ((alias, list(apps.get_app_config("core").get_models())),
                                       ("default", [*apps.get_app_config("auth").get_models(),
                                                    *apps.get_app_config("cluster").get_models()])):
                conn = connections[conn_alias]
                with conn.cursor() as cur:
                    for sql in conn.ops.sequence_reset_sql(no_style(), models):
                        cur.execute(sql)
        self.stdout.write(self.style.SUCCESS(f"Restore complete ({count} records)."))
