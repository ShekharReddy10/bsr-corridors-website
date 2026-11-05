"""Receipt and ID photos: upload to Google Drive, view through the app (so any signed-in host can see them without
Drive access), remove. Viewing an ID photo is logged, like showing a full ID number."""

import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import gdrive, photos
from ..models import AuditLog, Expense, Guest
from .revenue import revenue_unlocked

log = logging.getLogger(__name__)


def store(request, upload, kind: str, name: str) -> str:
    """Upload a photo to Drive; returns its file ID, or "" after telling the user what went wrong."""
    try:
        return gdrive.upload_photo(photos.prepare(upload), name, kind)
    except photos.BadPhoto as exc:
        messages.error(request, str(exc))
    except gdrive.DriveNotConfigured:
        messages.error(request, "Photos are kept in Google Drive, which isn’t connected yet (see Backups).")
    except Exception:
        log.exception("Photo upload failed")
        messages.error(request, "Couldn’t save the photo to Google Drive. Please try again.")
    return ""


def _image(file_id: str) -> HttpResponse:
    response = HttpResponse(gdrive.fetch(file_id), content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    return response


def _back(request, fallback):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(fallback)


@revenue_unlocked
def receipt(request, pk):
    e = get_object_or_404(Expense, pk=pk)
    return _image(e.receipt_id) if e.receipt_id else redirect("expense_edit", pk=pk)


@revenue_unlocked
@require_POST
def receipt_delete(request, pk):
    e = get_object_or_404(Expense, pk=pk)
    if e.receipt_id:
        gdrive.delete_file(e.receipt_id)
        e.receipt_id = ""
        e.save(update_fields=["receipt_id"])
        AuditLog.record("expense", f"Removed receipt photo of expense: {e}")
        messages.success(request, "Receipt photo removed.")
    return redirect("expense_edit", pk=pk)


def id_photo(request, pk):
    g = get_object_or_404(Guest, pk=pk)
    if not g.id_photo_id:
        return redirect("guest_detail", pk=pk)
    AuditLog.record("reveal_id", f"Viewed ID photo of {g.name}")
    return _image(g.id_photo_id)


@require_POST
def id_photo_upload(request, pk):
    g = get_object_or_404(Guest, pk=pk)
    upload = request.FILES.get("photo")
    if not upload:
        messages.error(request, "Choose a photo first.")
    elif file_id := store(request, upload, "ids", f"{g.name} — guest {g.pk}.jpg"):
        if g.id_photo_id:
            gdrive.delete_file(g.id_photo_id)
        g.id_photo_id = file_id
        g.save(update_fields=["id_photo_id"])
        AuditLog.record("guest", f"ID photo saved for {g.name}")
        messages.success(request, "ID photo saved.")
    return _back(request, f"/guests/{pk}/")


@require_POST
def id_photo_delete(request, pk):
    g = get_object_or_404(Guest, pk=pk)
    if g.id_photo_id:
        gdrive.delete_file(g.id_photo_id)
        g.id_photo_id = ""
        g.save(update_fields=["id_photo_id"])
        AuditLog.record("guest", f"ID photo removed for {g.name}")
        messages.success(request, "ID photo removed.")
    return _back(request, f"/guests/{pk}/")
