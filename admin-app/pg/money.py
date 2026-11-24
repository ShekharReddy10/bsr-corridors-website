"""PG / co-living rent and money (§8.5, §8.8).

Rent dues: each period is added on its due date (`sync`), with the rent agreed at that time — so changing a resident's
rent later never changes months already charged. When the resident leaves part-way through a period (notice / move
out), that period is charged by the day; withdrawing the notice charges it in full again.

Balance = rent dues − rent paid. Payments aren't tied to a period: they pay the oldest dues first (part payments carry
forward). The advance is kept apart (it isn't rent) and settled at move-out.

Money for a month (behind the money password), like the hotel's:
  received — rent paid for days already stayed in that month
  held     — rent paid for days still to come (paid ahead)
  pending  — days stayed in that month but not paid yet
"""

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote, urlencode

from django.core import signing
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

from cluster import current
from core.db import atomic
from core.models import AuditLog
from core.services import today

from .models import RentDue, RentPayment, RentRequest, Tenancy

ZERO = Decimal("0")
T = Tenancy.Status


def _money(x) -> Decimal:
    return Decimal(x).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _cut(full: Decimal, start: date, end: date, stop: date | None) -> Decimal:
    """The period's charge when the resident leaves on `stop` (charged by the day); full otherwise."""
    if stop is None or stop >= end:
        return full
    return _money(full * Decimal((stop - start).days) / Decimal((end - start).days))


def stop_of(t: Tenancy) -> date | None:
    """The day the bed is given back: move-out date, else the last day on notice (None while staying on)."""
    if t.status == T.MOVED_OUT:
        return t.move_out
    if t.status == T.NOTICE:
        return t.last_day
    return None


# ── Dues ──────────────────────────────────────────────────────────────────────


def sync(t: Tenancy, day: date | None = None) -> None:
    """Add the periods that have fallen due by `day`, and fit the last one to the leaving day (or back to full)."""
    from .services import natural_periods  # the rent calendar
    day = day or today()
    if t.status in (T.RESERVED, T.CANCELLED) or t.move_in > day:
        return
    stop = stop_of(t)
    with atomic():
        have = {d.start: d for d in t.dues.all()}
        for d in have.values():
            if stop is not None and stop <= d.start:
                d.delete()
                continue
            amount = _cut(d.full, d.start, d.end, stop)
            if amount != d.amount:
                d.amount = amount
                d.save(update_fields=["amount"])
        until = min(day, stop - timedelta(days=1)) if stop else day
        for p in natural_periods(t, until):
            if p.start not in have and (stop is None or p.start < stop):
                RentDue.objects.create(tenancy=t, start=p.start, end=p.end, full=p.amount,
                                       amount=_cut(p.amount, p.start, p.end, stop))


def sync_all(day: date | None = None) -> int:
    """Every current tenancy (and those that moved out recently): Today page and the nightly job."""
    day = day or today()
    n = 0
    for t in Tenancy.objects.filter(status__in=[T.STAYING, T.NOTICE]):
        sync(t, day)
        n += 1
    return n


def dues_total(t: Tenancy) -> Decimal:
    return t.dues.aggregate(s=Sum("amount"))["s"] or ZERO


def rent_paid(t: Tenancy) -> Decimal:
    return t.payments.filter(kind=RentPayment.Kind.RENT).aggregate(s=Sum("amount"))["s"] or ZERO


def balance(t: Tenancy) -> Decimal:
    """Rent still owed (negative: paid ahead). Call `sync` first for an up-to-date figure."""
    return dues_total(t) - rent_paid(t)


def owed_if_leaving(t: Tenancy, stop: date) -> Decimal:
    """Rent still owed if the resident leaves on `stop` (for the move-out form), without changing anything."""
    from .services import natural_periods
    have = {d.start: d for d in t.dues.all()}
    total = sum((_cut(d.full, d.start, d.end, stop) for d in have.values() if d.start < stop), ZERO)
    for p in natural_periods(t, stop - timedelta(days=1)):
        if p.start not in have and p.start < stop:
            total += _cut(p.amount, p.start, p.end, stop)
    return total - rent_paid(t)


