"""WhatsApp click-to-send: open the guest's chat with the message already typed.

The link opens WhatsApp on this phone / computer (whichever number is logged in there — ours is
7013684602); you check the text and tap Send. Nothing is sent by the server and no Meta account is needed.
"""

import re
from decimal import Decimal
from urllib.parse import quote

from django.conf import settings

from .models import AppSetting, Stay

# Hotel-wide details used in messages, edited on the WhatsApp page. key → (label, default).
HOTEL_INFO = {
    "wifi_name": ("Wi-Fi name", "BSR (your floor name)"),
    "wifi_password": ("Wi-Fi password", ""),  # never in the code: the repo is public
    "check_in_time": ("Check-in time", "11:00 AM – 10:00 PM"),
    "check_out_time": ("Check-out time", "10:00 AM"),
    "house_rules": ("House rules",
                    "• No smoking inside the rooms — please use the terrace.\n"
                    "• Please don’t eat on the beds, as it stains the white bedsheets. Use the dining table in the "
                    "kitchen or the table and chair in your room.\n"
                    "• Please don’t take towels, TV remotes, AC remotes or cupboard keys when checking out."),
    "location": ("Location / map link",
                 "1-60/11/5/5, Lakshmi Naga Nilayam, Banjara Nagar, Gachibowli, Hyderabad 500032\n"
                 "https://maps.app.goo.gl/Ynf988EGZqbhA1pe7"),
    "contact": ("Contact name & number", "+91 70136 84602 / +91 87908 23320"),
    "review_link": ("Google review link", ""),
}

# Placeholders filled from the stay. key → description (shown on the WhatsApp page).
STAY_FIELDS = {
    "guest": "Guest’s name",
    "room": "Room number",
    "floor": "Floor (from the Rooms page; else from the room number: 101 → 1)",
    "check_in": "Check-in date",
    "check_out": "Check-out date",
    "nights": "Number of nights",
    "total": "Total amount",
    "paid": "Amount paid",
    "balance": "Balance due",
    "invoice_link": "Link to the guest’s invoice (PDF) — opens without a login, shows only their invoice",
    "invoice_no": "Invoice number, e.g. 261115-261118-101",
    "payment_link": "UPI payment link (only in the “Payment link” message — sent from the stay’s Payment section)",
    "amount": "Amount just received (typed in next to the button; else the latest payment)",
    "ext_from": "Extension: first extra night (only in the “Stay extended” message)",
    "ext_to": "Extension: new check-out date",
    "ext_nights": "Extension: number of extra nights",
    "ext_amount": "Extension: amount for the extra nights",
    "prev_balance": "Extension: balance already due before it (the line is left out when there was none)",
    "total_due": "Total still to pay for the whole stay (extension + any earlier balance, less payments)",
    "host": "Host looking after the guest",
    "hotel": "Hotel name",
}

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def hotel_info() -> dict[str, str]:
    return {key: AppSetting.get(f"wa_{key}", default) for key, (_, default) in HOTEL_INFO.items()}


def save_hotel_info(values: dict[str, str]) -> None:
    for key in HOTEL_INFO:
        AppSetting.put(f"wa_{key}", values.get(key, "").strip())


def _money(v) -> str:
    return f"₹{v:,.0f}"


def floor(room) -> str:
    if room.floor.strip():
        return room.floor.strip()
    digits = "".join(c for c in room.number if c.isdigit())
    return digits[:-2] if len(digits) > 2 else ""


