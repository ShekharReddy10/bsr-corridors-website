"""Guest invoices as PDF (no GST — the hotel isn't GST-registered).

Automatic (made at check-out / with the departure message): Walk-in and Direct bookings (the whole stay, covering its
room moves too), and for platform bookings (Airbnb, Booking.com, …) the nights the guest extended directly with us and
the payments made to us for them.

By hand: any other platform booking gets an invoice for the whole stay only when staff press Generate invoice (marked
"manual"); from then on it is kept up to date at check-out and sent with the check-out message like any other.

An invoice exists once it is generated. Until then no link is sent and the guest's link doesn't open.

The number is built from the invoiced nights: first night (YYMMDD) - check-out (YYMMDD) - room number (3 digits; the
penthouse is 601), e.g. 261115-261118-101. It follows the stay's dates and room, so it is never stored.

Guests open their invoice through a signed link (no login), which only ever shows that one invoice.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core import signing
from fpdf import FPDF

from cluster.current import name as hotel_name

from .models import Stay
from .services import today

FONTS = Path(__file__).resolve().parent / "fonts"
LOGO = Path(__file__).resolve().parent / "invoice_logo.png"  # black and white
SALT = "invoice"
PENTHOUSE = "601"
BLACK = (0, 0, 0)
GREY = (55, 55, 55)
SHADE = (235, 235, 235)


def room_code(room) -> str:
    """3-digit room number for the invoice number; the penthouse is 601."""
    if "pent" in f"{room.number} {room.room_type.name}".lower():
        return PENTHOUSE
    digits = "".join(c for c in room.number if c.isdigit())
    return digits.zfill(3) if digits else "000"


def _live_chain(stay: Stay) -> list[Stay]:
    chain = [s for s in stay.chain() if not s.is_void]
    return chain or [stay]


@dataclass
class Part:
    """The invoiced part of one stay: all of it (Walk-in / Direct), or the nights extended directly with us."""
    stay: Stay
    start: date
    end: date | None
    amount: Decimal
    paid: Decimal
    payments: list
    whole: bool

    @property
    def nights(self) -> int:
        return ((self.end or max(today(), self.start)) - self.start).days


def _auto_parts(stay: Stay) -> list[Part]:
    out = []
    for s in _live_chain(stay):
        if s.is_void:
            continue
        if s.is_direct_booking:
            pays = list(s.payments.order_by("date", "pk"))
            out.append(Part(s, s.check_in, s.check_out, s.total, s.amount_paid or Decimal(0), pays, True))
        elif s.direct_from and (s.check_out is None or s.check_out > s.direct_from):
            pays = s.payments.filter(created_at__gte=s.direct_since) if s.direct_since else \
                s.payments.filter(date__gte=s.direct_from)
            pays = list(pays.order_by("date", "pk"))
            out.append(Part(s, max(s.direct_from, s.check_in), s.check_out, s.direct_amount or Decimal(0),
                            sum((x.amount for x in pays), Decimal(0)), pays, False))
    return out


def _whole_parts(stay: Stay) -> list[Part]:
    """A platform booking invoiced by hand: the whole stay, every payment."""
    return [Part(s, s.check_in, s.check_out, s.total, s.amount_paid or Decimal(0),
                 list(s.payments.order_by("date", "pk")), True) for s in _live_chain(stay) if not s.is_void]


def is_manual(stay: Stay) -> bool:
    from .models import AppSetting
    return AppSetting.get(f"{_drive_key(stay)}_manual") == "1"


def parts(stay: Stay, manual: bool | None = None) -> list[Part]:
    """What the invoice covers: the automatic part, else — for a platform booking invoiced by hand — the whole stay."""
    return _auto_parts(stay) or (_whole_parts(stay) if (is_manual(stay) if manual is None else manual) else [])


def eligible(stay: Stay) -> bool:
    """Invoiced automatically: Walk-in / Direct, or a platform booking extended directly with us."""
    return bool(_auto_parts(stay))


def number(stay: Stay, ps: list[Part] | None = None) -> str:
    ps = parts(stay) if ps is None else ps
    if not ps:
        return ""
    end = ps[-1].end or today()
    return f"{ps[0].start:%y%m%d}-{end:%y%m%d}-{room_code(ps[-1].stay.room)}"


def token(stay: Stay) -> str:
    return signing.Signer(salt=SALT).sign(str(stay.root.pk))


def stay_for_token(value: str) -> Stay | None:
    try:
        pk = int(signing.Signer(salt=SALT).unsign(value))
    except (signing.BadSignature, ValueError):
        return None
    return Stay.objects.filter(pk=pk).select_related("guest", "room", "room__room_type").first()


def money(v) -> str:
    """Indian digit grouping: 123456 → ₹1,23,456."""
    n = int(Decimal(v or 0).quantize(Decimal("1")))
    sign, s = ("-" if n < 0 else ""), str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(([head] if head else []) + groups) + "," + tail
    return f"{sign}₹{s}"


@dataclass
class Line:
    description: str
    detail: str
    qty: str
    rate: str
    amount: Decimal


@dataclass
class Invoice:
    number: str
    date: date
    stay: Stay
    lines: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    parts: list = field(default_factory=list)  # the invoiced stays (room moves) or their direct nights, in date order
    total: Decimal = Decimal(0)
    paid: Decimal = Decimal(0)
    deposit: Decimal = Decimal(0)

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid


def build(stay: Stay) -> Invoice:
    ps = parts(stay)
    if not ps:
        raise ValueError("This stay has no invoice (booked through a platform and not extended with us).")
    last = ps[-1].stay
    inv = Invoice(number=number(stay, ps), stay=last, parts=ps,
                  date=(last.check_out if last.status == Stay.Status.CHECKED_OUT and last.check_out else today()))
    for part in ps:
        s = part.stay
        room = f"Room {s.room.number} – {s.room.room_type.name}"
        end = f"{part.end:%d-%m-%Y}" if part.end else "ongoing"
        detail = f"{part.start:%d-%m-%Y} to {end}"
        if not part.whole:
            n = part.nights
            inv.lines.append(Line(room, detail, f"{n} night{'s' if n != 1 else ''}",
                                  money(part.amount / n) if n else "", part.amount))
        elif s.kind == Stay.Kind.MONTHLY:
            rent = s.monthly_rent or Decimal(0)
            months = s.total / rent if rent else None
            whole = months is not None and months == months.to_integral_value() and months > 0
            inv.lines.append(Line(f"{room} (monthly)", detail, f"{int(months)} month{'s' if months != 1 else ''}" if whole else "",
                                  money(rent) if whole else "", s.total))
        else:
            n = s.nights
            inv.lines.append(Line(room, detail, f"{n} night{'s' if n != 1 else ''}", money(s.total / n) if n else "",
                                  s.total))
        inv.total += part.amount
        inv.paid += part.paid
        if part.whole:
            inv.deposit += s.deposit_amount or Decimal(0)
        inv.payments += part.payments
    inv.payments.sort(key=lambda x: (x.date, x.pk))
    return inv


_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _words_below_100(n: int) -> str:
    return _ONES[n] if n < 20 else f"{_TENS[n // 10]} {_ONES[n % 10]}".strip()


def in_words(amount) -> str:
    """4500 → 'Rupees Four Thousand Five Hundred Only' (Indian system: lakh, crore)."""
    n = int(Decimal(amount or 0).quantize(Decimal("1")))
    if n <= 0:
        return "Rupees Zero Only"
    parts = []
    for size, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand"), (100, "Hundred")):
        if n >= size:
            count = n // size
            parts.append(f"{in_words(count)[7:-5] if count >= 100 else _words_below_100(count)} {name}")
            n %= size
    if n:
        parts.append(("and " if parts else "") + _words_below_100(n))
    return f"Rupees {' '.join(parts)} Only"


class _PDF(FPDF):
    """Typewriter-style invoice: FreeMono (Courier-like), black on white, ruled boxes."""

    def __init__(self):
        super().__init__(format="A4")
        self.add_font("Mono", "", str(FONTS / "FreeMono.ttf"))
        self.add_font("Mono", "B", str(FONTS / "FreeMonoBold.ttf"))
        self.set_margins(14, 12, 14)
        self.set_auto_page_break(True, 16)
        self.set_text_color(*BLACK)
        self.set_draw_color(*BLACK)

    def put(self, x, y, w, h, txt, size=9.5, style="", align="L", color=BLACK):
        self.set_xy(x, y)
        self.set_font("Mono", style, size)
        while size > 6 and self.get_string_width(txt) > w:  # long names shrink to fit their box
            size -= 0.5
            self.set_font("Mono", style, size)
        self.set_text_color(*color)
        self.cell(w, h, txt, align=align)

    def hline(self, y, width=0.2, x1=None, x2=None):
        self.set_line_width(width)
        self.line(x1 if x1 is not None else self.l_margin, y, x2 if x2 is not None else self.w - self.r_margin, y)


def pdf(stay: Stay) -> bytes:
    from .whatsapp import hotel_info

    inv = build(stay)
    info = hotel_info()
    guest = inv.stay.guest
    first = inv.parts[0].stay
    p = _PDF()
    p.add_page()
    left, right = p.l_margin, p.w - p.r_margin
    width = right - left

    # Letterhead, centred: logo, name, address, phone.
    y = 12
    if LOGO.exists():
        p.image(str(LOGO), x=(p.w - 18) / 2, y=y, h=20)
        y += 22
    p.put(left, y, width, 8, hotel_name().upper(), size=18, style="B", align="C")
    y += 8
    address = [ln.strip() for ln in info.get("location", "").splitlines() if ln.strip() and "://" not in ln]
    for ln in address:
        p.set_xy(left + 15, y)
        p.set_font("Mono", "", 9)
        p.multi_cell(width - 30, 4.2, ln, align="C")
        y = p.get_y()
    if info.get("contact"):
        p.put(left, y, width, 4.5, f"Phone: {info['contact']}", size=9, align="C")
        y += 4.5
    y += 2.5
    p.hline(y, 0.6)
    p.hline(y + 0.9, 0.2)

    # Title and invoice number / date.
    y += 3
    p.put(left, y, width, 7, "I N V O I C E", size=14, style="B", align="C")
    p.set_line_width(0.3)
    p.rect(right - 46, y + 0.5, 46, 6)
    p.put(right - 46, y + 1, 46, 5, "ORIGINAL FOR RECIPIENT", size=8, style="B", align="C")
    y += 9
    p.put(left, y, width / 2, 5, f"Invoice No : {inv.number}", style="B")
    p.put(left + width / 2, y, width / 2, 5, f"Date : {inv.date:%d-%m-%Y}", style="B", align="R")
    y += 7

    # Two boxes: billed to, stay details.
    rooms = ", ".join(dict.fromkeys(part.stay.room.number for part in inv.parts))
    start, end = inv.parts[0].start, inv.parts[-1].end
    nights = sum(part.nights for part in inv.parts)
    if all(part.whole for part in inv.parts):
        source = first.get_source_display() + (f" (Ref {first.source_ref})" if first.source_ref else "")
    else:  # a platform booking extended with us: only our nights are on it
        source = "Direct"
    billed = [("Name", guest.name), ("Phone", guest.phone), ("Address", guest.address)]
    stay_rows = [("Room", f"{rooms} - {inv.stay.room.room_type.name}"),
                 ("Check-in", f"{start:%d-%m-%Y} ({start:%a})"),
                 ("Check-out", f"{end:%d-%m-%Y} ({end:%a})" if end else "Ongoing (monthly)"),
                 ("Guests", f"{first.num_guests}    Nights: {nights}"),
                 ("Booked via", source)]
    half = width / 2

    def box(x, title, rows, label_w):
        yy = y + 1.5
        p.put(x + 2, yy, half - 4, 5, title, size=9, style="B")
        yy += 6
        for label, value in rows:
            if not value:
                continue
            p.put(x + 2, yy, label_w, 4.6, f"{label}", size=9)
            p.put(x + 2 + label_w, yy, 3, 4.6, ":", size=9)
            p.set_xy(x + 5 + label_w, yy)
            p.set_font("Mono", "", 9)
            p.multi_cell(half - label_w - 7, 4.6, str(value), align="L")
            yy = p.get_y()
        return yy

    bottom = max(box(left, "BILLED TO", billed, 17), box(left + half, "STAY DETAILS", stay_rows, 21)) + 2
    p.set_line_width(0.3)
    p.rect(left, y, half, bottom - y)
    p.rect(left + half, y, half, bottom - y)
    y = bottom + 5

    # Items table, ruled.
    cols = [("S.No", 13, "C"), ("Description", width - 13 - 28 - 30 - 33, "L"), ("Qty", 28, "C"), ("Rate", 30, "R"),
            ("Amount", 33, "R")]
    xs = [left]
    for _, w, _ in cols:
        xs.append(xs[-1] + w)
    p.set_fill_color(*SHADE)
    p.set_line_width(0.3)
    p.rect(left, y, width, 7, style="DF")
    for (title, w, align), x in zip(cols, xs):
        p.put(x + 1.5, y + 1, w - 3, 5, title, size=9.5, style="B", align=align)
    top, y = y, y + 7
    for n, line in enumerate(inv.lines, 1):
        p.put(xs[0] + 1.5, y + 1.5, cols[0][1] - 3, 5, str(n), align="C")
        p.put(xs[1] + 1.5, y + 1.5, cols[1][1] - 3, 5, line.description, style="B")
        p.put(xs[1] + 1.5, y + 6, cols[1][1] - 3, 4.5, line.detail, size=8.5)
        p.put(xs[2] + 1.5, y + 1.5, cols[2][1] - 3, 5, line.qty, align="C")
        p.put(xs[3] + 1.5, y + 1.5, cols[3][1] - 3, 5, line.rate, align="R")
        p.put(xs[4] + 1.5, y + 1.5, cols[4][1] - 3, 5, money(line.amount), align="R")
        y += 12
    y = max(y, top + 7 + 30)  # a few empty ruled lines' worth of space, like a printed form
    p.rect(left, top, width, y - top)
    for x in xs[1:-1]:
        p.line(x, top, x, y)

    # Totals, boxed on the right under Rate/Amount.
    tx = xs[3]
    rows = [("Total", money(inv.total), "B"), ("Paid", money(inv.paid), ""),
            ("Balance Due", money(max(inv.balance, 0)), "B")]
    for label, value, style in rows:
        p.rect(tx, y, right - tx, 7)
        p.put(tx + 1.5, y + 1, xs[4] - tx - 3, 5, label, style=style)
        p.put(xs[4] + 1.5, y + 1, cols[4][1] - 3, 5, value, style=style, align="R")
        y += 7
    p.line(xs[4], y - 21, xs[4], y)
    # Amount in words, beside the totals.
    p.set_xy(left, y - 19)
    p.set_font("Mono", "", 9)
    p.multi_cell(tx - left - 4, 4.6, f"Amount in words:\n{in_words(inv.total)}", align="L")
    if inv.balance <= 0 < inv.total:
        p.put(left, y - 6, tx - left - 4, 5, "*** PAID IN FULL ***", size=10, style="B")
    y += 4
    if inv.deposit:
        p.put(left, y, width, 5, f"Note: Refundable security deposit of {money(inv.deposit)} held "
                                 f"(not included in the total).", size=8.5)
        y += 6

    # Payments received.
    if inv.payments:
        y += 2
        p.put(left, y, width, 5, "PAYMENTS RECEIVED", size=9, style="B")
        y += 6
        pw = [30, 45, 35]
        p.set_fill_color(*SHADE)
        p.rect(left, y, sum(pw), 6, style="DF")
        for title, w, x, align in zip(("Date", "Mode", "Amount"), pw, (left, left + 30, left + 75), "LLR"):
            p.put(x + 1.5, y + 0.6, w - 3, 5, title, size=9, style="B", align=align)
        y0, y = y, y + 6
        for pay in inv.payments:
            if y > p.h - 45:
                p.add_page()
                y0 = y = p.t_margin
            p.put(left + 1.5, y + 0.6, 27, 5, f"{pay.date:%d-%m-%Y}", size=9)
            p.put(left + 31.5, y + 0.6, 42, 5, pay.get_mode_display() if pay.mode else "-", size=9)
            p.put(left + 76.5, y + 0.6, 32, 5, money(pay.amount), size=9, align="R")
            y += 6
        p.rect(left, y0, sum(pw), y - y0)
        p.line(left + 30, y0, left + 30, y)
        p.line(left + 75, y0, left + 75, y)

    # Footer: thanks and signatory.
    if y + 10 > p.h - 52:
        p.add_page()
    y = p.h - 52
    p.set_auto_page_break(False)
    p.put(right - 80, y, 80, 5, f"For {hotel_name()}", style="B", align="R")
    p.put(right - 80, y + 6, 80, 4.5, "Electronically generated", size=8.5, align="R")
    p.put(right - 80, y + 10.5, 80, 4.5, "(no signature required)", size=8.5, align="R")
    p.put(right - 80, y + 16, 80, 5, "Authorised Signatory", size=9, style="B", align="R")
    y += 24
    p.hline(y, 0.2)
    p.hline(y + 0.9, 0.6)
    p.put(left, y + 3, width, 5, "Thank you for staying with us. We look forward to welcoming you again.", size=9,
          align="C")
    p.put(left, y + 8, width, 4.5, "This is an electronically generated invoice and does not require a physical "
                                   "signature or stamp.", size=8, align="C", color=GREY)
    p.put(left, y + 12, width, 4.5, "It is valid and shall be treated as the original invoice.", size=8, align="C",
          color=GREY)
    return bytes(p.output())


# ── Copy in Google Drive ("Invoices" folder) ──


def _drive_key(stay: Stay) -> str:
    return f"invoice_{stay.root.pk}"


def drive_link(stay: Stay) -> str:
    from .models import AppSetting
    return AppSetting.get(f"{_drive_key(stay)}_link")


def generated_at(stay: Stay) -> str:
    """When the invoice was last generated ("" if never)."""
    from .models import AppSetting
    return AppSetting.get(f"{_drive_key(stay)}_at")


def has_invoice(stay: Stay) -> bool:
    """An invoice exists: generated (automatically or by hand) and the stay still has something to invoice."""
    return bool(generated_at(stay)) and bool(parts(stay))


def generate(stay: Stay, only_if_missing: bool = False, manual: bool = False) -> str:
    """Generate (or update) this stay's invoice, saving it in Drive in the same file.
    Returns "saved", "no-drive" (Drive not connected, so it's not saved there), "failed" (Drive error), or "exists"
    (only_if_missing, and it was already generated).

    One at a time per stay: Check out opens the departure message in the same tap, so the check-out and the message
    can both get here at once — the second waits for the first, so there's never a second file."""
    from .db import atomic
    from django.utils import timezone
    from . import gdrive
    from .models import AppSetting
    with atomic():
        Stay.objects.select_for_update().filter(pk=stay.root.pk).first()
        if only_if_missing and generated_at(stay):
            return "exists"
        if manual and not eligible(stay):  # a platform booking, by hand: the whole stay from now on
            AppSetting.put(f"{_drive_key(stay)}_manual", "1")
        data = pdf(stay)  # raises ValueError if the stay has no invoice
        AppSetting.put(f"{_drive_key(stay)}_at", f"{timezone.localtime():%d-%m-%Y %I:%M %p}")
        if not gdrive.is_configured():
            return "no-drive"
        no = number(stay)
        try:
            gdrive.save_invoice(data, f"{no} {stay.guest.name}.pdf".replace("/", " "), _drive_key(stay), no)
        except Exception:
            logging.getLogger(__name__).exception("Invoice to Drive failed")
            return "failed"
        return "saved"


def pending(stay: Stay) -> bool:
    """The stay has an invoice to make, not generated yet."""
    return not stay.is_void and not generated_at(stay) and eligible(stay)


def ensure(stay: Stay) -> bool:
    """Before sending a message with the invoice link (departure / check-out details): generate the invoice if the
    stay has one and it isn't generated yet. Returns True if one was generated now."""
    from .models import AuditLog
    if stay.is_void or not eligible(stay):
        return False
    if generate(stay, only_if_missing=True) == "exists":
        return False
    AuditLog.record("invoice", f"Invoice {number(stay)} generated (sent with the WhatsApp message)", stay)
    return True


def forget(stay: Stay) -> None:
    """The stay no longer has an invoice (cancelled, transferred, or no longer qualifies): remove it and its file."""
    from . import gdrive
    from .models import AppSetting
    key = _drive_key(stay)
    if gdrive.is_configured():
        try:
            gdrive.delete_invoice(key)
        except Exception:
            logging.getLogger(__name__).exception("Removing invoice from Drive failed")
            return
    AppSetting.objects.filter(key__startswith=f"{key}_").delete()


def remove_from_drive(stay: Stay) -> None:
    """After a cancel / transfer: if nothing invoiceable is left, the invoice goes."""
    if not parts(stay):
        forget(stay)


def on_check_out(request, stay: Stay) -> None:
    """Check-out generates the invoice (or updates it) once the guest has left — if the stay has one. Never blocks
    the check-out itself."""
    from django.contrib import messages
    if not (eligible(stay) or has_invoice(stay)) or _live_chain(stay)[-1].status != Stay.Status.CHECKED_OUT:
        return  # platform bookings: only if an invoice was made by hand
    again = bool(generated_at(stay))
    result = generate(stay)  # the dates may have changed (early check-out): always brought up to date
    from .models import AuditLog
    AuditLog.record("invoice", f"Invoice {number(stay)} {'updated' if again else 'generated'} at check-out", stay)
    if result == "failed":
        messages.warning(request, "The invoice was generated but couldn’t be saved to Google Drive just now — "
                                  "use Update invoice on the stay page to try again.")


def tidy() -> int:
    """Nightly: remove invoices of stays that no longer have anything to invoice (cancelled, transferred, deleted,
    or changed to a platform booking that wasn't invoiced by hand). Returns how many were removed."""
    import re
    from .models import AppSetting
    pks = {int(m.group(1)) for k in AppSetting.objects.filter(key__startswith="invoice_").values_list("key", flat=True)
           if (m := re.fullmatch(r"invoice_(\d+)_(?:id|link|at|manual)", k))}
    n = 0
    for pk in pks:
        stay = Stay.objects.filter(pk=pk).select_related("guest", "room", "room__room_type").first()
        if stay is None:
            from . import gdrive
            if gdrive.is_configured():
                gdrive.delete_invoice(f"invoice_{pk}")
            AppSetting.objects.filter(key__startswith=f"invoice_{pk}_").delete()
            n += 1
        elif not parts(stay):
            forget(stay)
            n += 1
    return n
