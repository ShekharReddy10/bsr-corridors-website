"""Receipt and ID photos: upload to Google Drive, view through the app (so any signed-in host can see them without
Drive access), remove. Viewing an ID photo is logged, like showing a full ID number."""

import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import gdrive, photos
from ..models import AuditLog, Expense, IdPhoto, Stay
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


MAX_PHOTOS = 10  # per upload


def people(stay) -> list[str]:
    """Whose ID photos a stay can have: the main guest, then guest 2, 3, …"""
    return [stay.guest.name] + [o.name or f"Guest {o.position}" for o in stay.other_guests.all()]


def photo_rows(stay) -> list[tuple[str, list]]:
    """(person, their photos) for each person on the stay — photos saved on the guest's earlier bookings included —
    then anyone else the guest has ID photos of (a companion from an earlier booking)."""
    by_person = {}
    for p in stay.guest.id_photos.all():
        by_person.setdefault(p.person, []).append(p)
    names = people(stay)
    return [(n, by_person.get(n, [])) for n in names] + [(n, ps) for n, ps in by_person.items() if n not in names]


def photo_name(stay, person: str, n: int) -> str:
    """Name_stayID_check-in date.jpg, with _2, _3, … for a person's further photos."""
    clean = " ".join(person.replace("/", " ").replace("_", " ").split()) or "Guest"
    return f"{clean}_{stay.pk}_{stay.check_in:%d-%m-%Y}{f'_{n}' if n > 1 else ''}.jpg"


def id_photo(request, pk):
    p = get_object_or_404(IdPhoto.objects.select_related("stay"), pk=pk)
    AuditLog.record("reveal_id", f"Viewed ID photo {p.name}", stay=p.stay)
    return _image(p.file_id)


@require_POST
def id_photos_upload(request, pk):
    stay = get_object_or_404(Stay.objects.select_related("guest"), pk=pk)
    person = request.POST.get("person", "")
    uploads = request.FILES.getlist("photo")
    if person not in people(stay) and not stay.guest.id_photos.filter(person=person).exists():
        messages.error(request, "Choose whose ID this is.")
    elif not uploads:
        messages.error(request, "Choose a photo first.")
    elif len(uploads) > MAX_PHOTOS:
        messages.error(request, f"Up to {MAX_PHOTOS} photos at a time, please.")
    else:
        n = stay.guest.id_photos.filter(person=person, stay=stay).count()
        saved = 0
        for upload in uploads:
            name = photo_name(stay, person, n + saved + 1)
            if not (file_id := store(request, upload, "ids", name)):
                break
            IdPhoto.objects.create(guest=stay.guest, stay=stay, person=person, file_id=file_id, name=name)
            saved += 1
        if saved:
            AuditLog.record("stay", f"{saved} ID photo{'s' if saved > 1 else ''} saved for {person}", stay=stay)
            messages.success(request, f"{saved} ID photo{'s' if saved > 1 else ''} saved for {person}.")
    return _back(request, f"/stays/{pk}/")


@require_POST
def id_photo_delete(request, pk):
    p = get_object_or_404(IdPhoto.objects.select_related("stay"), pk=pk)
    gdrive.delete_file(p.file_id)
    p.delete()
    AuditLog.record("stay", f"Removed ID photo {p.name}", stay=p.stay)
    messages.success(request, "ID photo removed.")
    return _back(request, f"/guests/{p.guest_id}/")
