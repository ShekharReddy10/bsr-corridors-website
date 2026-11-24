"""UPI payment links — no payment gateway, no fees: the guest pays straight to the hotel's UPI ID (settings.UPI_ID).

Staff send a link on WhatsApp (amount prefilled with the balance, can be changed). The guest's page has a "Pay with
UPI" button (opens GPay / PhonePe / Paytm / BHIM with everything filled in) and a QR code. Without a gateway nobody
tells us the money arrived, so the guest ticks "I have paid" (with the UPI reference, optional): the link closes, they
confirm to us on WhatsApp, and the stay page asks staff to check the UPI app and record the payment.
"""

import re
from decimal import Decimal
from urllib.parse import quote, urlencode

from django.core import signing

from django.utils import timezone

from cluster.current import name as hotel_name, upi as payments_upi

from .db import atomic
from .models import AuditLog, PaymentRequest, Stay

SALT = "pay"


def configured() -> bool:
    return bool(payments_upi()[0])


def token(req: PaymentRequest) -> str:
    return signing.Signer(salt=SALT).sign(str(req.pk))


def for_token(value: str) -> PaymentRequest | None:
    try:
        pk = int(signing.Signer(salt=SALT).unsign(value))
    except (signing.BadSignature, ValueError):
        return None
    return PaymentRequest.objects.filter(pk=pk).select_related("stay", "stay__guest", "stay__room").first()


def link(req: PaymentRequest, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/pay/{token(req)}/"


def upi_uri(req: PaymentRequest) -> str:
    """upi://pay?… — opens the guest's UPI app with the payee, amount and note filled in."""
    note = f"Room {req.stay.room.number} {hotel_name()}"[:50]
    return "upi://pay?" + urlencode({"pa": payments_upi()[0], "pn": payments_upi()[1], "am": f"{req.amount:.2f}",
                                     "cu": "INR", "tn": note}, quote_via=quote)


def qr_png(req: PaymentRequest) -> str:
    """The same UPI request as a QR code (PNG data URI). Small merchants can only be paid by scanning a QR (UPI rules
    block "pay by link" for them), so guests on the same phone save this image and open it from their UPI app's
    scanner (gallery / upload QR)."""
    import segno
    return segno.make(upi_uri(req), error="m").png_data_uri(scale=8, border=3)


def create(stay: Stay, amount: Decimal, by: str = "") -> PaymentRequest:
    """A new link for this stay; older links still waiting for payment stop working (only the latest is valid)."""
    if amount <= 0:
        raise ValueError("Enter an amount more than zero.")
    with atomic():
        stay.payment_requests.filter(status=PaymentRequest.Status.OPEN).update(status=PaymentRequest.Status.REPLACED)
        req = PaymentRequest.objects.create(stay=stay, amount=amount, created_by=by)
        AuditLog.record("payment_link", f"UPI payment link sent for ₹{amount:,.0f}", stay)
    return req


def claim(req: PaymentRequest, reference: str) -> bool:
    """The guest ticked "I have paid". Returns False if the link was no longer open."""
    reference = re.sub(r"[^0-9A-Za-z]", "", reference or "")[:40]
    with atomic():
        n = PaymentRequest.objects.filter(pk=req.pk, status=PaymentRequest.Status.OPEN).update(
            status=PaymentRequest.Status.CLAIMED, reference=reference, claimed_at=timezone.now())
        if n:
            req.refresh_from_db()
            AuditLog.record("payment_link", f"Guest says paid ₹{req.amount:,.0f} by UPI"
                                            f"{f' (ref {reference})' if reference else ''} — to verify", req.stay)
    return bool(n)


def mark_paid(req: PaymentRequest) -> None:
    PaymentRequest.objects.filter(pk=req.pk).update(status=PaymentRequest.Status.PAID)


def not_received(req: PaymentRequest) -> None:
    """Staff checked: the money didn't come — the link works again for the guest."""
    PaymentRequest.objects.filter(pk=req.pk, status=PaymentRequest.Status.CLAIMED).update(
        status=PaymentRequest.Status.OPEN, claimed_at=None)
    AuditLog.record("payment_link", f"UPI payment of ₹{req.amount:,.0f} not received — link open again", req.stay)


def to_verify():
    """Links where the guest says they paid, waiting for staff to check the UPI app."""
    return (PaymentRequest.objects.filter(status=PaymentRequest.Status.CLAIMED)
            .select_related("stay", "stay__guest", "stay__room").order_by("claimed_at"))


def hotel_whatsapp() -> str:
    """The hotel's number for the guest's confirmation: the first number in hotel info "Contact"."""
    from cluster import current
    from .whatsapp import hotel_info, wa_number
    item = current.get()
    if item is not None and not item.is_default and item.whatsapp_phone:
        return wa_number(item.whatsapp_phone)
    m = re.search(r"\+?\d[\d \-]{8,}\d", hotel_info().get("contact", ""))
    return wa_number(m.group(0)) if m else ""
