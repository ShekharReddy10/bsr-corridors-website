"""Send each table to the right place.

Item apps (core = hotel tables; PG/co-living apps later) go to the current item's connection. Everything else
(logins, sessions, cache, the cluster tables) always stays in the main database.
"""

from . import current

ITEM_APPS = {"core"}


class ClusterRouter:
    def db_for_read(self, model, **hints):
        return current.alias() if model._meta.app_label in ITEM_APPS else "default"

    db_for_write = db_for_read

    def allow_relation(self, a, b, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "default":
            return True  # BSR Corridors' tables and the shared tables, as always
        return app_label in ITEM_APPS
