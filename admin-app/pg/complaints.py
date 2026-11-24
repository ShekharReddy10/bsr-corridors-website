"""Complaints (§8.9): raised → being fixed → fixed, per property, linked to maintenance.

A complaint can put its bed (or the whole room) under maintenance until it is fixed, so no one is moved in meanwhile.
Only a bed / room that was in use is changed, and it is put back when the last complaint holding it is fixed.
"""

from datetime import date

from django.core.exceptions import ValidationError

from core.db import atomic
from core.models import AuditLog
from core.services import today

from .models import Bed, Complaint, Room, Tenancy

C = Complaint.Status


def _target(c: Complaint):
    return c.bed if c.hold == Complaint.Hold.BED else c.room


def _holders(c: Complaint):
    """Other open complaints holding the same bed / room."""
    qs = Complaint.objects.exclude(pk=c.pk).exclude(status=C.FIXED)
    if c.hold == Complaint.Hold.BED:
        return qs.filter(hold=Complaint.Hold.BED, bed=c.bed)
    return qs.filter(hold=Complaint.Hold.ROOM, room=c.room)


def raise_complaint(*, room: Room, category: str, details: str = "", bed: Bed | None = None,
                    tenancy: Tenancy | None = None, raised_on: date | None = None, hold: str = "",
                    by: str = "") -> Complaint:
    if bed is not None and bed.room_id != room.pk:
        raise ValidationError("That bed isn't in this room.")
    if hold == Complaint.Hold.BED and bed is None:
        raise ValidationError("Pick the bed to put under maintenance.")
    raised_on = raised_on or today()
    if raised_on > today():
        raise ValidationError("The date can't be in the future.")
    with atomic():
        c = Complaint(room=room, bed=bed, tenancy=tenancy, category=category, details=details.strip(),
                      raised_on=raised_on, created_by=by)
        if hold:
            target = bed if hold == Complaint.Hold.BED else room
            if target.status == target.Status.ACTIVE:  # already under maintenance: leave it to whoever did that
                target.status = target.Status.MAINTENANCE
                target.save(update_fields=["status"])
                c.hold = hold
        c.full_clean(exclude=["created_by"])
        c.save()
        AuditLog.record("pg_complaint", f"Complaint raised: {c.get_category_display()}, {c.place.lower()}"
                                        + (f" ({tenancy.resident.name})" if tenancy else "")
                                        + (" — under maintenance until fixed" if c.hold else ""))
    return c


def start(c: Complaint) -> None:
    if c.status != C.OPEN:
        raise ValidationError("Only an open complaint can be started.")
    c.status = C.WORKING
    c.save(update_fields=["status"])


def fix(c: Complaint, *, fixed_by: str = "", note: str = "", day: date | None = None) -> None:
    if not c.is_open:
        raise ValidationError("This complaint is already fixed.")
    day = day or today()
    if day < c.raised_on:
        raise ValidationError("It can't be fixed before it was raised.")
    with atomic():
        c.status, c.fixed_on, c.fixed_by, c.fix_note = C.FIXED, day, fixed_by.strip()[:100], note.strip()[:200]
        c.save(update_fields=["status", "fixed_on", "fixed_by", "fix_note"])
        freed = ""
        if c.hold and not _holders(c).exists():
            target = _target(c)
            if target.status == target.Status.MAINTENANCE:
                target.status = target.Status.ACTIVE
                target.save(update_fields=["status"])
                freed = f"; {c.hold} back in use"
        AuditLog.record("pg_complaint", f"Complaint fixed: {c.get_category_display()}, {c.place.lower()}"
                                        + (f" by {c.fixed_by}" if c.fixed_by else "") + freed)


def reopen(c: Complaint) -> None:
    if c.is_open:
        raise ValidationError("This complaint is still open.")
    with atomic():
        c.status, c.fixed_on = C.OPEN, None
        c.save(update_fields=["status", "fixed_on"])
        if c.hold:
            target = _target(c)
            if target.status == target.Status.ACTIVE:
                target.status = target.Status.MAINTENANCE
                target.save(update_fields=["status"])
        AuditLog.record("pg_complaint", f"Complaint reopened: {c.get_category_display()}, {c.place.lower()}")


def open_complaints():
    return (Complaint.objects.exclude(status=C.FIXED).select_related("room", "bed__room", "tenancy__resident")
            .order_by("raised_on", "pk"))
