"""PG / co-living rules: who is in which bed, move-in, notice, shifting beds, move-out with deposit settlement,
adding / removing beds, and the rent calendar (when each period's rent is due).

Every change is one transaction on the item's own database and is written to the item's activity log.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Q

from cluster import current
from core.db import atomic
from core.models import AuditLog
from core.services import today

from .models import Bed, BedSpell, Resident, Room, Tenancy

T = Tenancy.Status


def _log(action: str, summary: str) -> None:
    AuditLog.record(action, summary)


def _lock(bed: Bed) -> None:
    """Serialise changes per bed (Postgres row lock; no-op on SQLite)."""
    Bed.objects.select_for_update().filter(pk=bed.pk).first()


def _taken_error() -> ValidationError:
    return ValidationError("That bed is already taken (or booked) for those dates.")


def _check_free(bed: Bed, start: date, exclude_tenancy=None) -> None:
    """Python check before the database's own one (which also catches two people at the same moment)."""
    if bed.is_removed:
        raise ValidationError("That bed has been removed — restore it first.")
    clash = bed.spells.live().filter(Q(end__isnull=True) | Q(end__gt=start))
    if exclude_tenancy is not None:
        clash = clash.exclude(tenancy=exclude_tenancy)
    if clash.exists():
        raise _taken_error()


# ── Bed states ────────────────────────────────────────────────────────────────


@dataclass
class BedView:
    bed: Bed
    state: str  # vacant · occupied · notice · reserved · maintenance · removed
    tenancy: Tenancy | None = None  # who is in it now (or booked next, for "reserved")

    @property
    def label(self) -> str:
        return {"vacant": "Vacant", "occupied": "Occupied", "notice": "On notice", "reserved": "Reserved",
                "maintenance": "Under maintenance", "removed": "Removed"}[self.state]


def bed_views(beds, day: date | None = None) -> list[BedView]:
    """Each bed's state on `day`, with its current (or next booked) tenancy — two queries for any number of beds."""
    day = day or today()
    beds = list(beds)
    ids = [b.pk for b in beds]
    now, booked = {}, {}
    spells = (BedSpell.objects.live().filter(bed_id__in=ids).filter(Q(end__isnull=True) | Q(end__gt=day))
              .select_related("tenancy__resident"))
    for s in spells.order_by("start"):
        if s.start <= day:
            now[s.bed_id] = s.tenancy
        else:
            booked.setdefault(s.bed_id, s.tenancy)
    out = []
    for b in beds:
        t = now.get(b.pk)
        if b.is_removed:
            out.append(BedView(b, "removed"))
        elif t is not None:
            out.append(BedView(b, "notice" if t.status == T.NOTICE else "occupied", t))
        elif b.pk in booked:
            out.append(BedView(b, "reserved", booked[b.pk]))
        elif b.status == Bed.Status.MAINTENANCE or b.room.status == Room.Status.MAINTENANCE:
            out.append(BedView(b, "maintenance"))
        else:
            out.append(BedView(b, "vacant"))
    return out


def counts(views: list[BedView]) -> dict[str, int]:
    live = [v for v in views if v.state != "removed"]
    c = {"total": len(live), "taken": sum(v.state in ("occupied", "notice") for v in live)}
    for state in ("vacant", "reserved", "notice", "maintenance"):
        c[state] = sum(v.state == state for v in live)
    return c


# ── Move-in, reservation, cancel ──────────────────────────────────────────────


