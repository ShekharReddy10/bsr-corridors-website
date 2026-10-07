import hmac
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .. import gdrive
from ..exports import build_workbook
from ..forms import DateRangeForm
from ..models import AuditLog, BackupRun, Guest, Stay
from ..services import today

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def export(request):
    t = today()
    form = DateRangeForm(request.POST or None, initial={"start": t.replace(day=1), "end": t})
    if request.method == "POST" and form.is_valid():
        start, end, full = form.cleaned_data["start"], form.cleaned_data["end"], form.cleaned_data["include_full_ids"]
        AuditLog.record("export", f"Exported stays {start:%d-%m-%Y} → {end:%d-%m-%Y}{' with full IDs' if full else ''}")
        if request.POST.get("action") == "drive":
            run = gdrive.export_to_drive(start, end, full)
            (messages.success if run.ok else messages.error)(request, run.message)
            return redirect("export")
        response = HttpResponse(build_workbook(start, end, full), content_type=XLSX)
        response["Content-Disposition"] = f'attachment; filename="bsr-stays-{start:%Y%m%d}-{end:%Y%m%d}.xlsx"'
        return response
    return render(request, "core/export.html", {
        "form": form,
        "drive_ready": gdrive.is_configured(),
        "recent": BackupRun.objects.filter(kind=BackupRun.Kind.EXPORT)[:10],
    })


def backups(request):
    if request.method == "POST":
        run = gdrive.run_backup()
        (messages.success if run.ok else messages.error)(request, run.message)
        return redirect("backups")
    return render(request, "core/backups.html", {
        "runs": BackupRun.objects.filter(kind=BackupRun.Kind.BACKUP)[:30],
        "drive_ready": gdrive.is_configured(),
        "keep": settings.BACKUP_KEEP_DAYS,
    })


@csrf_exempt
@require_POST
def backup_hook(request):
    """Called nightly by GitHub Actions with the X-Backup-Token header."""
    token = request.headers.get("X-Backup-Token", "")
    if not settings.BACKUP_TOKEN or not hmac.compare_digest(token, settings.BACKUP_TOKEN):
        return HttpResponseForbidden("bad token")
    run = gdrive.run_backup()
    return JsonResponse({"ok": run.ok, "message": run.message}, status=200 if run.ok else 500)


def settings_page(request):
    years = request.POST.get("years", "3")
    if request.method == "POST":
        if request.POST.get("confirm", "").strip().upper() != "PURGE" or not years.isdigit() or int(years) < 1:
            messages.error(request, "Type PURGE and choose at least 1 year to delete old records.")
        else:
            cutoff = today() - timedelta(days=365 * int(years))
            with transaction.atomic():
                old = Stay.objects.filter(check_out__lt=cutoff).exclude(status__in=[Stay.Status.UPCOMING, Stay.Status.CHECKED_IN])
                count = old.count()
                old.delete()
                orphans = Guest.objects.filter(stays__isnull=True)
                guests = orphans.count()
                orphans.delete()
                AuditLog.record("purge", f"Deleted {count} stay(s) and {guests} guest(s) that ended before {cutoff:%d-%m-%Y}")
            messages.success(request, f"Deleted {count} old stay(s) and {guests} guest record(s).")
        return redirect("settings")
    return render(request, "core/settings.html", {
        "drive_ready": gdrive.is_configured(),
        "encryption_set": bool(settings.FIELD_ENCRYPTION_KEY),
        "backup_hook_set": bool(settings.BACKUP_TOKEN),
        "logs": AuditLog.objects.select_related("stay")[:100],
    })
