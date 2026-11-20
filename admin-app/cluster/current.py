"""The cluster item being worked on, and the database connection for it.

BSR Corridors (the default item) uses the main database connection, exactly as before. Every other item gets its own
connection whose search_path is ONLY that item's schema — so a missing table is an error, never a silent read of
another item's data. Its connection settings are copied from the main database, or come from the item's own
database URL.

Requests set the item from the address (middleware); commands and the nightly job use `using(item)`.
"""

from contextlib import contextmanager
from contextvars import ContextVar

import dj_database_url
from django.db import connections
from django.db.backends.signals import connection_created
from django.dispatch import receiver

_item: ContextVar = ContextVar("cluster_item", default=None)
SCHEMA_KEY = "CLUSTER_SCHEMA"


def get():
    """The current item, or None (then BSR Corridors' data is used, as before v2)."""
    return _item.get()


def alias() -> str:
    item = _item.get()
    return register(item) if item is not None else "default"


@contextmanager
def using(item):
    token = _item.set(item)
    try:
        yield item
    finally:
        _item.reset(token)


def set_item(item):
    return _item.set(item)


def reset(token):
    _item.reset(token)


def register(item) -> str:
    """Make sure Django has a connection for this item; returns its alias."""
    if item.is_default:
        return "default"
    if not item.schema and not item.db_url:
        # Without its own schema or database it would see the main database's tables — BSR Corridors' data.
        raise ValueError(f"Cluster item {item.code!r} has no tables of its own yet.")
    name = item.alias
    if name not in connections.settings:
        base = dict(connections["default"].settings_dict)
        if item.db_url:
            own = dj_database_url.parse(item.db_url)
            base.update({k: own[k] for k in ("ENGINE", "NAME", "USER", "PASSWORD", "HOST", "PORT") if k in own})
            base["OPTIONS"] = own.get("OPTIONS", {})
        base.update({"CONN_MAX_AGE": 0, "ATOMIC_REQUESTS": False, "TEST": {**base.get("TEST", {}), "NAME": None},
                     SCHEMA_KEY: item.schema})
        connections.settings[name] = base
    return name


def forget(item) -> None:
    """Drop the connection (e.g. after the item's database settings changed)."""
    name = item.alias
    if item.is_default or name not in connections.settings:
        return
    conn = getattr(connections._connections, name, None)
    if conn is not None:
        conn.close()
        try:
            delattr(connections._connections, name)
        except AttributeError:
            pass
    del connections.settings[name]


@receiver(connection_created)
def _schema_only(sender, connection, **kwargs):
    schema = connection.settings_dict.get(SCHEMA_KEY)
    if schema and connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')


def local(path: str) -> str:
    """'/stays/5/' → the same page of the current item ('/p/abc/stays/5/' for a cluster item, unchanged for BSR)."""
    from django.urls import get_script_prefix
    return get_script_prefix().rstrip("/") + path


def name() -> str:
    """The current item's name — BSR Corridors keeps HOTEL_NAME from the environment, as before."""
    from django.conf import settings
    item = _item.get()
    return settings.HOTEL_NAME if item is None or item.is_default else item.name


def is_default() -> bool:
    item = _item.get()
    return item is None or item.is_default


def upi() -> tuple[str, str]:
    """(UPI ID, payee name). BSR Corridors: from the environment, as before. Other items: set up in phase 2 — until
    then none, so their payment links stay off (they never pay into BSR Corridors' account)."""
    from django.conf import settings
    return (settings.UPI_ID, settings.UPI_NAME) if is_default() else ("", "")