def move_in(tenancy: Tenancy, *, by: str = "", deposit_mode: str = "cash") -> Tenancy:
    """Save a new tenancy and give it the bed from its move-in date. A future date makes it a reservation."""
    if not tenancy.resident.ready_to_move_in and tenancy.move_in <= today():
        raise ValidationError("Add the resident's permanent address and ID number before they move in.")
    other = tenancy.resident.tenancies.open().exclude(pk=tenancy.pk).select_related("bed__room").first()
    if other is not None:
        raise ValidationError(f"{tenancy.resident.name} already has bed {other.bed} ({other.get_status_display()}) — "
                              "shift them instead.")
    tenancy.status = T.RESERVED if tenancy.move_in > today() else T.STAYING
    tenancy.created_by = by
    with atomic():
        _lock(tenancy.bed)
        _check_free(tenancy.bed, tenancy.move_in)
        try:
            with atomic():
                tenancy.save()
                BedSpell.objects.create(tenancy=tenancy, bed=tenancy.bed, start=tenancy.move_in)
        except IntegrityError as exc:
            raise _taken_error() from exc
        what = "reserved" if tenancy.status == T.RESERVED else "moved in to"
        _log("pg_move_in", f"{tenancy.resident.name} {what} bed {tenancy.bed} from {tenancy.move_in:%d-%m-%Y}")
        if tenancy.deposit_received > 0:  # the advance (or a reservation's token) paid now: kept as a payment
            from .models import RentPayment
            RentPayment.objects.create(tenancy=tenancy, kind=RentPayment.Kind.DEPOSIT, amount=tenancy.deposit_received,
                                       paid_on=min(today(), tenancy.move_in), mode=deposit_mode,
                                       note="At move-in" if tenancy.status == T.STAYING else "Token advance",
                                       recorded_by=by)
        from . import money
        money.sync(tenancy)  # the first rent period falls due on the move-in day
    return tenancy


def confirm_move_in(tenancy: Tenancy, day: date | None = None) -> None:
    """A reservation becomes a stay (on the planned day, or the day they actually arrived)."""
    day = day or today()
    if tenancy.status != T.RESERVED:
        raise ValidationError("Only a reservation can be moved in.")
    if not tenancy.resident.ready_to_move_in:
        raise ValidationError("Add the resident's permanent address and ID number before they move in.")
    with atomic():
        _lock(tenancy.bed)
        spell = tenancy.spells.get(end__isnull=True)
        if day != spell.start:
            _check_free(tenancy.bed, min(day, spell.start), exclude_tenancy=tenancy)
        try:
            with atomic():
                spell.start = tenancy.move_in = day
                spell.save(update_fields=["start"])
                tenancy.status = T.STAYING
                tenancy.save(update_fields=["status", "move_in"])
        except IntegrityError as exc:
            raise _taken_error() from exc
        _log("pg_move_in", f"{tenancy.resident.name} moved in to bed {tenancy.bed} on {day:%d-%m-%Y}")
        from . import money
        money.sync(tenancy)


def cancel(tenancy: Tenancy, reason: str = "") -> None:
    if tenancy.status != T.RESERVED:
        raise ValidationError("Only a reservation can be cancelled.")
    with atomic():
        for s in tenancy.spells.all():
            s.end = s.start  # an empty range: never blocks the bed
            s.save(update_fields=["end"])
        tenancy.status = T.CANCELLED
        if reason:
            tenancy.notes = (tenancy.notes + "\n" if tenancy.notes else "") + f"Cancelled: {reason}"
        tenancy.save(update_fields=["status", "notes"])
        _log("pg_cancel", f"Reservation of {tenancy.resident.name} for bed {tenancy.bed} cancelled"
                          + (f" ({reason})" if reason else ""))


# ── Notice, shift, move-out ───────────────────────────────────────────────────


def give_notice(tenancy: Tenancy, given_on: date, last_day: date | None = None) -> None:
    if tenancy.status not in (T.STAYING, T.NOTICE):
        raise ValidationError("Notice can be given only while staying.")
    last_day = last_day or given_on + timedelta(days=tenancy.notice_days)
    if last_day < tenancy.move_in:
        raise ValidationError("The last day can't be before the move-in date.")
    tenancy.status, tenancy.notice_given_on, tenancy.last_day = T.NOTICE, given_on, last_day
    with atomic():
        tenancy.save(update_fields=["status", "notice_given_on", "last_day"])
        _log("pg_notice", f"{tenancy.resident.name} gave notice on {given_on:%d-%m-%Y}; last day {last_day:%d-%m-%Y}")
        from . import money
        money.sync(tenancy)  # the period with the last day in it is now charged by the day