@dataclass
class DueLine:
    due: RentDue
    paid: Decimal  # how much of it the payments cover (oldest dues first)
    waived: Decimal = ZERO  # let off at move-out (covers what payments didn't, after them)

    @property
    def left(self) -> Decimal:
        return self.due.amount - self.paid - self.waived

    @property
    def state(self) -> str:
        if self.left <= 0:
            return "waived" if self.waived and not self.paid else "paid"
        return "part" if self.paid > 0 or self.waived > 0 else "unpaid"


def lines(t: Tenancy) -> list[DueLine]:
    """Each rent period with what pays it: real payments (and the advance) oldest first, then any waiver."""
    waived = t.payments.filter(kind=RentPayment.Kind.RENT, mode=RentPayment.Mode.WAIVED).aggregate(
        s=Sum("amount"))["s"] or ZERO
    pool = rent_paid(t) - waived
    out = []
    for d in t.dues.order_by("start"):
        covered = min(pool, d.amount) if pool > 0 else ZERO
        pool -= covered
        let_off = min(waived, d.amount - covered) if waived > 0 else ZERO
        waived -= let_off
        out.append(DueLine(d, covered, let_off))
    return out


def unpaid_since(t: Tenancy) -> date | None:
    """The due date of the oldest period not fully paid."""
    for ln in lines(t):
        if ln.left > 0:
            return ln.due.start
    return None


@dataclass
class Owing:
    tenancy: Tenancy
    amount: Decimal
    since: date | None

    @property
    def overdue(self) -> bool:
        return self.since is not None and self.since < today()


def owing(day: date | None = None) -> list[Owing]:
    """Everyone with rent to pay now (current tenancies, and moved-out ones still owing), oldest first."""
    day = day or today()
    out = []
    qs = Tenancy.objects.filter(status__in=[T.STAYING, T.NOTICE, T.MOVED_OUT]).select_related("resident", "bed__room")
    for t in qs:
        if t.status != T.MOVED_OUT:
            sync(t, day)
        b = balance(t)
        if b > 0:
            out.append(Owing(t, b, unpaid_since(t)))
    return sorted(out, key=lambda o: (o.since or day, o.tenancy.resident.name))


def due_on(day: date) -> list[RentDue]:
    """Periods falling due on `day` (for "rent due today")."""
    return list(RentDue.objects.filter(start=day).select_related("tenancy__resident", "tenancy__bed__room"))


# ── Payments ──────────────────────────────────────────────────────────────────


def record(t: Tenancy, amount: Decimal, *, kind: str = RentPayment.Kind.RENT, mode: str = RentPayment.Mode.CASH,
           paid_on: date | None = None, reference: str = "", note: str = "", by: str = "") -> RentPayment:
    if amount is None or amount <= 0:
        raise ValidationError("Enter an amount more than zero.")
    if t.status in (T.CANCELLED,):
        raise ValidationError("This reservation was cancelled.")
    paid_on = paid_on or today()
    if paid_on > today():
        raise ValidationError("The payment date can't be in the future.")
    with atomic():
        p = RentPayment.objects.create(tenancy=t, amount=amount, kind=kind, mode=mode, paid_on=paid_on,
                                       reference=re.sub(r"[^0-9A-Za-z/-]", "", reference)[:40], note=note[:200],
                                       recorded_by=by)
        if kind == RentPayment.Kind.DEPOSIT:
            _sync_deposit(t)
        AuditLog.record("pg_payment", f"{t.resident.name}: {p.get_kind_display().lower()} ₹{amount:,.0f} received "
                                      f"({p.get_mode_display()})")
    return p


def delete(p: RentPayment) -> None:
    t = p.tenancy
    with atomic():
        p.delete()
        if p.kind == RentPayment.Kind.DEPOSIT:
            _sync_deposit(t)
        AuditLog.record("pg_payment", f"{t.resident.name}: payment of ₹{p.amount:,.0f} on {p.paid_on:%d-%m-%Y} deleted")


