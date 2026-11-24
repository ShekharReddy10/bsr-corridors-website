"""Apply table changes to every cluster item (run on each deploy, after `migrate`).

BSR Corridors is updated by `migrate` as always; this updates the other hotels, PGs and co-livings, all of a type
together. One item failing (e.g. its own database unreachable) doesn't stop the others; the command reports it.
"""

from django.core.management.base import BaseCommand

from cluster import provision
from cluster.models import Item


class Command(BaseCommand):
    help = "Apply database changes to every property's tables."

    def handle(self, **options):
        failed = []
        for item in Item.objects.filter(is_default=False).exclude(schema="", db_url=""):
            try:
                provision.create_schema(item)
                provision.migrate(item)
                self.stdout.write(f"{item.name}: up to date")
            except Exception as exc:  # report and carry on with the others
                failed.append(item.name)
                self.stderr.write(f"{item.name}: FAILED — {exc}")
        if failed:
            self.stderr.write(f"Not updated: {', '.join(failed)}. They show 'can't read' until fixed.")
        else:
            self.stdout.write(self.style.SUCCESS("All properties up to date."))
