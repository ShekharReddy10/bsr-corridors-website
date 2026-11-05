"""Google Drive: nightly backups, the always-up-to-date register Sheet, and exports.

Uses OAuth credentials for the owner's own Gmail account (a refresh token obtained once with
`python manage.py google_auth`). Scope `drive.file` means the app can only see files it created itself.
"""

import io
import logging
from datetime import date

from django.conf import settings
from django.utils import timezone

from .exports import backup_json, build_workbook
from .models import AppSetting, BackupRun

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET = "application/vnd.google-apps.spreadsheet"
FOLDER = "application/vnd.google-apps.folder"


class DriveNotConfigured(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REFRESH_TOKEN)


def _service():
    if not is_configured():
        raise DriveNotConfigured("Google Drive is not connected yet (GOOGLE_CLIENT_ID / SECRET / REFRESH_TOKEN).")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=settings.GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _exists(svc, file_id: str) -> bool:
    if not file_id:
        return False
    try:
        return not svc.files().get(fileId=file_id, fields="id,trashed").execute().get("trashed", False)
    except Exception:
        return False


def _folder(svc, key: str, name: str, parent: str | None = None) -> str:
    """Find (by remembered ID) or create a folder the app owns."""
    folder_id = AppSetting.get(key)
    if _exists(svc, folder_id):
        return folder_id
    body = {"name": name, "mimeType": FOLDER, **({"parents": [parent]} if parent else {})}
    folder_id = svc.files().create(body=body, fields="id").execute()["id"]
    AppSetting.put(key, folder_id)
    return folder_id


def _upload(svc, data: bytes, name: str, mimetype: str, folder: str, *, as_sheet=False, file_id: str = "") -> dict:
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
    if file_id:
        return svc.files().update(fileId=file_id, media_body=media, fields="id,webViewLink").execute()
    body = {"name": name, "parents": [folder], **({"mimeType": SHEET} if as_sheet else {})}
    return svc.files().create(body=body, media_body=media, fields="id,webViewLink").execute()


def _folders(svc) -> tuple[str, str, str]:
    root = _folder(svc, "gdrive_root", settings.GDRIVE_ROOT_FOLDER)
    backups = _folder(svc, "gdrive_backups", "Backups", root)
    exports = _folder(svc, "gdrive_exports", "Exports", root)
    return root, backups, exports


def _prune(svc, folder: str, keep: int) -> int:
    files = (
        svc.files()
        .list(q=f"'{folder}' in parents and trashed=false and name contains 'backup-'", orderBy="createdTime desc",
              fields="files(id,name)", pageSize=1000)
        .execute()
        .get("files", [])
    )
    for f in files[keep:]:
        svc.files().delete(fileId=f["id"]).execute()
    return max(0, len(files) - keep)


def run_backup() -> BackupRun:
    """JSON backup (keeps the last N) + refresh the 'Guest register' Google Sheet."""
    run = BackupRun.objects.create(kind=BackupRun.Kind.BACKUP)
    try:
        svc = _service()
        root, backups, _ = _folders(svc)
        stamp = timezone.localtime().strftime("%Y-%m-%d_%H%M")
        _upload(svc, backup_json(), f"backup-{stamp}.json", "application/json", backups)
        pruned = _prune(svc, backups, settings.BACKUP_KEEP_DAYS)

        sheet_id = AppSetting.get("gdrive_register_sheet")
        sheet = _upload(svc, build_workbook(), f"{settings.HOTEL_NAME} — Guest register", XLSX, root,
                        as_sheet=True, file_id=sheet_id if _exists(svc, sheet_id) else "")
        AppSetting.put("gdrive_register_sheet", sheet["id"])

        run.ok, run.link = True, sheet.get("webViewLink", "")
        run.message = f"Backup saved; register sheet updated{f'; removed {pruned} old backup(s)' if pruned else ''}."
    except Exception as exc:  # recorded and shown on the dashboard
        log.exception("Backup failed")
        run.message = str(exc)[:1000]
    run.finished_at = timezone.now()
    run.save()
    return run


def export_to_drive(start: date, end: date, full_ids: bool) -> BackupRun:
    run = BackupRun.objects.create(kind=BackupRun.Kind.EXPORT)
    try:
        svc = _service()
        _, _, exports = _folders(svc)
        name = f"Stays {start:%d-%m-%Y} to {end:%d-%m-%Y}"
        f = _upload(svc, build_workbook(start, end, full_ids), name, XLSX, exports, as_sheet=True)
        run.ok, run.link, run.message = True, f.get("webViewLink", ""), f"Exported “{name}” to Google Drive."
    except Exception as exc:
        log.exception("Export failed")
        run.message = str(exc)[:1000]
    run.finished_at = timezone.now()
    run.save()
    return run


# ── Photos (receipts, ID cards) ──

PHOTO_FOLDERS = {"receipts": "Expense receipts", "ids": "Guest ID photos"}


def upload_photo(data: bytes, name: str, kind: str) -> str:
    """Save a JPEG in the app's Drive folder for `kind` ("receipts" / "ids"); returns the Drive file ID."""
    svc = _service()
    root, _, _ = _folders(svc)
    folder = _folder(svc, f"gdrive_{kind}", PHOTO_FOLDERS[kind], root)
    return _upload(svc, data, name, "image/jpeg", folder)["id"]


def fetch(file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, _service().files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def delete_file(file_id: str) -> None:
    try:
        _service().files().delete(fileId=file_id).execute()
    except Exception:  # already gone, or Drive unreachable: the link is removed either way
        log.warning("Could not delete Drive file %s", file_id)


# ── Monthly report ──


def report_link(month: date) -> str:
    return AppSetting.get(f"report_{month:%Y-%m}")


def save_monthly_report(month: date) -> str:
    """Save that month's stays, payments, expenses and summary as a Google Sheet in "Monthly reports"; returns its link."""
    from datetime import timedelta

    svc = _service()
    root, _, _ = _folders(svc)
    folder = _folder(svc, "gdrive_reports", "Monthly reports", root)
    nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    existing = AppSetting.get(f"report_id_{month:%Y-%m}")
    f = _upload(svc, build_workbook(month, nxt - timedelta(days=1)), f"{settings.HOTEL_NAME} — {month:%B %Y} report",
                XLSX, folder, as_sheet=True, file_id=existing if _exists(svc, existing) else "")
    AppSetting.put(f"report_id_{month:%Y-%m}", f["id"])
    AppSetting.put(f"report_{month:%Y-%m}", f.get("webViewLink", ""))
    return f.get("webViewLink", "")


def save_last_months_report_if_missing(today_: date) -> str:
    """Nightly: once a month is over, save its report (only once)."""
    from datetime import timedelta

    last = (today_.replace(day=1) - timedelta(days=1)).replace(day=1)
    return "" if report_link(last) else save_monthly_report(last)
