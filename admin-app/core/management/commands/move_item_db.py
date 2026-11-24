"""Move a cluster item to its own database (same steps as the button on the item's page, without a time limit):

    python manage.py move_item_db pgone 'postgresql://…?sslmode=require'
"""

from django.core.management.base import BaseCommand, CommandError

from cluster.models import Item
from cluster.move import MoveFailed, move


class Command(BaseCommand):
    help = "Copy a cluster item to an empty database, check every record, then switch to it."

    def add_arguments(self, parser):
        parser.add_argument("code")
        parser.add_argument("db_url")

    def handle(self, code, db_url, **options):
        item = Item.objects.filter(code=code).first()
        if item is None:
            raise CommandError(f"No cluster item {code!r}.")
        try:
            copied = move(item, db_url)
        except MoveFailed as exc:
            raise CommandError(f"Not moved: {exc}")
        self.stdout.write(self.style.SUCCESS(f"Moved {item.name}: {sum(copied.values())} records copied and checked."))
