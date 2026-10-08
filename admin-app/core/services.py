"""Business rules for stays: availability, safe saves, extensions, check-in/out."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AuditLog, Room, Stay


class StayConflict(Exception):
    """The room is already occupied for some of the requested nights."""

    def __init__(self, conflicts):
        self.conflicts = list(conflicts)
        names = ", ".join(f"{s.guest.name} ({s.dates_label})" for s in self.conflicts)
        super().__init__(f"Room is already booked for some of these nights: {names or 'another stay'}.")


def today() -> date:
    return timezone.localdate()


def conflicts(room: Room, start: date, end: date | None, exclude_ids=()):
    return (
        Stay.objects.live()
        .overlapping(start, end)
        .filter(room=room)
        .exclude(pk__in=[i for i in exclude_ids if i])
        .select_related("guest")
        .order_by("check_in")
    )


def free_rooms(start: date, end: date | None, *, room_type=None, exclude_room=None):
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
        if not stay.is_void:
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
    # Room-type check: nights on which every room of this type is taken, and future bookings
    # that can be moved to another room of the same type so the guest can stay in their room.
    full_nights: list = field(default_factory=list)
    moves: list | None = None  # [(blocking_stay, new_room), …] or None if not possible

    @property
    def extra_nights(self) -> int:
        return (self.new_check_out - self.stay.check_out).days

    @property
    def can_extend_in_place(self) -> bool:
        return self.free or self.moves is not None


def _rooms_of_type(room_type):
    return Room.objects.filter(room_type=room_type, is_active=True, status=Room.Status.ACTIVE)


def type_full_nights(stay: Stay, start: date, end: date) -> list[date]:
    """Nights in [start, end) on which all rooms of the stay's type are taken by other stays."""
    capacity = _rooms_of_type(stay.room.room_type).count()
    others = list(
        Stay.objects.live().overlapping(start, end).filter(room__room_type=stay.room.room_type)
        .exclude(pk=stay.pk).values_list("check_in", "check_out")
    )
    full, d = [], start
    while d < end:
        taken = sum(1 for ci, co in others if ci <= d and (co is None or d < co))
        if taken + 1 > capacity:
            full.append(d)
        d += timedelta(days=1)
    return full


def _reassign_blocking(stay: Stay, blocking: list) -> list | None:
    """Find rooms of the same type for future bookings in the guest's room, so the guest can stay put."""
    if any(b.status != Stay.Status.UPCOMING for b in blocking):
        return None  # never move someone who is already in-house
    candidates = list(_rooms_of_type(stay.room.room_type).exclude(pk=stay.room_id).order_by("sort_order", "number"))
    planned: dict[int, list] = {}
    moves = []
    for b in sorted(blocking, key=lambda x: x.check_in):
        def fits(room, b=b):
            if conflicts(room, b.check_in, b.check_out, exclude_ids=[b.pk]).exists():
                return False
            return not any(b.check_in < (co or date.max) and ci < (b.check_out or date.max)
                           for ci, co in planned.get(room.pk, []))
        room = next((r for r in candidates if fits(r)), None)
        if room is None:
            return None
        planned.setdefault(room.pk, []).append((b.check_in, b.check_out))
        moves.append((b, room))
    return moves


def plan_extension(stay: Stay, new_check_out: date) -> ExtensionPlan:
    if stay.is_open_ended:
        raise ValueError("Monthly stays have no check-out to extend — use “Add month’s rent”, or set a check-out date.")
    if new_check_out <= stay.check_out:
        raise ValueError("The new check-out date must be after the current one.")
    blocking = list(conflicts(stay.room, stay.check_out, new_check_out, exclude_ids=[stay.pk]))
    plan = ExtensionPlan(stay=stay, new_check_out=new_check_out, free=not blocking, blocking=blocking)
    if blocking:
        plan.full_nights = type_full_nights(stay, stay.check_out, new_check_out)
        if not plan.full_nights:
            plan.moves = _reassign_blocking(stay, blocking)
    if not plan.can_extend_in_place:
        plan.split_rooms = list(
            free_rooms(stay.check_out, new_check_out, room_type=stay.room.room_type, exclude_room=stay.room)
        )
        # The guest moves from today (never before check-in, never after the current check-out).
        plan.move_start = min(max(stay.check_in, today()), stay.check_out)
        rooms = free_rooms(plan.move_start, new_check_out, exclude_room=stay.room)
        # Same room type first, then the rest.
        plan.move_rooms = sorted(rooms, key=lambda r: (r.room_type_id != stay.room.room_type_id, r.sort_order, r.number))
    return plan


