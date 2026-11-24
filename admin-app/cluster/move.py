"""Guided move of a cluster item to its own database (S6): copy, check, then switch — never to an empty database.

1. The item is made read-only (archived) while it moves, so nothing changes during the copy.
2. The new database must be reachable and empty (no rooms, guests or stays).
3. The item's tables are created there, then every record is copied in one transaction.
4. Every table's count is compared. Only if all match does the item switch to the new database.
5. The old copy (its schema in the main database) is kept as a fallback; nothing is deleted.

If any step fails, the item stays where it was and is made writable again.
"""

from django.apps import apps
from django.core import serializers
from django.core.management.color import no_style
from django.db import connections, transaction

from . import current, provision
from .models import Item


class MoveFailed(Exception):
    pass


def _temp(item: Item, db_url: str) -> Item:
    """An unsaved copy of the item pointing at the new database, under its own connection name."""
    return Item(code=f"{item.code}_move", name=item.name, kind=item.kind, db_url=db_url, schema="")


def _models():
    return list(apps.get_app_config("core").get_models())


def counts(alias: str) -> dict[str, int]:
    return {m._meta.label: m.objects.using(alias).count() for m in _models()}


def move(item: Item, db_url: str) -> dict[str, int]:
    """Returns the copied counts. Raises MoveFailed (nothing changed) on any problem."""
    if item.is_default:
        raise MoveFailed("BSR Corridors stays in the main database.")
    if not db_url.startswith(("postgres://", "postgresql://")):
        raise MoveFailed("Use a Postgres connection link (postgresql://…).")
    was_archived = item.archived
    Item.objects.filter(pk=item.pk).update(archived=True)  # read-only while it moves
    target = _temp(item, db_url)
    new = current.register(target)
    try:
        try:
            with connections[new].cursor() as cur:
                cur.execute("SELECT 1")
        except Exception as exc:
            raise MoveFailed(f"Can’t connect to the new database: {exc}") from exc
        provision.migrate(target)  # its tables, for its type (this also adds default rows, e.g. WhatsApp messages)
        real = {"core.Room", "core.Guest", "core.Stay", "core.Payment", "core.Expense"}
        if any(n for label, n in counts(new).items() if label in real):
            raise MoveFailed("The new database already has data in it — use an empty one.")
        with current.using(item):
            old = current.alias()
            before = counts(old)
            with transaction.atomic(using=new):
                conn = connections[new]
                with conn.cursor() as cur:  # clear the default rows, so the copy is exact
                    for sql in conn.ops.sql_flush(no_style(), [m._meta.db_table for m in _models()], allow_cascade=True):
                        cur.execute(sql)
                for model in _models():
                    rows = serializers.serialize("python", model.objects.using(old).order_by("pk"))
                    for obj in serializers.deserialize("python", rows):
                        obj.save(using=new)
                with connections[new].cursor() as cur:
                    for sql in connections[new].ops.sequence_reset_sql(no_style(), _models()):
                        cur.execute(sql)
                after = counts(new)
                if after != before:
                    diff = [f"{k}: {before[k]} → {after.get(k)}" for k in before if before[k] != after.get(k)]
                    raise MoveFailed("Counts don’t match after copying (" + "; ".join(diff) + ") — nothing switched.")
        # Switch: from now on the item uses the new database. The old schema stays as a fallback.
        current.forget(item)
        old_place = f"Moved to its own database; the old copy is kept in schema {item.schema} of the main database."
        item.db_url, item.schema, item.archived = db_url, "", was_archived
        item.notes = (item.notes + "\n" if item.notes else "") + old_place
        item.save(update_fields=["db_url", "schema", "archived", "notes"])
        return after
    except MoveFailed:
        Item.objects.filter(pk=item.pk).update(archived=was_archived)
        raise
    except Exception as exc:
        Item.objects.filter(pk=item.pk).update(archived=was_archived)
        raise MoveFailed(f"The move stopped: {exc}") from exc
    finally:
        current.forget(target)
