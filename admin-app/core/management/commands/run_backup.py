from django.core.management.base import BaseCommand, CommandError

from core.gdrive import run_backup


class Command(BaseCommand):
    help = "Back up to Google Drive now (JSON + guest register sheet)."

    def handle(self, *args, **options):
        run = run_backup()
        if not run.ok:
            raise CommandError(run.message)
        self.stdout.write(self.style.SUCCESS(run.message))
