"""UPI payment links (send from the stay page, the guest's public pay page) and the Testing page."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import invoice, payments, services, whatsapp
from ..shortlinks import shorten
from ..models import AuditLog, Guest, MessageTemplate, PaymentRequest, Room, RoomType, Stay
from .whatsapp import MOBILE, AppRedirect

TEST_ROOM = "TEST"
TEST_TYPE = "Test room (delete after testing)"
TEST_GUEST = "Test Guest"


def _link_message():
    """The message that carries the link: the one named Payment link, else any with {payment_link}."""
    with_link = [t for t in MessageTemplate.objects.all() if "{payment_link}" in t.body]
    return next((t for t in with_link if t.name.strip().lower() == "payment link"), with_link[0] if with_link else None)


@require_POST
def send_link(request, pk):
    """Create a payment link for the amount entered (prefilled with the balance) and open it in WhatsApp."""
    stay = get_object_or_404(Stay.objects.select_related("guest", "room"), pk=pk)
    try:
        amount = Decimal(request.POST.get("amount") or "0").quantize(Decimal("0.01"))
    except InvalidOperation:
        amount = Decimal(0)
    template = _link_message()
    problem = ("Payment links need the hotel’s UPI ID — add UPI_ID in Render → Environment." if not payments.configured()
               else f"Add {stay.guest.name}’s phone number first." if not stay.guest.phone
               else "A cancelled or transferred stay can’t take payments." if stay.is_void
               else "Enter an amount more than zero." if amount <= 0
               else "The “Payment link” WhatsApp message is missing — add one with {payment_link}." if not template
               else "")
    if problem:
        messages.error(request, problem)
        return redirect("stay_detail", pk=pk)
    req = payments.create(stay, amount, request.user.get_full_name() or request.user.username)
    ctx = {**whatsapp.context(stay, amount, request.build_absolute_uri("/")),
           "payment_link": shorten(payments.link(req, request.build_absolute_uri("/")))}
    text = whatsapp.with_footer(whatsapp.render(template.body, ctx))
    mobile = bool(MOBILE.search(request.headers.get("User-Agent", "")))
    return AppRedirect(whatsapp.link(stay.guest.phone, text, mobile))


def guest_page(request, token):
    """Public (no login): the guest's payment page. A wrong or altered link is a plain 404."""
    req = payments.for_token(token)
    if req is None:
        raise Http404
    stay = req.stay
    if request.method == "POST" and req.status == PaymentRequest.Status.OPEN and not stay.is_void:
        if request.POST.get("paid") == "1":
            payments.claim(req, request.POST.get("reference", ""))
            return redirect("pay_page", token=token)
    confirm = ""
    if req.status == PaymentRequest.Status.CLAIMED and (number := payments.hotel_whatsapp()):
        text = (f"Hello {settings.HOTEL_NAME}, I have paid {invoice.money(req.amount)} by UPI for Room "
                f"{stay.room.number} ({stay.guest.name})." + (f" UPI reference: {req.reference}." if req.reference else ""))
        confirm = f"https://wa.me/{number}?text={quote(text)}"
    open_ = req.status == PaymentRequest.Status.OPEN and not stay.is_void
    return render(request, "core/pay.html", {
        "req": req, "stay": stay, "open": open_, "confirm": confirm,
        "upi": payments.upi_uri(req) if open_ else "", "qr": payments.qr_png(req) if open_ else "",
        "upi_id": settings.UPI_ID, "upi_name": settings.UPI_NAME, "first_name": stay.guest.name.split()[0],
    })


# ── Testing ──


def _test_stays():
    return Stay.objects.filter(room__number=TEST_ROOM).select_related("guest", "room").order_by("-pk")


def testing(request):
    """A test room, guest and stay, to try payment links, invoices and messages on your own phone — and one tap to
    remove them all afterwards (they count in revenue while they exist)."""
    if request.method == "POST":
        if request.POST.get("action") == "delete":
            n = 0
            for s in _test_stays():
                invoice.forget(s)
                s.delete()
                n += 1
            Guest.objects.filter(name=TEST_GUEST, stays__isnull=True).delete()
            Room.objects.filter(number=TEST_ROOM, stays__isnull=True).delete()
            RoomType.objects.filter(name=TEST_TYPE, rooms__isnull=True).delete()
            AuditLog.record("testing", f"Test data deleted ({n} test stay{'s' if n != 1 else ''})")
            messages.success(request, "Test room, guest and stays deleted.")
            return redirect("testing")
        phone = request.POST.get("phone", "").strip()
        try:
            amount = Decimal(request.POST.get("amount") or "1")
        except InvalidOperation:
            amount = Decimal(1)
        if sum(c.isdigit() for c in phone) < 10:
            messages.error(request, "Enter your WhatsApp number (at least 10 digits) — the test messages go there.")
            return redirect("testing")
        other = Guest.objects.filter(phone_key=Guest.key_for(phone)).exclude(name=TEST_GUEST).first()
        if other:
            messages.error(request, f"That number is saved for {other.name}. Use another number for testing.")
            return redirect("testing")
        rtype, _ = RoomType.objects.get_or_create(name=TEST_TYPE, defaults={"max_guests": 4})
        room, _ = Room.objects.get_or_create(number=TEST_ROOM, defaults={"room_type": rtype, "sort_order": 9999,
                                                                          "notes": "For testing — delete from the Testing page."})
        guest = Guest.objects.filter(phone_key=Guest.key_for(phone)).first() or Guest(name=TEST_GUEST)
        guest.phone, guest.address = phone, guest.address or "Test address"
        guest.save()
        t = services.today()
        live = _test_stays().filter(status__in=[Stay.Status.UPCOMING, Stay.Status.CHECKED_IN]).first()
        if live:
            messages.info(request, "A test stay is already open — here it is.")
            return redirect("stay_detail", pk=live.pk)
        s = Stay(guest=guest, room=room, check_in=t, check_out=t + timedelta(days=1), total_amount=amount,
                 source=Stay.Source.WALK_IN, status=Stay.Status.CHECKED_IN, host=(services.host_names() or [""])[0],
                 notes="TEST — delete from the Testing page when done.")
        try:
            services.save_stay(s, action="create", summary="Test stay created (Testing page)")
        except services.StayConflict:
            messages.error(request, "The test room is busy — delete the test data and try again.")
            return redirect("testing")
        messages.success(request, f"Test stay created: Room {TEST_ROOM}, {invoice.money(amount)}.")
        return redirect("stay_detail", pk=s.pk)
    return render(request, "core/testing.html", {
        "stays": list(_test_stays()), "upi_ready": payments.configured(), "upi_id": settings.UPI_ID,
    })
