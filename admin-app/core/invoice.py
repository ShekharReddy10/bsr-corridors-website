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
LOGO = Path(settings.BASE_DIR) / "static" / "img" / "icon.png"
SALT = "invoice"
PENTHOUSE = "601"
ACCENT = (163, 120, 52)
INK = (33, 37, 41)
MUTED = (110, 117, 125)
LINE = (222, 214, 200)


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


class _PDF(FPDF):
    def __init__(self):
        super().__init__(format="A4")
        self.add_font("Noto", "", str(FONTS / "NotoSans-Regular.ttf"))
        self.add_font("Noto", "B", str(FONTS / "NotoSans-Bold.ttf"))
        self.set_margins(16, 16, 16)
        self.set_auto_page_break(True, 18)

    def text_at(self, x, y, w, h, txt, size=9.5, style="", color=INK, align="L"):
        self.set_xy(x, y)
        self.set_font("Noto", style, size)
        self.set_text_color(*color)
        self.cell(w, h, txt, align=align)

    def rule(self, y, color=LINE, width=0.3):
        self.set_draw_color(*color)
        self.set_line_width(width)
        self.line(self.l_margin, y, self.w - self.r_margin, y)


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

    # Header: hotel on the left, invoice number and date on the right.
    if LOGO.exists():
        p.image(str(LOGO), x=left, y=16, w=17)
    p.text_at(left + 21, 16, 100, 8, settings.HOTEL_NAME.upper(), size=17, style="B", color=ACCENT)
    y = 25
    address = [ln.strip() for ln in info.get("location", "").splitlines() if ln.strip() and "://" not in ln]
    for ln in address + ([info["contact"]] if info.get("contact") else []):
        p.set_xy(left + 21, y)
        p.set_font("Noto", "", 8.5)
        p.set_text_color(*MUTED)
        p.multi_cell(105, 4.2, ln, align="L")
        y = p.get_y()
    p.text_at(right - 70, 16, 70, 9, "INVOICE", size=20, style="B", align="R")
    p.text_at(right - 70, 26, 70, 5, f"No: {inv.number}", size=9.5, style="B", align="R")
    p.text_at(right - 70, 31, 70, 5, f"Date: {inv.date:%d-%m-%Y}", size=9.5, align="R")
    y = max(y, 38) + 4
    p.rule(y, ACCENT, 0.6)

    # Billed to / stay.
    y += 5
    col = left + width / 2 + 4
    p.text_at(left, y, 80, 5, "BILLED TO", size=8, style="B", color=MUTED)
    p.text_at(col, y, 80, 5, "STAY", size=8, style="B", color=MUTED)
    billed = [(guest.name, "B")] + [(v, "") for v in (guest.phone, getattr(guest, "address", "")) if v]
    rooms = ", ".join(dict.fromkeys(f"Room {s.room.number}" for s in inv.lines_stays))
    end = inv.lines_stays[-1].check_out
    nights = sum(s.nights for s in inv.lines_stays)
    source = first.get_source_display() + (f" · Ref {first.source_ref}" if first.source_ref else "")
    stay_rows = [(f"{rooms} · {inv.stay.room.room_type.name}", "B"),
                 (f"Check-in: {first.check_in:%a %d-%m-%Y}", ""),
                 (f"Check-out: {end:%a %d-%m-%Y}" if end else "Check-out: ongoing (monthly)", ""),
                 (f"Guests: {first.num_guests} · Nights: {nights}", ""),
                 (f"Booked via: {source}", "")]
    yy = y + 5.5
    for text, style in billed:
        p.set_xy(left, yy)
        p.set_font("Noto", style, 9.5)
        p.set_text_color(*INK)
        p.multi_cell(width / 2 - 6, 4.8, text, align="L")
        yy = p.get_y()
    y2 = y + 5.5
    for text, style in stay_rows:
        p.text_at(col, y2, width / 2 - 4, 4.8, text, style=style)
        y2 += 4.8
    y = max(yy, y2) + 5

    # Lines.
    cols = [("Description", width * 0.52, "L"), ("Qty", width * 0.14, "R"), ("Rate", width * 0.16, "R"),
            ("Amount", width * 0.18, "R")]
    p.set_fill_color(247, 242, 233)
    p.rect(left, y, width, 7.5, style="F")
    x = left
    for title, w, align in cols:
        p.text_at(x + 2 if align == "L" else x, y + 1.2, w - 2, 5, title, size=8.5, style="B", color=MUTED, align=align)
        x += w
    y += 9
    for line in inv.lines:
        x = left
        p.text_at(x + 2, y, cols[0][1] - 2, 5, line.description, style="B")
        p.text_at(x + 2, y + 5, cols[0][1] - 2, 4.5, line.detail, size=8.5, color=MUTED)
        x += cols[0][1]
        for txt, (_, w, align) in zip([line.qty, line.rate, money(line.amount)], cols[1:]):
            p.text_at(x, y, w - 2, 5, txt, align=align)
            x += w
        y += 12
        p.rule(y - 2)

    # Totals.
    y += 2
    tx, tw = left + width * 0.55, width * 0.45 - 2
    for label, value, style, color in [("Total", money(inv.total), "B", INK),
                                       ("Paid", money(inv.paid), "", INK),
                                       ("Balance due", money(max(inv.balance, 0)), "B",
                                        (176, 42, 42) if inv.balance > 0 else INK)]:
        p.text_at(tx, y, tw / 2, 6, label, size=10, style=style)
        p.text_at(tx + tw / 2, y, tw / 2, 6, value, size=10, style=style, color=color, align="R")
        y += 6.5
    if inv.balance <= 0 and inv.total > 0:
        p.text_at(tx, y + 1, tw, 6, "PAID IN FULL", size=10.5, style="B", color=(30, 120, 70), align="R")
        y += 8
    if inv.deposit:
        p.text_at(left, y + 1, width, 5, f"Refundable security deposit held: {money(inv.deposit)} "
                                         f"(not included in the total).", size=8.5, color=MUTED)
        y += 7

    # Payments.
    if inv.payments:
        y += 4
        p.rule(y)
        y += 3
        p.text_at(left, y, 80, 5, "PAYMENTS RECEIVED", size=8, style="B", color=MUTED)
        y += 6
        for pay in inv.payments:
            if y > p.h - 40:
                p.add_page()
                y = p.t_margin
            p.text_at(left, y, 35, 5, f"{pay.date:%d-%m-%Y}")
            p.text_at(left + 35, y, 40, 5, pay.get_mode_display() if pay.mode else "")
            p.text_at(left + 75, y, 30, 5, money(pay.amount), align="R")
            y += 5.5

    # Footer.
    y = max(y + 8, p.h - 42)
    if y > p.h - 30:
        p.add_page()
        y = p.t_margin
    p.rule(y, ACCENT, 0.6)
    p.set_xy(left, y + 4)
    p.set_font("Noto", "", 9.5)
    p.set_text_color(*INK)
    p.multi_cell(width, 5, "Thank you for staying with us. We look forward to welcoming you again.", align="C")
    p.set_x(left)
    p.set_font("Noto", "", 8)
    p.set_text_color(*MUTED)
    p.multi_cell(width, 4.5, "This is a computer-generated invoice and does not require a signature.", align="C")
    return bytes(p.output())
