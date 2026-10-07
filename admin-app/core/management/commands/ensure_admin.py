"""Create or update the single admin account from ADMIN_USERNAME / ADMIN_PASSWORD.

Runs on every deploy (build.sh), so changing the password in Render's environment settings
and redeploying changes the login. Render's free plan has no shell, so this replaces createsuperuser.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create/update the admin user from ADMIN_USERNAME and ADMIN_PASSWORD."

    def handle(self, *args, **options):
        username = os.environ.get("ADMIN_USERNAME", "").strip()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not username or not password:
            raise CommandError("Set ADMIN_USERNAME and ADMIN_PASSWORD.")
        if len(password) < 10:
            raise CommandError("ADMIN_PASSWORD must be at least 10 characters.")
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username)
        user.is_staff = user.is_superuser = user.is_active = True
        if created or not user.check_password(password):
            user.set_password(password)
        user.save()
        # Only one admin: deactivate any other accounts.
        User.objects.exclude(pk=user.pk).update(is_active=False)
        self.stdout.write(self.style.SUCCESS(f"Admin '{username}' {'created' if created else 'up to date'}."))
