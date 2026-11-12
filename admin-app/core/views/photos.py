"""Receipt and ID photos: upload to Google Drive, view through the app (so any signed-in host can see them without
Drive access), remove. Viewing an ID photo is logged, like showing a full ID number."""

import hashlib
import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import gdrive, photos
from ..middleware import current_user_name
from ..models import AuditLog, Expense, IdPhoto, Stay, StayIdPhoto
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
    """Whose ID photos a stay can have: the main guest, then guest 2, 3, … — one per guest on the stay, named or not."""
    names = [stay.guest.name] + [o.name or f"Guest {o.position}" for o in stay.other_guests.all()]
    names += [f"Guest {n}" for n in range(len(names) + 1, stay.num_guests + 1) if f"Guest {n}" not in names]
    return names


def locked(stay) -> bool:
    """A checked-out stay's ID photos are the record of what was shown — they can't be unticked any more."""
    return stay.status == Stay.Status.CHECKED_OUT


def photo_rows(stay) -> list[dict]:
    """For each person on the stay (then anyone else the guest has ID photos of, e.g. a companion from an earlier
    booking): the photos ticked for this stay, and the guest's other photos of that person that can be ticked."""
    used = set(stay.photo_links.values_list("photo_id", flat=True))
    by_person = {}
    for p in stay.guest.id_photos.all():
        by_person.setdefault(p.person, []).append(p)
    names = people(stay)
    names += [n for n in by_person if n not in names]
    return [{"person": n, "used": [p for p in by_person.get(n, []) if p.pk in used],
             "earlier": [p for p in by_person.get(n, []) if p.pk not in used]} for n in names]


def _fingerprint(upload) -> str:
    h = hashlib.sha256()
    for chunk in upload.chunks():
        h.update(chunk)
    upload.seek(0)
    return h.hexdigest()


def _link(stay, photo) -> bool:
    _, created = StayIdPhoto.objects.get_or_create(stay=stay, photo=photo, defaults={"added_by": current_user_name()})
    return created


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
    """New photos for a person on this stay: saved once for the guest and ticked for this stay. A photo already
    saved for this guest (same file) isn't stored again — the saved one is ticked instead."""
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
        saved, already = 0, []
        for upload in uploads:
            sha = _fingerprint(upload)
            if same := stay.guest.id_photos.filter(sha256=sha).first():
                _link(stay, same)
                already.append(same.name)
                continue
            name = photo_name(stay, person, n + saved + 1)
            if not (file_id := store(request, upload, "ids", name)):
                break
            _link(stay, IdPhoto.objects.create(guest=stay.guest, stay=stay, person=person, file_id=file_id,
                                               name=name, sha256=sha))
            saved += 1
        if saved:
            AuditLog.record("stay", f"{saved} ID photo{'s' if saved > 1 else ''} saved for {person}", stay=stay)
            messages.success(request, f"{saved} ID photo{'s' if saved > 1 else ''} saved for {person}.")
        if already:
            AuditLog.record("stay", f"ID photo sent again, not stored twice — ticked for this stay: "
                                    f"{', '.join(already)}", stay=stay)
            messages.info(request, f"Already saved for this guest, so not stored again — ticked for this stay: "
                                   f"{', '.join(already)}.")
    return _back(request, f"/stays/{pk}/")


@require_POST
def id_photos_use(request, pk):
    """Tick the guest's saved photos for this stay."""
    stay = get_object_or_404(Stay.objects.select_related("guest"), pk=pk)
    chosen = stay.guest.id_photos.filter(pk__in=request.POST.getlist("photo"))
    added = [p.name for p in chosen if _link(stay, p)]
    if added:
        AuditLog.record("stay", f"Ticked ID photo{'s' if len(added) > 1 else ''} for this stay: {', '.join(added)}",
                        stay=stay)
        messages.success(request, f"{len(added)} saved photo{'s' if len(added) > 1 else ''} ticked for this stay.")
    else:
        messages.error(request, "Tick at least one photo.")
    return _back(request, f"/stays/{pk}/")


@require_POST
def id_photo_unuse(request, pk, photo_pk):
    """Untick a photo for this stay (it stays saved for the guest). Not once the stay is checked out."""
    stay = get_object_or_404(Stay, pk=pk)
    link = get_object_or_404(StayIdPhoto.objects.select_related("photo"), stay=stay, photo_id=photo_pk)
    if locked(stay):
        messages.error(request, "This stay is checked out — its ID photos are kept as the record.")
    else:
        link.delete()
        AuditLog.record("stay", f"Unticked ID photo for this stay: {link.photo.name}", stay=stay)
        messages.success(request, "Photo unticked for this stay (still saved for the guest).")
    return _back(request, f"/stays/{pk}/")


@require_POST
def id_photo_person(request, pk, photo_pk):
    """Change whose ID a photo is (e.g. guest 2's ID was added under the main guest). Not once it is the record for a
    checked-out stay."""
    stay = get_object_or_404(Stay.objects.select_related("guest"), pk=pk)
    p = get_object_or_404(IdPhoto, pk=photo_pk, guest=stay.guest)
    person = request.POST.get("person", "")
    if person not in people(stay) and not stay.guest.id_photos.filter(person=person).exists():
        messages.error(request, "Choose whose ID this is.")
    elif p.stays.filter(status=Stay.Status.CHECKED_OUT).exists():
        messages.error(request, "This photo is the ID record of a checked-out stay — it can’t be changed.")
    elif person != p.person:
        AuditLog.record("stay", f"ID photo {p.name}: changed from {p.person} to {person}", stay=stay)
        p.person = person
        p.save(update_fields=["person"])
        messages.success(request, f"Photo is now {person}’s ID.")
    return _back(request, f"/stays/{pk}/")


@require_POST
def id_photo_delete(request, pk):
    """Delete a photo for good (Drive too). Not if it is the record for a checked-out stay."""
    p = get_object_or_404(IdPhoto, pk=pk)
    used_on = p.stays.filter(status=Stay.Status.CHECKED_OUT).order_by("check_in")
    if used_on:
        messages.error(request, f"{p.name} can’t be deleted — it’s the ID record for checked-out stay"
                                f"{'s' if len(used_on) > 1 else ''} {', '.join(f'#{s.pk}' for s in used_on)}.")
        return _back(request, f"/guests/{p.guest_id}/")
    gdrive.delete_file(p.file_id)
    p.delete()
    AuditLog.record("guest", f"Deleted ID photo {p.name}")
    messages.success(request, "ID photo deleted.")
    return _back(request, f"/guests/{p.guest_id}/")