def _money(amount: Decimal, paid: Decimal) -> str:
    text = f" · +₹{amount:,.0f} added to total"
    return text + (f" · ₹{paid:,.0f} paid" if paid else "")


def _apply_payment(stay: Stay, paid: Decimal, mode: str) -> None:
    if paid:
        stay.amount_paid = (stay.amount_paid or Decimal(0)) + paid
        if mode:
            stay.payment_mode = mode


def extend_in_place(stay: Stay, new_check_out: date, amount=Decimal(0), paid=Decimal(0), mode="") -> Stay:
    old = stay.check_out
    stay.check_out = new_check_out
    stay.total_amount = (stay.total_amount or Decimal(0)) + amount
    _apply_payment(stay, paid, mode)
    return save_stay(stay, action="extend",
                     summary=f"Extended in {stay.room}: check-out {old:%d-%m-%Y} → {new_check_out:%d-%m-%Y}{_money(amount, paid)}")


def extend_with_moves(stay: Stay, new_check_out: date, moves: list, amount=Decimal(0), paid=Decimal(0), mode="") -> Stay:
    """Move future bookings out of the guest's room (to rooms of the same type), then extend in place."""
    with transaction.atomic():
        for b, room in moves:
            old = b.room
            b.room = room
            save_stay(b, action="move", summary=f"Room changed {old} → {room} so {stay.guest.name} could extend in {old}")
        return extend_in_place(stay, new_check_out, amount, paid, mode)


def extend_split(stay: Stay, new_check_out: date, room: Room, amount=Decimal(0), paid=Decimal(0), mode="") -> Stay:
    """Keep the current room until its check-out, then continue in `room` for the extra nights."""
    with transaction.atomic():
        continuation = Stay(
            guest=stay.guest,
            room=room,
            check_in=stay.check_out,
            check_out=new_check_out,
            num_guests=stay.num_guests,
            total_amount=amount,
            amount_paid=paid or Decimal(0),
            payment_mode=mode or stay.payment_mode,
            source=stay.source,
            source_ref=stay.source_ref,
            status=Stay.Status.UPCOMING,
            form_c_filed=stay.form_c_filed,
            linked_to=stay.root,
            notes=f"Continuation of stay in {stay.room}.",
        )
        save_stay(
            continuation,
            action="extend",
            summary=f"Extended by moving to {room} for {stay.check_out:%d-%m-%Y} → {new_check_out:%d-%m-%Y} ({stay.room} was booked){_money(amount, paid)}",
        )
        AuditLog.record("extend", f"Continues in {room} from {stay.check_out:%d-%m-%Y} to {new_check_out:%d-%m-%Y}", stay)
    return continuation