def _sync_deposit(t: Tenancy) -> None:
    """The advance received is the sum of advance payments (and the settlement uses it)."""
    t.deposit_received = t.payments.filter(kind=RentPayment.Kind.DEPOSIT).aggregate(s=Sum("amount"))["s"] or ZERO
    t.save(update_fields=["deposit_received"])


# ── Receipts (signed public link) ─────────────────────────────────────────────

RECEIPT_SALT = "pg-receipt"


def receipt_no(p: RentPayment) -> str:
    item = current.get()
    return f"{(item.code if item else 'PG').upper()}-R{p.pk:05d}"


def receipt_token(p: RentPayment) -> str:
    return signing.Signer(salt=RECEIPT_SALT).sign(str(p.pk))


def payment_for_token(value: str) -> RentPayment | None:
    try:
        pk = int(signing.Signer(salt=RECEIPT_SALT).unsign(value))
    except (signing.BadSignature, ValueError):
        return None
    return RentPayment.objects.filter(pk=pk).select_related("tenancy__resident", "tenancy__bed__room").first()


# ── UPI payment links (as the hotel's) ────────────────────────────────────────

PAY_SALT = "pg-pay"


def upi_ready() -> bool:
    return bool(current.upi()[0])


def create_request(t: Tenancy, amount: Decimal, by: str = "") -> RentRequest:
    if amount <= 0:
        raise ValidationError("Enter an amount more than zero.")
    if not upi_ready():
        raise ValidationError("Payment links need this property's UPI ID — add it on the property's page.")
    with atomic():
        t.requests.filter(status=RentRequest.Status.OPEN).update(status=RentRequest.Status.REPLACED)
        req = RentRequest.objects.create(tenancy=t, amount=amount, created_by=by)
        AuditLog.record("payment_link", f"UPI payment link sent to {t.resident.name} for ₹{amount:,.0f}")
    return req


def pay_token(req: RentRequest) -> str:
    return signing.Signer(salt=PAY_SALT).sign(str(req.pk))


def request_for_token(value: str) -> RentRequest | None:
    try:
        pk = int(signing.Signer(salt=PAY_SALT).unsign(value))
    except (signing.BadSignature, ValueError):
        return None
    return RentRequest.objects.filter(pk=pk).select_related("tenancy__resident", "tenancy__bed__room").first()


def upi_uri(req: RentRequest) -> str:
    t = req.tenancy
    note = f"Rent {t.bed} {t.resident.name.split()[0]}"[:50]
    upi_id, upi_name = current.upi()
    return "upi://pay?" + urlencode({"pa": upi_id, "pn": upi_name, "am": f"{req.amount:.2f}", "cu": "INR", "tn": note},
                                    quote_via=quote)


def qr_png(req: RentRequest) -> str:
    import segno
    return segno.make(upi_uri(req), error="m").png_data_uri(scale=8, border=3)


def claim(req: RentRequest, reference: str) -> bool:
    reference = re.sub(r"[^0-9A-Za-z]", "", reference or "")[:40]
    with atomic():
        n = RentRequest.objects.filter(pk=req.pk, status=RentRequest.Status.OPEN).update(
            status=RentRequest.Status.CLAIMED, reference=reference, claimed_at=timezone.now())
        if n:
            req.refresh_from_db()
            AuditLog.record("payment_link", f"{req.tenancy.resident.name} says paid ₹{req.amount:,.0f} by UPI"
                                            f"{f' (ref {reference})' if reference else ''} — to verify")
    return bool(n)


def confirm(req: RentRequest, by: str = "") -> RentPayment:
    """Staff saw the money in the UPI app: record it as rent."""
    if req.status != RentRequest.Status.CLAIMED:
        raise ValidationError("This link isn't waiting to be checked.")
    with atomic():
        p = record(req.tenancy, req.amount, mode=RentPayment.Mode.UPI, paid_on=timezone.localdate(req.claimed_at),
                   reference=req.reference, note="UPI payment link", by=by)
        req.status = RentRequest.Status.PAID
        req.save(update_fields=["status"])
    return p


