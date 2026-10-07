"""One-time: connect the owner's Google account and print a refresh token.

Run on your own computer (it opens a browser):
    python manage.py google_auth
Then set GOOGLE_REFRESH_TOKEN (and the client ID/secret) in Render's environment variables.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.gdrive import SCOPES


class Command(BaseCommand):
    help = "Authorise Google Drive access and print the refresh token."

    def handle(self, *args, **options):
        if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
            raise CommandError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first (see README).")
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            },
            scopes=SCOPES,
        )
        self.stdout.write(f"Sign in as {settings.GOOGLE_ACCOUNT_EMAIL} when the browser opens.")
        creds = flow.run_local_server(port=0, prompt="consent", access_type="offline", login_hint=settings.GOOGLE_ACCOUNT_EMAIL)
        if not creds.refresh_token:
            raise CommandError("Google did not return a refresh token. Remove the app's access at "
                               "https://myaccount.google.com/permissions and run this again.")
        self.stdout.write("\nAdd this to your environment variables:\n")
        self.stdout.write(self.style.SUCCESS(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}"))
