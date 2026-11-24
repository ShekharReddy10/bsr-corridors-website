"""Create a cluster item's tables, and keep every item's tables up to date.

A new item gets its own schema (a separate section of the database, with its own copy of every table for its type),
then the type's migrations run there. On every deploy `manage.py migrate_items` applies new table changes to every
item of each type (decision 5.4). BSR Corridors' tables are migrated by the normal `migrate`, as before.
"""

import re

from django.core.management import call_command
from django.db import connections

from . import current
from .models import Item

APPS_FOR = {Item.Kind.HOTEL: ["core"], Item.Kind.PG: ["core", "pg"], Item.Kind.COLIVING: ["core", "pg"]}


def schema_name(code: str) -> str:
    return "item_" + re.sub(r"[^a-z0-9_]", "_", code.lower())


def create_schema(item: Item) -> None:
    alias = current.register(item)
    schema = item.schema
    if not schema:  # its own database: its tables go in that database's usual place
        return
    if not re.fullmatch(r"[a-z0-9_]{1,63}", schema):
        raise ValueError(f"Bad schema name: {schema!r}")
    # Created through a plain connection to the same database (the item's own connection can't see other schemas).
    base = "default" if not item.db_url else alias
    with connections[base].cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


def migrate(item: Item, verbosity: int = 0) -> None:
    if item.is_default:
        raise ValueError("BSR Corridors is migrated by the normal migrate command.")
    current.forget(item)  # fresh connection, so the search_path is set
    alias = current.register(item)
    # Data steps inside migrations use the models' normal queries: with the item set they read and write this item's
    # tables. Without it they would go to BSR Corridors' tables.
    with current.using(item):
        for app in APPS_FOR[item.kind]:
            call_command("migrate", app, database=alias, verbosity=verbosity, interactive=False)


def provision(item: Item) -> None:
    """New item: its schema, then its tables."""
    if item.is_default:
        raise ValueError("BSR Corridors already has its tables.")
    if not item.schema:
        item.schema = schema_name(item.code)
        item.save(update_fields=["schema"])
    create_schema(item)
    migrate(item)


def drop(item: Item) -> None:
    """Tests and the Testing page only: remove an item's schema and everything in it."""
    if item.is_default or not item.schema:
        raise ValueError("Refusing to drop BSR Corridors.")
    current.forget(item)
    with connections["default"].cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{item.schema}" CASCADE')
