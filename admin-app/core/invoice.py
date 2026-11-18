"""Guest invoices as PDF (no GST — the hotel isn't GST-registered).

One invoice per stay, covering its room moves too (linked stays). The number is built from the stay:
check-in date (YYMMDD) - check-out date (YYMMDD) - room number (3 digits; the penthouse is 601), e.g.
261115-261118-101. It follows the stay's dates and room, so it is never stored.

Guests open their invoice through a signed link (no login), which only ever shows that one invoice.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core import signing
from fpdf import FPDF

from .models import Payment, Stay
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


def number(stay: Stay) -> str:
    chain = _live_chain(stay)
    end = chain[-1].check_out or today()
    return f"{chain[0].check_in:%y%m%d}-{end:%y%m%d}-{room_code(chain[-1].room)}"


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
    lines_stays: list = field(default_factory=list)  # the stays covered (room moves), in date order
    total: Decimal = Decimal(0)
    paid: Decimal = Decimal(0)
    deposit: Decimal = Decimal(0)

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid


def build(stay: Stay) -> Invoice:
    chain = _live_chain(stay)
    last = chain[-1]
    inv = Invoice(number=number(stay), stay=last, lines_stays=chain,
                  date=(last.check_out if last.status == Stay.Status.CHECKED_OUT and last.check_out else today()))
    for s in chain:
        room = f"Room {s.room.number} – {s.room.room_type.name}"
        end = f"{s.check_out:%d-%m-%Y}" if s.check_out else "ongoing"
        detail = f"{s.check_in:%d-%m-%Y} to {end}"
        if s.kind == Stay.Kind.MONTHLY:
            rent = s.monthly_rent or Decimal(0)
            months = s.total / rent if rent else None
            whole = months is not None and months == months.to_integral_value() and months > 0
            inv.lines.append(Line(f"{room} (monthly)", detail, f"{int(months)} month{'s' if months != 1 else ''}" if whole else "",
                                  money(rent) if whole else "", s.total))
        else:
            n = s.nights
            inv.lines.append(Line(room, detail, f"{n} night{'s' if n != 1 else ''}", money(s.total / n) if n else "",
                                  s.total))
        inv.total += s.total
        inv.paid += s.amount_paid or Decimal(0)
        inv.deposit += s.deposit_amount or Decimal(0)
    inv.payments = list(Payment.objects.filter(stay__in=chain).order_by("date", "pk"))
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
    first = inv.lines_stays[0]
    p = _PDF()
    p.add_page()
    left, right = p.l_margin, p.w - p.r_margin
    width = right - left

    # Letterhead, centred: logo, name, address, phone.
    y = 12
    if LOGO.exists():
        p.image(str(LOGO), x=(p.w - 18) / 2, y=y, h=20)
        y += 22
    p.put(left, y, width, 8, settings.HOTEL_NAME.upper(), size=18, style="B", align="C")
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
    rooms = ", ".join(dict.fromkeys(s.room.number for s in inv.lines_stays))
    end = inv.lines_stays[-1].check_out
    nights = sum(s.nights for s in inv.lines_stays)
    source = first.get_source_display() + (f" (Ref {first.source_ref})" if first.source_ref else "")
    billed = [("Name", guest.name), ("Phone", guest.phone), ("Address", guest.address)]
    stay_rows = [("Room", f"{rooms} - {inv.stay.room.room_type.name}"),
                 ("Check-in", f"{first.check_in:%d-%m-%Y} ({first.check_in:%a})"),
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
    p.put(right - 80, y, 80, 5, f"For {settings.HOTEL_NAME}", style="B", align="R")
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


def save_to_drive(stay: Stay) -> str:
    """Save this stay's invoice in Drive (replacing the earlier copy); returns the Drive link."""
    from . import gdrive
    name = f"{number(stay)} {stay.guest.name}.pdf".replace("/", " ")
    return gdrive.save_invoice(pdf(stay), name, _drive_key(stay))


def sync_to_drive(request, stay: Stay) -> None:
    """After check-out, or a change to a checked-out stay: keep its invoice in Drive up to date. Never blocks the
    action itself — if Drive fails, the nightly run saves it."""
    import logging
    from django.contrib import messages
    from . import gdrive
    chain = _live_chain(stay)
    if chain[-1].status != Stay.Status.CHECKED_OUT or not gdrive.is_configured():
        return
    try:
        save_to_drive(stay)
    except Exception:
        logging.getLogger(__name__).exception("Invoice to Drive failed")
        messages.warning(request, "The invoice couldn’t be saved to Google Drive just now — it will be saved tonight.")


def save_missing_to_drive(days: int = 60) -> int:
    """Nightly: checked-out stays of the last `days` days whose invoice isn't in Drive yet."""
    from datetime import timedelta
    from .models import AppSetting
    saved = set(AppSetting.objects.filter(key__startswith="invoice_", key__endswith="_id").values_list("key", flat=True))
    n = 0
    for s in (Stay.objects.filter(status=Stay.Status.CHECKED_OUT, check_out__gte=today() - timedelta(days=days))
              .select_related("guest", "room", "room__room_type", "linked_to")):
        if s.continuations.filter(status__in=[Stay.Status.CHECKED_IN, Stay.Status.UPCOMING]).exists():
            continue  # moved rooms and still staying
        if f"{_drive_key(s)}_id" not in saved:
            save_to_drive(s)
            saved.add(f"{_drive_key(s)}_id")
            n += 1
    return n