def withdraw_notice(tenancy: Tenancy) -> None:
    if tenancy.status != T.NOTICE:
        raise ValidationError("There is no notice to withdraw.")
    tenancy.status, tenancy.notice_given_on, tenancy.last_day = T.STAYING, None, None
    with atomic():
        tenancy.save(update_fields=["status", "notice_given_on", "last_day"])
        _log("pg_notice", f"{tenancy.resident.name} withdrew their notice")
        from . import money
        money.sync(tenancy)  # back to full periods


def shift(tenancy: Tenancy, new_bed: Bed, day: date) -> None:
    """Move a staying resident to another bed from `day` (same tenancy; both beds keep the history)."""
    if tenancy.status not in (T.STAYING, T.NOTICE, T.RESERVED):
        raise ValidationError("Only a current or reserved tenancy can change beds.")
    if new_bed.pk == tenancy.bed_id:
        raise ValidationError("That is already their bed.")
    with atomic():
        for b in sorted([tenancy.bed, new_bed], key=lambda b: b.pk):  # same order everywhere: no deadlock
            _lock(b)
        spell = tenancy.spells.get(end__isnull=True)
        if tenancy.status == T.RESERVED or day <= spell.start:
            day = spell.start  # not moved in yet (or shifting on day one): just book the other bed instead
        _check_free(new_bed, day)
        old = tenancy.bed
        try:
            with atomic():
                if day == spell.start:
                    spell.bed = new_bed
                    spell.save(update_fields=["bed"])
                else:
                    spell.end = day
                    spell.save(update_fields=["end"])
                    BedSpell.objects.create(tenancy=tenancy, bed=new_bed, start=day)
                tenancy.bed = new_bed
                tenancy.save(update_fields=["bed"])
        except IntegrityError as exc:
            raise _taken_error() from exc
        _log("pg_shift", f"{tenancy.resident.name} shifted from bed {old} to {new_bed} on {day:%d-%m-%Y}")


def standard_deductions(item) -> list[dict]:
    """The item's standard deductions ("Cleaning: 500" per line) as form rows."""
    rows = []
    for line in (getattr(item, "deductions", "") or "").splitlines():
        reason, _, amount = line.rpartition(":")
        try:
            value = Decimal(amount.strip().replace(",", "").lstrip("₹"))
        except Exception:
            continue
        if reason.strip() and value > 0:
            rows.append({"reason": reason.strip(), "amount": str(value)})
    return rows


def settle(tenancy: Tenancy, *, unpaid_rent: Decimal, deductions: list[dict]) -> Decimal:
    """advance received − rent still owed − deductions. Negative: the resident still owes that much.
    Rent paid ahead (negative owed) comes back with the advance."""
    total = sum((Decimal(str(d["amount"])) for d in deductions), Decimal("0"))
    return tenancy.deposit_received - unpaid_rent - total


