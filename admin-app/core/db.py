"""Database helpers that follow the cluster item being worked on (see cluster/current.py)."""

from django.db import connections, transaction

from cluster.current import alias


def atomic():
    """A transaction on the current item's database (BSR Corridors: the main database, as before)."""
    return transaction.atomic(using=alias())


def connection():
    return connections[alias()]
