"""Restore a JSON backup downloaded from Google Drive.

    python manage.py restore_backup backup-2026-10-06_0200.json --yes

Needs the same FIELD_ENCRYPTION_KEY that was used when the backup was made.
Existing rows with the same IDs are overwritten; nothing else is deleted.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Restore rooms, guests and stays from a JSON backup file."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--yes", action="store_true", help="Confirm the restore.")

    def handle(self, path, yes, **options):
        if not yes:
            raise CommandError("This overwrites matching records. Re-run with --yes to confirm.")
        call_command("loaddata", path)
        self.stdout.write(self.style.SUCCESS("Restore complete."))