def move_out(tenancy: Tenancy, day: date, *, deductions=(), by: str = "", refund_days: int | None = None,
             waive: Decimal = Decimal("0"), waive_reason: str = "") -> Tenancy:
    """The resident leaves on `day` (the bed is free from that day). Rent is charged up to that day; `waive` of what
    is still owed can be let off (with a reason), the rest is taken from the advance, rent paid ahead goes back with
    it, and what remains is the refund (§8.6)."""
    from . import money
    from .models import RentPayment
    if tenancy.status not in (T.STAYING, T.NOTICE):
        raise ValidationError("Only someone staying can move out.")
    if day < tenancy.move_in:
        raise ValidationError("The move-out date can't be before the move-in date.")
    deductions = [{"reason": d["reason"].strip(), "amount": str(Decimal(str(d["amount"])))}
                  for d in deductions if d.get("reason", "").strip() and Decimal(str(d.get("amount") or 0)) > 0]
    waive, waive_reason = Decimal(str(waive or 0)), (waive_reason or "").strip()[:200]
    if waive < 0:
        raise ValidationError("The rent waived can't be negative.")
    if waive > 0 and not waive_reason:
        raise ValidationError("Give a reason for waiving the rent.")
    item = current.get()
    if refund_days is None:
        refund_days = getattr(item, "refund_days", 7) or 0
    with atomic():
        _lock(tenancy.bed)
        spell = tenancy.spells.get(end__isnull=True)
        if day < spell.start:
            raise ValidationError(f"They moved into this bed on {spell.start:%d-%m-%Y}; pick a later date.")
        spell.end = day
        spell.save(update_fields=["end"])
        tenancy.status, tenancy.move_out = T.MOVED_OUT, day
        tenancy.last_day = tenancy.last_day or day
        tenancy.save(update_fields=["status", "move_out", "last_day"])
        money.sync(tenancy, max(day, today()))  # rent up to the move-out day (the last period by the day)
        owed = money.balance(tenancy)
        if waive > max(owed, Decimal("0")):
            raise ValidationError(f"Only ₹{max(owed, Decimal('0')):,.0f} of rent is owed — you can't waive more.")
        if waive > 0:
            RentPayment.objects.create(tenancy=tenancy, kind=RentPayment.Kind.RENT, amount=waive, paid_on=day,
                                       mode=RentPayment.Mode.WAIVED, recorded_by=by, note=waive_reason)
            owed -= waive
        tenancy.rent_waived, tenancy.waive_reason = waive, waive_reason if waive > 0 else ""
        tenancy.unpaid_rent, tenancy.deductions = owed, deductions
        tenancy.refund = settle(tenancy, unpaid_rent=owed, deductions=deductions)
        # Settle the rent against the advance, so the rent account ends at zero (or at what is still owed).
        available = tenancy.deposit_received - sum((Decimal(d["amount"]) for d in deductions), Decimal("0"))
        settled = owed if owed < 0 else min(owed, max(available, Decimal("0")))
        if settled:
            RentPayment.objects.create(tenancy=tenancy, kind=RentPayment.Kind.RENT, amount=settled, paid_on=day,
                                       mode=RentPayment.Mode.ADVANCE, recorded_by=by,
                                       note="Taken from the advance" if settled > 0 else "Paid ahead — back with the advance")
        tenancy.refund_due_by = day + timedelta(days=refund_days) if tenancy.refund > 0 else None
        tenancy.settled_by = by
        tenancy.save()
        _log("pg_move_out", f"{tenancy.resident.name} moved out of bed {tenancy.bed} on {day:%d-%m-%Y}; "
                            + (f"rent of ₹{waive:,.0f} waived ({waive_reason}); " if waive > 0 else "")
                            + (f"deposit to refund ₹{tenancy.refund:,.0f}" if tenancy.refund > 0 else
                               f"still owes ₹{-tenancy.refund:,.0f}" if tenancy.refund < 0 else "nothing to refund"))
    return tenancy


def refund_paid(tenancy: Tenancy, paid_on: date, mode: str) -> None:
    if not tenancy.refund_pending:
        raise ValidationError("There is no deposit refund waiting for this tenancy.")
    tenancy.refund_paid_on, tenancy.refund_mode = paid_on, mode
    with atomic():
        tenancy.save(update_fields=["refund_paid_on", "refund_mode"])
        _log("pg_refund", f"Deposit refund ₹{tenancy.refund:,.0f} paid to {tenancy.resident.name} ({mode})")


# ── Beds: add, remove, restore ────────────────────────────────────────────────


def add_beds(room: Room, n: int = 1) -> list[Bed]:
    made = []
    with atomic():
        for _ in range(n):
            made.append(Bed.objects.create(room=room, label=room.next_label(), added_on=today()))
        if made:
            _log("pg_bed", f"Room {room.number}: added bed{'s' if n > 1 else ''} {', '.join(b.label for b in made)}")
    return made


