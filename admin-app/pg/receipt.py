"""Rent receipt (PDF) for a payment — residents use it e.g. for HRA claims. Same typewriter style as the invoices."""

from cluster import current
from cluster import branding
from core.invoice import _PDF, in_words, letterhead_logo, money as inr

from . import money
from .models import RentPayment


def pdf(p: RentPayment) -> bytes:
    t = p.tenancy
    item = current.get()
    doc = _PDF()
    doc.add_page()
    left, right = doc.l_margin, doc.w - doc.r_margin
    width = right - left
    y = letterhead_logo(doc, item, 12, h=16)
    doc.put(left, y, width, 8, current.name().upper(), size=18, style="B", align="C")
    y += 9
    for ln in branding.business_lines(item):
        doc.put(left, y, width, 4.5, ln, size=9, align="C")
        y += 4.5
    for ln in [x.strip() for x in (getattr(item, "address", "") or "").splitlines() if x.strip()]:
        doc.put(left, y, width, 4.5, ln, size=9, align="C")
        y += 4.5
    if getattr(item, "phones", ""):
        doc.put(left, y, width, 4.5, f"Phone: {item.phones}", size=9, align="C")
        y += 4.5
    y += 2.5
    doc.hline(y, 0.6)
    doc.hline(y + 0.9, 0.2)
    y += 4
    title = "R E N T   R E C E I P T" if p.kind == RentPayment.Kind.RENT else "A D V A N C E   R E C E I P T"
    doc.put(left, y, width, 7, title, size=14, style="B", align="C")
    y += 10
    doc.put(left, y, width / 2, 5, f"Receipt No : {money.receipt_no(p)}", style="B")
    doc.put(left + width / 2, y, width / 2, 5, f"Date : {p.paid_on:%d-%m-%Y}", style="B", align="R")
    y += 9
    rows = [
        ("Received from", t.resident.name),
        ("Phone", t.resident.phone),
        ("Permanent address", " ".join(t.resident.address.split())),
        ("Accommodation", f"Bed {t.bed.label}, Room {t.bed.room.number}, {current.name()}"),
        ("Address", " ".join((getattr(item, "address", "") or "").split())),
        ("Staying since", f"{t.move_in:%d-%m-%Y}"),
        ("Towards", "Rent (including electricity and food)" if p.kind == RentPayment.Kind.RENT
         else "Refundable advance / security deposit"),
        ("Paid by", p.get_mode_display() + (f" (ref {p.reference})" if p.reference else "")),
    ]
    for label, value in rows:
        if not value:
            continue
        doc.put(left, y, 42, 6, label, size=10)
        doc.put(left + 42, y, 4, 6, ":", size=10)
        doc.set_xy(left + 47, y)
        doc.set_font("Mono", "", 10)
        doc.multi_cell(width - 47, 6, str(value))
        y = doc.get_y() + 1
    y += 4
    doc.set_line_width(0.4)
    doc.rect(left, y, width, 18)
    doc.put(left + 4, y + 3, width - 8, 6, f"Amount received : {inr(p.amount)}", size=13, style="B")
    doc.put(left + 4, y + 10, width - 8, 5, f"({in_words(p.amount)})", size=9)
    y += 26
    if note := branding.footer(item, ""):
        doc.put(left, y, width, 5, note, size=9, align="C")
        y += 6
    doc.put(left, y, width, 5, "This is a computer-generated receipt and needs no signature.", size=8, align="C")
    return bytes(doc.output())
