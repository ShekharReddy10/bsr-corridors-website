"""Send each table to the right place.

Item apps (core = hotel tables and what every item has: expenses, messages, backups…; pg = PG / co-living tables) go
to the current item's connection. Everything else (logins, sessions, cache, the cluster tables) always stays in the
main database. The PG tables are never created in the main database: BSR Corridors' tables stay as they are.
"""

from . import current

ITEM_APPS = {"core", "pg"}
ITEM_ONLY_APPS = {"pg"}  # only in an item's own schema / database


class ClusterRouter:
    def db_for_read(self, model, **hints):
        return current.alias() if model._meta.app_label in ITEM_APPS else "default"

    db_for_write = db_for_read

    def allow_relation(self, a, b, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "default":
            return app_label not in ITEM_ONLY_APPS  # BSR Corridors' tables and the shared tables, as always
        return app_label in ITEM_APPS