def remove_bed(bed: Bed, reason: str, by: str = "") -> None:
    """Only a vacant bed (no one in it now or booked later). It is kept, with its history, under Removed beds."""
    if bed.is_removed:
        raise ValidationError("That bed is already removed.")
    if not reason.strip():
        raise ValidationError("Give a reason for removing the bed.")
    with atomic():
        _lock(bed)
        if bed.busy_from(today()):
            raise ValidationError("Someone is in this bed or has booked it — move them out or shift them first.")
        bed.removed_on, bed.removed_by, bed.removed_reason = today(), by, reason.strip()
        bed.save(update_fields=["removed_on", "removed_by", "removed_reason"])
        _log("pg_bed", f"Bed {bed} removed: {reason.strip()}")


def restore_bed(bed: Bed) -> None:
    if not bed.is_removed:
        raise ValidationError("That bed isn't removed.")
    with atomic():
        bed.removed_on, bed.removed_by, bed.removed_reason = None, "", ""
        bed.save(update_fields=["removed_on", "removed_by", "removed_reason"])
        _log("pg_bed", f"Bed {bed} restored")


# ── Rent calendar ─────────────────────────────────────────────────────────────


def _add_months(d: date, months: int, day: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


@dataclass
class Period:
    start: date  # rent for [start, end) is due on `start`
    end: date
    amount: Decimal
    part: bool = False  # pro-rated (first period up to the fixed date, or the last one up to the move-out)


def _money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def natural_periods(tenancy: Tenancy, until: date) -> list[Period]:
    """Rent periods from move-in, each due on its first day, up to `until` — ignoring any leaving day.

    Monthly + "same date as move-in": the 14th → 14th … (31st moves in: the last day of shorter months).
    Monthly + "fixed date": move-in to the next fixed date is charged by the day, then full months from that date.
    Weekly / daily: every 7 days / every day from move-in.
    """
    out: list[Period] = []
    start = tenancy.move_in
    if tenancy.cycle == Tenancy.Cycle.MONTHLY:
        anchor = start.day
        if tenancy.due_rule == Tenancy.DueRule.FIXED and tenancy.due_day:
            anchor = tenancy.due_day
            first = date(start.year, start.month, anchor)
            if first <= start:
                first = _add_months(first, 1, anchor)
            if start.day != anchor:
                days_in = (first - _add_months(first, -1, anchor)).days  # the full month it is part of
                if start <= until:
                    out.append(Period(start, first, _money(tenancy.rent * (first - start).days / days_in), True))
                start = first
        base, k = start, 0
        while start <= until:
            k += 1
            nxt = _add_months(base, k, anchor)
            out.append(Period(start, nxt, tenancy.rent))
            start = nxt
    else:
        step = 7 if tenancy.cycle == Tenancy.Cycle.WEEKLY else 1
        while start <= until:
            out.append(Period(start, start + timedelta(days=step), tenancy.rent))
            start += timedelta(days=step)
    return out


def periods(tenancy: Tenancy, until: date) -> list[Period]:
    """The rent calendar up to `until`, with the last period charged by the day when they leave part-way through it
    (move-out date, else the last day on notice)."""
    stop = tenancy.move_out or tenancy.last_day
    out = []
    for p in natural_periods(tenancy, min(until, stop - timedelta(days=1)) if stop else until):
        if stop and p.end > stop:
            out.append(Period(p.start, stop, _money(p.amount * (stop - p.start).days / (p.end - p.start).days), True))
        else:
            out.append(p)
    return out


def next_due(tenancy: Tenancy, day: date | None = None) -> Period | None:
    """The next rent period starting after `day` (None once they've moved out or the last day has passed)."""
    day = day or today()
    if not tenancy.is_open:
        return None
    for p in periods(tenancy, day + timedelta(days=62)):
        if p.start > day:  # today's is already due: the next one
            return p
    return None


def find_resident(phone: str) -> Resident | None:
    key = Resident.key_for(phone)
    return Resident.objects.filter(phone_key=key).first() if len(key) >= 10 else None


# ── Vacancy view (to share with people asking for a bed) ──────────────────────


@dataclass
class VacancyGroup:
    room_type: object
    now: list = field(default_factory=list)  # vacant beds
    soon: list = field(default_factory=list)  # (bed, free from) — on notice, no one booked after
    advance: Decimal | None = None  # the lowest usual advance of these rooms

    @property
    def features(self) -> str:
        rooms = {b.room for b in self.now} | {b.room for b, _ in self.soon}
        bits = []
        if rooms and all(r.attached_bath for r in rooms):
            bits.append("attached bathroom")
        if rooms and any(r.balcony for r in rooms):
            bits.append("balcony in some rooms" if not all(r.balcony for r in rooms) else "balcony")
        return ", ".join(bits)


def vacancies(day: date | None = None, soon_days: int = 30) -> list[VacancyGroup]:
    """Vacant beds by room type, and beds coming free within `soon_days` (on notice, nobody booked after)."""
    day = day or today()
    beds = Bed.objects.in_use().select_related("room", "room__room_type", "room__floor")
    views = bed_views(beds, day)
    on_notice = [v for v in views if v.state == "notice" and v.tenancy.last_day
                 and v.tenancy.last_day <= day + timedelta(days=soon_days)]
    soon_ids = {v.bed.pk for v in on_notice}
    booked = set(BedSpell.objects.live().filter(bed__in=[v.bed for v in on_notice], start__gt=day)
                 .values_list("bed_id", flat=True))
    groups: dict[int, VacancyGroup] = {}
    for v in views:
        rt = v.bed.room.room_type
        if v.state == "vacant":
            groups.setdefault(rt.pk, VacancyGroup(rt)).now.append(v.bed)
        elif v.bed.pk in soon_ids and v.bed.pk not in booked:
            groups.setdefault(rt.pk, VacancyGroup(rt)).soon.append((v.bed, v.tenancy.last_day))
    for g in groups.values():
        g.soon.sort(key=lambda x: x[1])
        deposits = [b.room.deposit for b in g.now] + [b.room.deposit for b, _ in g.soon]
        g.advance = min((d for d in deposits if d), default=None)
    return sorted(groups.values(), key=lambda g: (-len(g.now), g.room_type.name))


def vacancy_text(groups: list[VacancyGroup], name: str, item=None) -> str:
    """The message to paste or send to someone looking for a bed (no names, no rents: rent is agreed per person)."""
    def rooms(beds):
        nums = sorted({b.room.number for b in beds}, key=lambda n: (len(n), n))
        return f"room{'s' if len(nums) > 1 else ''} {', '.join(nums)}"

    lines = [f"Beds available at {name} ({today():%d %b %Y}):", ""]
    if not groups:
        lines.append("All beds are taken right now. Ask us to keep you in mind for the next one.")
    for g in groups:
        head = g.room_type.name + (f" ({g.features})" if g.features else "")
        lines.append(f"*{head}*")
        if g.now:
            lines.append(f"• {len(g.now)} bed{'s' if len(g.now) > 1 else ''} now — {rooms(g.now)}")
        for d, beds in _by_date(g.soon):
            when = "today" if d <= today() else f"from {d:%d %b}"
            lines.append(f"• {len(beds)} bed{'s' if len(beds) > 1 else ''} free {when} — {rooms(beds)}")
        if g.advance:
            lines.append(f"• Advance: ₹{g.advance:,.0f} (refundable)")
        lines.append("")
    for label, value in (("Address", getattr(item, "address", "")), ("Map", getattr(item, "map_link", "")),
                         ("Call / WhatsApp", getattr(item, "phones", ""))):
        if value:
            lines.append(f"{label}: {' '.join(str(value).split())}")
    return "\n".join(lines).strip()


def _by_date(soon):
    out: dict = {}
    for bed, d in soon:
        out.setdefault(d, []).append(bed)
    return sorted(out.items())
