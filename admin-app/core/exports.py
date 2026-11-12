"""Excel exports and JSON backups."""

import io
from datetime import date

from django.core import serializers
from django.db.models import Q
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import revenue
from .models import AuditLog, Expense, Guest, MaintenanceLog, MessageTemplate, Payment, IdPhoto, RecurringExpense, Room, RoomType, Settlement, Stay, StayGuest, StayIdPhoto

HEADER_FILL = PatternFill("solid", fgColor="1F2A30")
HEADER_FONT = Font(bold=True, color="FFFFFF")


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list], first: bool = False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    for i, header in enumerate(headers, start=1):
        width = max([len(str(header))] + [len(str(r[i - 1])) for r in rows[:500] if r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 50)
    return ws


def _id(value: str, full: bool) -> str:
    if not value:
        return ""
    return value if full else f"••••{value[-4:]}"


def stays_in_range(start: date | None = None, end: date | None = None):
    stays = (Stay.objects.select_related("guest", "room", "room__room_type").prefetch_related("other_guests")
             .order_by("check_in", "room__number"))
    if start and end:
        stays = stays.filter(Q(check_out__gte=start) | Q(check_out__isnull=True), check_in__lte=end)
    return stays


def build_workbook(start: date | None = None, end: date | None = None, full_ids: bool = False) -> bytes:
    stays = list(stays_in_range(start, end))
    wb = Workbook()
    _sheet(
        wb,
        "Stays",
        ["Stay ID", "Guest", "Phone", "Room", "Room type", "Stay type", "Check-in", "Check-out", "Nights", "Guests",
         "Booked via", "Booking ref", "Total", "Paid", "Balance", "Monthly rent", "Deposit", "Payment mode", "Status",
         "Host", "Transferred to", "Transfer reason",
         "Nationality",
         "ID proof", "ID number", "Address", "Other guests", "Form C filed", "Notes"],
        [
            [s.pk, s.guest.name, s.guest.phone, s.room.number, s.room.room_type.name, s.get_kind_display(), s.check_in,
             s.check_out or "Open (monthly)", s.nights, s.num_guests, s.get_source_display(), s.source_ref,
             float(s.total), float(s.amount_paid), float(s.balance), float(s.monthly_rent), float(s.deposit_amount),
             s.get_payment_mode_display(), s.get_status_display(), s.host, s.transferred_to, s.transfer_reason,
             s.guest.nationality,
             s.guest.get_id_type_display(), _id(s.guest.id_number, full_ids), s.guest.address,
             "; ".join(f"{o.name or 'Guest ' + str(o.position)}"
                       + (f", {o.phone}" if o.phone else "")
                       + (f" ({o.get_id_type_display()} {_id(o.id_number, full_ids)})" if o.id_number else "")
                       for o in s.other_guests.all()),
             "Yes" if s.form_c_filed else ("No" if s.guest.is_foreign else ""), s.notes]
            for s in stays
        ],
        first=True,
    )
    guests = {s.guest_id: s.guest for s in stays} if start else {g.pk: g for g in Guest.objects.all()}
    _sheet(
        wb,
        "Guests",
        ["Guest ID", "Name", "Phone", "Address", "Nationality", "ID proof", "ID number",
         "Passport", "Visa number", "Visa type", "Arrival in India"],
        [
            [g.pk, g.name, g.phone, g.address, g.nationality, g.get_id_type_display(), _id(g.id_number, full_ids),
             _id(g.passport_number, full_ids), g.visa_number, g.visa_type, g.arrival_in_india]
            for g in sorted(guests.values(), key=lambda g: g.name.lower())
        ],
    )
    _sheet(
        wb,
        "Rooms",
        ["Room", "Floor", "Type", "AC", "Bed", "Max guests", "Status", "Active"],
        [
            [r.number, r.floor, r.room_type.name, "AC" if r.room_type.is_ac else "Non-AC", r.room_type.bed_type,
             r.room_type.max_guests, r.get_status_display(), "Yes" if r.is_active else "No"]
            for r in Room.objects.select_related("room_type")
        ],
    )
    expenses = Expense.objects.select_related("room").order_by("date", "pk")
    if start and end:
        expenses = expenses.filter(date__gte=start, date__lte=end)
    _sheet(
        wb,
        "Expenses",
        ["Date", "Category", "Reason", "Amount", "Payment mode", "Paid to", "Bill / reference", "Room", "Notes"],
        [
            [e.date, e.get_category_display(), e.reason, float(e.amount), e.get_payment_mode_display(), e.paid_to,
             e.reference, e.room.number if e.room else "", e.notes]
            for e in expenses
        ],
    )
    payments = Payment.objects.select_related("stay__guest", "stay__room").order_by("date", "pk")
    if start and end:
        payments = payments.filter(date__gte=start, date__lte=end)
    _sheet(
        wb,
        "Payments",
        ["Date", "Guest", "Room", "Stay ID", "Amount", "Mode", "Collected by", "Note"],
        [[p.date, p.stay.guest.name, p.stay.room.number, p.stay_id, float(p.amount), p.get_mode_display(),
          p.collected_by, p.note] for p in payments],
    )
    months = revenue.build("all").months()
    if start and end:
        months = [(m, b) for m, b in months if start.replace(day=1) <= m <= end]
    _sheet(
        wb,
        "Monthly summary",
        ["Month", "Received", "Expenses", "Profit", "Settled", "Left to settle"],
        [[m.strftime("%B %Y"), float(b.received), float(b.expenses), float(b.profit), float(b.settled), float(b.left)]
         for m, b in months],
    )
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, date):
                    cell.number_format = "DD-MM-YYYY"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


BACKUP_MODELS = [RoomType, Room, Guest, Stay, StayGuest, IdPhoto, StayIdPhoto, Payment, RecurringExpense, Expense, MaintenanceLog, Settlement, MessageTemplate, AuditLog]


def backup_json() -> bytes:
    """Full backup in Django fixture format (restore with `manage.py loaddata`).

    ID numbers stay encrypted — restoring needs the same FIELD_ENCRYPTION_KEY.
    """
    objects = [obj for model in BACKUP_MODELS for obj in model.objects.order_by("pk")]
    return serializers.serialize("json", objects, indent=1).encode()