def context(stay: Stay, amount=None, base_url: str = "") -> dict[str, str]:
    """base_url: the site address (https://…/), for links in the message."""
    from . import invoice
    from .shortlinks import shorten
    if amount is None:
        last = stay.payments.order_by("-date", "-pk").first() if stay.pk else None
        amount = last.amount if last else stay.amount_paid
    has_inv = bool(stay.pk) and invoice.has_invoice(stay)
    ctx = {
        "guest": stay.guest.name,
        "room": stay.room.number,
        "floor": floor(stay.room),
        "check_in": f"{stay.check_in:%a %d-%m-%Y}",
        "check_out": f"{stay.check_out:%a %d-%m-%Y}" if stay.check_out else "open (monthly)",
        "nights": str(stay.nights),
        "total": _money(stay.total),
        "paid": _money(stay.amount_paid),
        "balance": _money(max(stay.balance, 0)),
        "amount": _money(amount or 0),
        "host": stay.host,
        "hotel": settings.HOTEL_NAME,
        # Only once the invoice is generated; until then the message's invoice part is left out (see render).
        "invoice_no": invoice.number(stay) if has_inv else "",
        "invoice_link": shorten(f"{base_url.rstrip('/')}/invoice/{invoice.token(stay)}/") if has_inv else "",
        "total_due": _money(max(due(stay), 0)) if stay.pk else _money(max(stay.balance, 0)),
    }
    if stay.pk and (ext := extension(stay)):
        ctx.update({"ext_from": f"{ext['from']:%a %d-%m-%Y}", "ext_to": f"{ext['to']:%a %d-%m-%Y}",
                    "ext_nights": str(ext["nights"]), "ext_amount": _money(ext["amount"]),
                    "prev_balance": _money(ext["prev"]) if ext["prev"] > 0 else ""})
    # Hotel info may use stay placeholders too, e.g. a Wi-Fi name "BSR {floor}".
    return {**{k: render(v, ctx) for k, v in hotel_info().items()}, **ctx}


def render(body: str, ctx: dict[str, str]) -> str:
    """Fill {placeholders}; unknown ones (or stray braces) are left as typed. A paragraph with the invoice link is
    left out while the stay has no invoice (not generated yet, or a platform booking not extended with us)."""
    if "invoice_link" in ctx and not ctx["invoice_link"]:
        body = "\n\n".join(p for p in re.split(r"\n\s*\n", body) if "{invoice_link}" not in p and "{invoice_no}" not in p)
    if ctx.get("prev_balance") == "":  # no earlier balance: that line goes
        body = "\n".join(line for line in body.split("\n") if "{prev_balance}" not in line)
    return PLACEHOLDER.sub(lambda m: ctx.get(m.group(1), m.group(0)), body).strip()


def footer() -> str:
    return f"- {settings.HOTEL_NAME}"


def with_footer(text: str) -> str:
    """Every message ends with the hotel's sign-off (once, even if the message already has it)."""
    text = text.rstrip()
    if not text or text.endswith(footer()):
        return text
    return f"{text}\n\n{footer()}"


def wa_number(phone: str) -> str:
    """Digits for WhatsApp: country code + number. A bare 10-digit (or 0-prefixed) number is taken as Indian."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    return digits


def link(phone: str, text: str, mobile: bool) -> str:
    """Straight to the chat: the WhatsApp app on a phone, WhatsApp Web on a computer
    (wa.me would first show a "Chat on WhatsApp" page)."""
    query = f"phone={wa_number(phone)}" + (f"&text={quote(text)}" if text else "")
    return f"whatsapp://send?{query}" if mobile else f"https://web.whatsapp.com/send?{query}"


# ── Stay extensions: the latest one per stay, for the "Stay extended" message ──

EXT_FIELDS = ("{ext_from}", "{ext_to}", "{ext_nights}", "{ext_amount}", "{prev_balance}")


def due(stay: Stay) -> Decimal:
    """Still to pay for the whole stay (all its rooms after a move)."""
    return sum((s.balance for s in stay.chain() if not s.is_void), Decimal(0))


def record_extension(stay: Stay, old_check_out, new_check_out, amount, prev_balance) -> None:
    import json
    AppSetting.put(f"extension_{stay.root.pk}", json.dumps({
        "from": old_check_out.isoformat(), "to": new_check_out.isoformat(),
        "nights": (new_check_out - old_check_out).days, "amount": str(amount or 0), "prev": str(max(prev_balance, 0))}))


def extension(stay: Stay) -> dict | None:
    import json
    from datetime import date
    raw = AppSetting.get(f"extension_{stay.root.pk}")
    if not raw:
        return None
    d = json.loads(raw)
    return {"from": date.fromisoformat(d["from"]), "to": date.fromisoformat(d["to"]), "nights": d["nights"],
            "amount": Decimal(d["amount"]), "prev": Decimal(d["prev"])}


def extension_message():
    """The message sent after an extension: the one named “Stay extended”, else any with {ext_amount}."""
    from .models import MessageTemplate
    with_ext = [t for t in MessageTemplate.objects.all() if "{ext_amount}" in t.body]
    return next((t for t in with_ext if t.name.strip().lower() == "stay extended"), with_ext[0] if with_ext else None)