def extend_move(stay: Stay, new_check_out: date, room: Room, move_start: date,
                amount=Decimal(0), paid=Decimal(0), mode="") -> Stay:
    """Move the guest to `room` from `move_start` until the new check-out."""
    if move_start >= stay.check_out:
        return extend_split(stay, new_check_out, room, amount, paid, mode)
    with transaction.atomic():
        if move_start <= stay.check_in:
            old_room = stay.room
            stay.room = room
            stay.check_out = new_check_out
            stay.total_amount = (stay.total_amount or Decimal(0)) + amount
            _apply_payment(stay, paid, mode)
            return save_stay(stay, action="extend",
                             summary=f"Moved {old_room} → {room} and extended to {new_check_out:%d-%m-%Y}{_money(amount, paid)}")

        continuation = Stay(
            guest=stay.guest,
            room=room,
            check_in=move_start,
            check_out=new_check_out,
            num_guests=stay.num_guests,
            total_amount=amount,
            amount_paid=paid or Decimal(0),
            payment_mode=mode or stay.payment_mode,
            source=stay.source,
            source_ref=stay.source_ref,
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
        save_stay(continuation, action="extend",
                  summary=f"Moved in from {stay.room}; stays until {new_check_out:%d-%m-%Y}{_money(amount, paid)}")
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
    if stay.is_open_ended:
        stay.check_out = max(t, stay.check_in + timedelta(days=1))
        note = f" — monthly stay ended {stay.check_out:%d-%m-%Y}"
    elif stay.check_in < t < stay.check_out:
        note = f" — early checkout, was booked until {stay.check_out:%d-%m-%Y}"
        stay.check_out = t
    stay.status = Stay.Status.CHECKED_OUT
    stay.checked_out_at = timezone.now()
    return save_stay(stay, action="check_out", summary=f"Checked out of {stay.room}{note}")


def booked_check_out(stay: Stay) -> date | None:
    """The check-out date the stay had before it was checked out (early checkout shortens it).
    Read back from the check-out log; None means it was an open (monthly) stay."""
    log = stay.logs.filter(action__in=["check_out", "transfer"]).order_by("-at").first()
    text = log.summary if log else ""
    if "monthly stay ended" in text:
        return None
    m = re.search(r"was booked until (\d{2}-\d{2}-\d{4}|open)", text)
    if not m:
        return stay.check_out
    return None if m[1] == "open" else datetime.strptime(m[1], "%d-%m-%Y").date()


def undo_check_out(stay: Stay, check_out: date | None) -> Stay:
    """Checked out by mistake: back to checked in, with the given check-out date (None = open monthly stay)."""
    if stay.status != Stay.Status.CHECKED_OUT:
        raise ValueError("Only checked-out stays can be undone.")
    if check_out is None and stay.kind != Stay.Kind.MONTHLY:
        raise ValueError("Pick the check-out date.")
    if check_out is not None and check_out <= stay.check_in:
        raise ValueError("The check-out date must be after check-in.")
    old = stay.check_out
    stay.status = Stay.Status.CHECKED_IN
    stay.check_out = check_out
    stay.checked_out_at = None
    stay.transferred_to = stay.transfer_reason = ""
    until = f"{check_out:%d-%m-%Y}" if check_out else "open (monthly)"
    return save_stay(stay, action="undo_check_out",
                     summary=f"Check-out undone — back in {stay.room}, check-out {old:%d-%m-%Y} → {until}")


def undo_check_in(stay: Stay) -> Stay:
    if stay.status != Stay.Status.CHECKED_IN:
        raise ValueError("Only checked-in stays can be undone.")
    stay.status = Stay.Status.UPCOMING
    stay.checked_in_at = None
    return save_stay(stay, action="undo_check_in", summary="Check-in undone — back to upcoming")


def shorten(stay: Stay, new_check_out: date) -> Stay:
    """Set an earlier check-out (or, for a monthly stay, its leaving date)."""
    if new_check_out <= stay.check_in or (stay.check_out and new_check_out >= stay.check_out):
        raise ValueError("The leaving date must be after check-in and before the current check-out.")
    old = f"{stay.check_out:%d-%m-%Y}" if stay.check_out else "open"
    stay.check_out = new_check_out
    return save_stay(stay, action="shorten", summary=f"Check-out set: {old} → {new_check_out:%d-%m-%Y}")


def add_month_rent(stay: Stay) -> Stay:
    """Monthly guests: add one month's rent to the stay total."""
    if not stay.monthly_rent:
        raise ValueError("Set the monthly rent on the stay first (Edit).")
    stay.total_amount = (stay.total_amount or Decimal(0)) + stay.monthly_rent
    return save_stay(stay, action="rent", summary=f"Added a month’s rent ₹{stay.monthly_rent:,.0f} to the total")


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


def transfer_out(stay: Stay, hotel: str, reason: str = "") -> Stay:
    """Send the guest to another hotel and free the room.
    If they have already stayed some nights, those are kept (early checkout today); otherwise the
    whole booking is marked transferred and the room is free for all its dates."""
    hotel, reason = hotel.strip(), reason.strip()
    if not hotel:
        raise ValueError("Enter the name of the hotel the guest was sent to.")
    if not stay.is_active:
        raise ValueError("Only upcoming or checked-in stays can be transferred.")
    stay.transferred_to, stay.transfer_reason = hotel, reason
    why = f" ({reason})" if reason else ""
    t = today()
    if stay.status == Stay.Status.CHECKED_IN and stay.check_in < t:
        old = f"{stay.check_out:%d-%m-%Y}" if stay.check_out else "open"
        if stay.check_out is None or stay.check_out > t:
            stay.check_out = t
        stay.status = Stay.Status.CHECKED_OUT
        stay.checked_out_at = timezone.now()
        summary = f"Transferred to {hotel}{why} — checked out of {stay.room} today (was booked until {old})"
    else:
        stay.status = Stay.Status.TRANSFERRED
        summary = f"Transferred to {hotel}{why} — {stay.room} is free again"
    return save_stay(stay, action="transfer", summary=summary)
