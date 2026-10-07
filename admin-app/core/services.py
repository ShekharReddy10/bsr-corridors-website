"""Business rules for stays: availability, safe saves, extensions, check-in/out."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AuditLog, Room, Stay


class StayConflict(Exception):
    """The room is already occupied for some of the requested nights."""

    def __init__(self, conflicts):
        self.conflicts = list(conflicts)
        names = ", ".join(f"{s.guest.name} ({s.check_in:%d-%m} → {s.check_out:%d-%m})" for s in self.conflicts)
        super().__init__(f"Room is already booked for some of these nights: {names or 'another stay'}.")


def today() -> date:
    return timezone.localdate()


def conflicts(room: Room, start: date, end: date, exclude_ids=()):
    return (
        Stay.objects.live()
        .overlapping(start, end)
        .filter(room=room)
        .exclude(pk__in=[i for i in exclude_ids if i])
        .select_related("guest")
        .order_by("check_in")
    )


def free_rooms(start: date, end: date, *, room_type=None, exclude_room=None):
    """Active rooms with no live stay in [start, end)."""
    busy = Stay.objects.live().overlapping(start, end).values("room_id")
    rooms = Room.objects.filter(is_active=True, status=Room.Status.ACTIVE).exclude(pk__in=busy)
    if room_type is not None:
        rooms = rooms.filter(room_type=room_type)
    if exclude_room is not None:
        rooms = rooms.exclude(pk=exclude_room.pk)
    return rooms.select_related("room_type")


def _lock_room(room: Room) -> None:
    """Serialise writes per room (Postgres row lock; no-op on SQLite)."""
    Room.objects.select_for_update().filter(pk=room.pk).first()


def save_stay(stay: Stay, *, action: str, summary: str) -> Stay:
    """Save a stay only if its room is free for all its nights. Raises StayConflict."""
    with transaction.atomic():
        _lock_room(stay.room)
        if stay.status != Stay.Status.CANCELLED:
            clash = conflicts(stay.room, stay.check_in, stay.check_out, exclude_ids=[stay.pk])
            if clash.exists():
                raise StayConflict(clash)
        try:
            with transaction.atomic():
                stay.save()
        except IntegrityError as exc:  # Postgres exclusion constraint caught a race
            raise StayConflict([]) from exc
        AuditLog.record(action, summary, stay)
    return stay


# ── Extension ────────────────────────────────────────────────


@dataclass
class ExtensionPlan:
    stay: Stay
    new_check_out: date
    free: bool
    blocking: list = field(default_factory=list)
    split_rooms: list = field(default_factory=list)  # same type, free for the extra nights only
    move_rooms: list = field(default_factory=list)  # free from move_start to the new check-out
    move_start: date | None = None

    @property
    def extra_nights(self) -> int:
        return (self.new_check_out - self.stay.check_out).days


def plan_extension(stay: Stay, new_check_out: date) -> ExtensionPlan:
    if new_check_out <= stay.check_out:
        raise ValueError("The new check-out date must be after the current one.")
    blocking = list(conflicts(stay.room, stay.check_out, new_check_out, exclude_ids=[stay.pk]))
    plan = ExtensionPlan(stay=stay, new_check_out=new_check_out, free=not blocking, blocking=blocking)
    if blocking:
        plan.split_rooms = list(
            free_rooms(stay.check_out, new_check_out, room_type=stay.room.room_type, exclude_room=stay.room)
        )
        # The guest moves from today (never before check-in, never after the current check-out).
        plan.move_start = min(max(stay.check_in, today()), stay.check_out)
        rooms = free_rooms(plan.move_start, new_check_out, exclude_room=stay.room)
        # Same room type first, then the rest.
        plan.move_rooms = sorted(rooms, key=lambda r: (r.room_type_id != stay.room.room_type_id, r.sort_order, r.number))
    return plan


def extend_in_place(stay: Stay, new_check_out: date) -> Stay:
    old = stay.check_out
    stay.check_out = new_check_out
    return save_stay(stay, action="extend", summary=f"Extended in {stay.room}: check-out {old:%d-%m-%Y} → {new_check_out:%d-%m-%Y}")


def extend_split(stay: Stay, new_check_out: date, room: Room) -> Stay:
    """Keep the current room until its check-out, then continue in `room` for the extra nights."""
    with transaction.atomic():
        continuation = Stay(
            guest=stay.guest,
            room=room,
            check_in=stay.check_out,
            check_out=new_check_out,
            num_guests=stay.num_guests,
            nightly_rate=stay.nightly_rate,
            payment_mode=stay.payment_mode,
            status=Stay.Status.UPCOMING,
            form_c_filed=stay.form_c_filed,
            linked_to=stay.root,
            notes=f"Continuation of stay in {stay.room}.",
        )
        save_stay(
            continuation,
            action="extend",
            summary=f"Extended by moving to {room} for {stay.check_out:%d-%m-%Y} → {new_check_out:%d-%m-%Y} ({stay.room} was booked)",
        )
        AuditLog.record("extend", f"Continues in {room} from {stay.check_out:%d-%m-%Y} to {new_check_out:%d-%m-%Y}", stay)
    return continuation


def extend_move(stay: Stay, new_check_out: date, room: Room, move_start: date) -> Stay:
    """Move the guest to `room` from `move_start` until the new check-out."""
    if move_start >= stay.check_out:
        return extend_split(stay, new_check_out, room)
    with transaction.atomic():
        if move_start <= stay.check_in:
            old_room = stay.room
            stay.room = room
            stay.check_out = new_check_out
            return save_stay(stay, action="extend", summary=f"Moved {old_room} → {room} and extended to {new_check_out:%d-%m-%Y}")

        continuation = Stay(
            guest=stay.guest,
            room=room,
            check_in=move_start,
            check_out=new_check_out,
            num_guests=stay.num_guests,
            nightly_rate=stay.nightly_rate,
            payment_mode=stay.payment_mode,
            status=stay.status,
            checked_in_at=timezone.now() if stay.status == Stay.Status.CHECKED_IN else None,
            form_c_filed=stay.form_c_filed,
            linked_to=stay.root,
            notes=f"Moved from {stay.room}.",
        )
        old_check_out = stay.check_out
        stay.check_out = move_start
        if stay.status == Stay.Status.CHECKED_IN:
            stay.status = Stay.Status.CHECKED_OUT
            stay.checked_out_at = timezone.now()
        save_stay(stay, action="move", summary=f"Moved out of {stay.room} on {move_start:%d-%m-%Y} (was until {old_check_out:%d-%m-%Y})")
        save_stay(continuation, action="extend", summary=f"Moved in from {stay.room}; stays until {new_check_out:%d-%m-%Y}")
    return continuation


# ── Check-in / check-out / payments ──────────────────────────


def check_in(stay: Stay) -> Stay:
    stay.status = Stay.Status.CHECKED_IN
    stay.checked_in_at = timezone.now()
    return save_stay(stay, action="check_in", summary=f"Checked in to {stay.room}")


def check_out(stay: Stay) -> Stay:
    """Check out now. If the guest leaves before the booked date, the stay is shortened (early checkout)."""
    note = ""
    t = today()
    if stay.check_in < t < stay.check_out:
        note = f" — early checkout, was booked until {stay.check_out:%d-%m-%Y}"
        stay.check_out = t
    stay.status = Stay.Status.CHECKED_OUT
    stay.checked_out_at = timezone.now()
    return save_stay(stay, action="check_out", summary=f"Checked out of {stay.room}{note}")


def shorten(stay: Stay, new_check_out: date) -> Stay:
    if not (stay.check_in < new_check_out < stay.check_out):
        raise ValueError("Early check-out must be after check-in and before the current check-out.")
    old = stay.check_out
    stay.check_out = new_check_out
    return save_stay(stay, action="shorten", summary=f"Shortened: check-out {old:%d-%m-%Y} → {new_check_out:%d-%m-%Y}")


def add_payment(stay: Stay, amount: Decimal, mode: str) -> Stay:
    if amount <= 0:
        raise ValueError("Payment must be more than zero.")
    stay.amount_paid = (stay.amount_paid or Decimal(0)) + amount
    if mode:
        stay.payment_mode = mode
    label = dict(Stay.PaymentMode.choices).get(mode, "")
    return save_stay(stay, action="payment", summary=f"Payment received ₹{amount:,.2f}{f' ({label})' if label else ''}")


def cancel(stay: Stay) -> Stay:
    stay.status = Stay.Status.CANCELLED
    return save_stay(stay, action="cancel", summary="Stay cancelled")
