"""WhatsApp messages to residents: fill a PgMessage's {placeholders} from the tenancy and the property.

A line whose placeholder has nothing to show is left out (e.g. "Balance still due" when nothing is due), and so is
the paragraph with {payment_link} when no link can be made (no UPI ID, or nothing to pay).
"""

import re
from decimal import Decimal

from cluster import current
from core.invoice import money as inr
from core.services import today
from core.shortlinks import shorten
from core.whatsapp import with_footer

from . import money, services
from .models import PgMessage, RentPayment, Tenancy

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

FIELDS = {
    "name": "Resident's full name", "first_name": "First name", "property": "Property name", "room": "Room number",
    "bed": "Bed (e.g. 201-A)", "rent": "Rent per cycle", "cycle": "monthly / weekly / daily",
    "due_rule": "When rent is due (e.g. on the 5th of every month)", "move_in": "Move-in date",
    "deposit": "Advance received", "amount_due": "Rent owed now", "due_date": "Next due date",
    "due_since": "Oldest unpaid due date", "amount": "Payment link amount", "payment_link": "UPI payment link",
    "amount_paid": "Payment received", "paid_on": "Its date", "receipt_link": "Its receipt",
    "last_day": "Last day (notice)", "settlement": "Move-out settlement lines", "contact": "Property phone numbers",
    "address": "Property address", "map_link": "Map link",
}


def _date(d) -> str:
    return f"{d:%d-%m-%Y}" if d else ""


def due_rule_text(t: Tenancy) -> str:
    if t.cycle == Tenancy.Cycle.MONTHLY:
        day = t.due_day if t.due_rule == Tenancy.DueRule.FIXED and t.due_day else t.move_in.day
        suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"on the {day}{suffix} of every month"
    if t.cycle == Tenancy.Cycle.WEEKLY:
        return f"every {t.move_in:%A}"
    return "every day"


def settlement_text(t: Tenancy) -> str:
    rows = [f"Advance received: {inr(t.deposit_received)}"]
    if t.unpaid_rent > 0:
        rows.append(f"Rent due: {inr(t.unpaid_rent)}")
    elif t.unpaid_rent < 0:
        rows.append(f"Rent paid ahead: {inr(-t.unpaid_rent)}")
    rows += [f"{d['reason']}: {inr(d['amount'])}" for d in t.deductions]
    if t.refund > 0:
        rows.append(f"Refund to you: {inr(t.refund)}" + (f", by {_date(t.refund_due_by)}" if t.refund_due_by else ""))
    elif t.refund < 0:
        rows.append(f"Still to be paid by you: {inr(-t.refund)}")
    else:
        rows.append("Nothing more to pay or refund.")
    return "\n".join(rows)


def context(t: Tenancy, base_url: str = "", payment: RentPayment | None = None, amount: Decimal | None = None) -> dict:
    item = current.get()
    owed = money.balance(t)
    nxt = services.next_due(t)
    ctx = {
        "name": t.resident.name, "first_name": t.resident.name.split()[0], "property": current.name(),
        "room": t.bed.room.number, "bed": str(t.bed), "rent": inr(t.rent), "cycle": t.get_cycle_display().lower(),
        "due_rule": due_rule_text(t), "move_in": _date(t.move_in), "deposit": inr(t.deposit_received),
        "amount_due": inr(owed) if owed > 0 else "", "due_date": _date(nxt.start) if nxt else "",
        "due_since": _date(money.unpaid_since(t)), "last_day": _date(t.last_day),
        "contact": getattr(item, "phones", "") or "", "address": getattr(item, "address", "") or "",
        "map_link": getattr(item, "map_link", "") or "", "settlement": settlement_text(t) if t.move_out else "",
        "amount": inr(amount) if amount else "", "payment_link": "", "amount_paid": "", "paid_on": "",
        "receipt_link": "",
    }
    if payment is not None:
        ctx.update({"amount_paid": inr(payment.amount), "paid_on": _date(payment.paid_on)})
        if base_url:
            ctx["receipt_link"] = shorten(f"{base_url.rstrip('/')}/pay/receipt/{money.receipt_token(payment)}/")
    return ctx


def render(body: str, ctx: dict) -> str:
    if not ctx.get("payment_link"):
        body = "\n\n".join(p for p in re.split(r"\n\s*\n", body) if "{payment_link}" not in p)
    kept = []
    for line in body.split("\n"):
        names = PLACEHOLDER.findall(line)
        if names and any(n in ctx and ctx[n] == "" for n in names):
            continue  # nothing to show on this line
        kept.append(line)
    text = PLACEHOLDER.sub(lambda m: ctx.get(m.group(1), m.group(0)), "\n".join(kept))
    return with_footer(re.sub(r"\n{3,}", "\n\n", text).strip())


def build(t: Tenancy, msg: PgMessage, base_url: str, *, by: str = "", payment=None, amount=None) -> str:
    """The message text. With {payment_link} in it, a UPI link is made for `amount` (default: rent owed now)."""
    ctx = context(t, base_url, payment, amount)
    if "{payment_link}" in msg.body and money.upi_ready():
        pay = amount if amount is not None else money.balance(t)
        if pay and pay > 0:
            req = money.create_request(t, pay, by)
            ctx["amount"] = inr(pay)
            ctx["payment_link"] = shorten(f"{base_url.rstrip('/')}/pay/rent/{money.pay_token(req)}/")
    return render(msg.body, ctx)


def for_use(use: str) -> PgMessage | None:
    return PgMessage.objects.filter(use=use).first()


def today_str() -> str:
    return _date(today())
