"""Google Drive: nightly backups, the always-up-to-date register Sheet, and exports.

Uses OAuth credentials for the owner's own Gmail account (a refresh token obtained once with
`python manage.py google_auth`). Scope `drive.file` means the app can only see files it created itself.
"""

import gzip
import io
import logging
from datetime import date, datetime, timedelta

from django.conf import settings
from django.utils import timezone

from cluster import current as cluster_current
from cluster.current import name as hotel_name

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
    return cluster_current.drive() is not None


def _service():
    access = cluster_current.drive()
    if access is None:
        raise DriveNotConfigured("Google Drive is not connected yet.")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=access["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=access["client_id"],
        client_secret=access["client_secret"],
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
        return svc.files().update(fileId=file_id, body={"name": name}, media_body=media,
                                  fields="id,webViewLink").execute()
    body = {"name": name, "parents": [folder], **({"mimeType": SHEET} if as_sheet else {})}
    return svc.files().create(body=body, media_body=media, fields="id,webViewLink").execute()


def _folders(svc) -> tuple[str, str, str]:
    root = _folder(svc, "gdrive_root", (cluster_current.drive() or {}).get("folder") or settings.GDRIVE_ROOT_FOLDER)
    backups = _folder(svc, "gdrive_backups", "Backups", root)
    exports = _folder(svc, "gdrive_exports", "Exports", root)
    return root, backups, exports


KEEP_DAILY, KEEP_WEEKLY, KEEP_MONTHLY = 7, 4, 12
EXPORT_KEEP_DAYS = 30


def backups_to_keep(created: list[datetime]) -> set[int]:
    """Which backups to keep (indexes into `created`, newest first): the newest of each of the last 7 days,
    4 weeks and 12 months. Each backup holds everything, so the newest alone is a complete copy; the older ones
    are there to go back to how things were."""
    keep, seen = set(), {"d": [], "w": [], "m": []}
    for i, at in enumerate(created):
        local = timezone.localtime(at)
        for tier, key, limit in (("d", local.date(), KEEP_DAILY), ("w", local.isocalendar()[:2], KEEP_WEEKLY),
                                 ("m", (local.year, local.month), KEEP_MONTHLY)):
            if key not in seen[tier] and len(seen[tier]) < limit:
                seen[tier].append(key)
                keep.add(i)
    return keep


def _list(svc, q: str) -> list[dict]:
    return (svc.files().list(q=q, orderBy="createdTime desc", fields="files(id,name,createdTime)", pageSize=1000)
            .execute().get("files", []))


def _prune(svc, folder: str) -> int:
    files = _list(svc, f"'{folder}' in parents and trashed=false and name contains 'backup'")
    keep = backups_to_keep([datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00")) for f in files])
    old = [f for i, f in enumerate(files) if i not in keep]
    for f in old:
        svc.files().delete(fileId=f["id"]).execute()
    return len(old)


def _prune_exports(svc, folder: str) -> int:
    cutoff = (timezone.now() - timedelta(days=EXPORT_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    old = _list(svc, f"'{folder}' in parents and trashed=false and createdTime < '{cutoff}'")
    for f in old:
        svc.files().delete(fileId=f["id"]).execute()
    return len(old)


def run_backup() -> BackupRun:
    """JSON backup (keeps the last N) + refresh the 'Guest register' Google Sheet."""
    run = BackupRun.objects.create(kind=BackupRun.Kind.BACKUP)
    try:
        svc = _service()
        root, backups, exports = _folders(svc)
        stamp = timezone.localtime().strftime("%d-%m-%Y_%I-%M%p")
        _upload(svc, gzip.compress(backup_json()), f"backup_{stamp}.json.gz", "application/gzip", backups)
        pruned = _prune(svc, backups) + _prune_exports(svc, exports)

        sheet_id = AppSetting.get("gdrive_register_sheet")
        sheet = _upload(svc, build_workbook(), f"{hotel_name()} — Guest register", XLSX, root,
                        as_sheet=True, file_id=sheet_id if _exists(svc, sheet_id) else "")
        AppSetting.put("gdrive_register_sheet", sheet["id"])

        run.ok, run.link = True, sheet.get("webViewLink", "")
        run.message = f"Backup saved; register sheet updated{f'; removed {pruned} old file(s)' if pruned else ''}."
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

PHOTO_FOLDERS = {"receipts": "Expense receipts", "ids": "Guest ID photos", "item": "Photos",
                 "pg": "Resident documents"}  # PG / co-living: agreements and ID photos


def upload_photo(data: bytes, name: str, kind: str) -> str:
    """Save a JPEG in the app's Drive folder for `kind` ("receipts" / "ids"); returns the Drive file ID."""
    svc = _service()
    root, _, _ = _folders(svc)
    folder = _folder(svc, f"gdrive_{kind}", PHOTO_FOLDERS[kind], root)
    return _upload(svc, data, name, "image/jpeg", folder)["id"]


def upload_file(data: bytes, name: str, mimetype: str, kind: str) -> str:
    """Like upload_photo, for a photo or a PDF (rental agreements)."""
    svc = _service()
    root, _, _ = _folders(svc)
    folder = _folder(svc, f"gdrive_{kind}", PHOTO_FOLDERS[kind], root)
    return _upload(svc, data, name, mimetype, folder)["id"]


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


# ── Guest invoices ──


def save_invoice(data: bytes, name: str, key: str, number: str) -> str:
    """Save one stay's invoice PDF in the "Invoices" folder; returns its Drive link.
    Always one file per invoice: the stay's remembered file is updated (and renamed if its number changed); failing
    that, a file already carrying this number is reused. Any other file with the same number is removed, so no two
    invoices in the folder ever share a number."""
    svc = _service()
    root, _, _ = _folders(svc)
    folder = _folder(svc, "gdrive_invoices", "Invoices", root)
    same_number = _list(svc, f"'{folder}' in parents and trashed=false and name contains '{number}'")
    existing = AppSetting.get(f"{key}_id")
    if not _exists(svc, existing):
        existing = same_number[0]["id"] if same_number else ""
    f = _upload(svc, data, name, "application/pdf", folder, file_id=existing)
    for other in same_number:
        if other["id"] != f["id"]:
            svc.files().delete(fileId=other["id"]).execute()
    AppSetting.put(f"{key}_id", f["id"])
    AppSetting.put(f"{key}_link", f.get("webViewLink", ""))
    return f.get("webViewLink", "")


def delete_invoice(key: str) -> None:
    """A cancelled / transferred stay has no invoice: remove its file."""
    file_id = AppSetting.get(f"{key}_id")
    if file_id:
        delete_file(file_id)
        AppSetting.objects.filter(key__in=[f"{key}_id", f"{key}_link", f"{key}_at"]).delete()


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
    f = _upload(svc, build_workbook(month, nxt - timedelta(days=1)), f"{hotel_name()} — {month:%B %Y} report",
                XLSX, folder, as_sheet=True, file_id=existing if _exists(svc, existing) else "")
    AppSetting.put(f"report_id_{month:%Y-%m}", f["id"])
    AppSetting.put(f"report_{month:%Y-%m}", f.get("webViewLink", ""))
    return f.get("webViewLink", "")


def save_last_months_report_if_missing(today_: date) -> str:
    """Nightly: once a month is over, save its report (only once)."""
    from datetime import timedelta

    last = (today_.replace(day=1) - timedelta(days=1)).replace(day=1)
    return "" if report_link(last) else save_monthly_report(last)