def not_received(req: RentRequest) -> None:
    if req.status != RentRequest.Status.CLAIMED:
        raise ValidationError("This link isn't waiting to be checked.")
    with atomic():
        req.status = RentRequest.Status.OPEN
        req.save(update_fields=["status"])
        AuditLog.record("payment_link", f"UPI payment from {req.tenancy.resident.name} not found — link open again")


def to_verify():
    return RentRequest.objects.filter(status=RentRequest.Status.CLAIMED).select_related("tenancy__resident",
                                                                                         "tenancy__bed__room")


# ── Money for a month ─────────────────────────────────────────────────────────


@dataclass
class Month:
    start: date
    end: date  # exclusive
    received: Decimal = ZERO
    held: Decimal = ZERO
    pending: Decimal = ZERO
    collected: Decimal = ZERO  # rent payments made in the month (cash in)
    deposits_in: Decimal = ZERO
    refunds_out: Decimal = ZERO
    waived: Decimal = ZERO  # rent let off at move-out this month
    rows: list = field(default_factory=list)  # (tenancy, received, held, pending)

    @property
    def earned(self) -> Decimal:
        return self.received + self.pending


def month(first: date, day: date | None = None) -> Month:
    day = day or today()
    end = first.replace(day=monthrange(first.year, first.month)[1]) + timedelta(days=1)
    m = Month(first, end)
    upto = min(end, day + timedelta(days=1))  # days stayed so far (today counts)
    sync_all(day)
    qs = (Tenancy.objects.exclude(status__in=[T.RESERVED, T.CANCELLED]).filter(move_in__lt=end)
          .select_related("resident", "bed__room"))
    for t in qs:
        r = h = pnd = ZERO
        for ln in lines(t):
            d = ln.due
            stop = stop_of(t)
            real_end = min(d.end, stop) if stop and stop < d.end else d.end  # the days actually charged
            per_day = d.amount / Decimal(max((real_end - d.start).days, 1))
            frac = ln.paid / d.amount if d.amount else Decimal(1)
            off = ln.waived / d.amount if d.amount else ZERO  # waived: neither received nor pending
            past = max(0, (min(real_end, upto) - max(d.start, first)).days)
            future = max(0, (min(real_end, end) - max(d.start, upto, first)).days)
            r += per_day * past * frac
            pnd += per_day * past * max(1 - frac - off, ZERO)
            h += per_day * future * frac
        r, h, pnd = _money(r), _money(h), _money(pnd)
        if r or h or pnd:
            m.rows.append((t, r, h, pnd))
        m.received += r
        m.held += h
        m.pending += pnd
    pays = RentPayment.objects.filter(paid_on__gte=first, paid_on__lt=end)
    m.collected = (pays.filter(kind=RentPayment.Kind.RENT).exclude(mode__in=RentPayment.SETTLEMENT_MODES)
                   .aggregate(s=Sum("amount"))["s"] or ZERO)
    m.waived = (pays.filter(kind=RentPayment.Kind.RENT, mode=RentPayment.Mode.WAIVED)
                .aggregate(s=Sum("amount"))["s"] or ZERO)
    m.deposits_in = pays.filter(kind=RentPayment.Kind.DEPOSIT).aggregate(s=Sum("amount"))["s"] or ZERO
    m.refunds_out = (Tenancy.objects.filter(refund_paid_on__gte=first, refund_paid_on__lt=end)
                     .aggregate(s=Sum("refund"))["s"] or ZERO)
    m.rows.sort(key=lambda x: (x[0].bed.room.number, x[0].bed.label))
    return m


def deposits_held() -> Decimal:
    """Advances we're holding: current residents', and those of residents who moved out but aren't refunded yet."""
    open_ = Tenancy.objects.filter(status__in=[T.RESERVED, T.STAYING, T.NOTICE]).aggregate(s=Sum("deposit_received"))["s"]
    waiting = Tenancy.objects.filter(status=T.MOVED_OUT, refund__gt=0, refund_paid_on__isnull=True).aggregate(
        s=Sum("refund"))["s"]
    return (open_ or ZERO) + (waiting or ZERO)
