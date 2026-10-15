"""WhatsApp click-to-send: build a wa.me link with the message already typed.

The link opens WhatsApp on this phone / computer (whichever number is logged in there — ours is
7013684602); you check the text and tap Send. Nothing is sent by the server and no Meta account is needed.
"""

import re
from urllib.parse import quote

from django.conf import settings

from .models import AppSetting, Stay

# Hotel-wide details used in messages, edited on the WhatsApp page. key → (label, default).
HOTEL_INFO = {
    "wifi_name": ("Wi-Fi name", "BSR {floor}"),
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


def context(stay: Stay) -> dict[str, str]:
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
        "host": stay.host,
        "hotel": settings.HOTEL_NAME,
    }
    # Hotel info may use stay placeholders too, e.g. Wi-Fi name "BSR {floor}".
    return {**{k: render(v, ctx) for k, v in hotel_info().items()}, **ctx}


def render(body: str, ctx: dict[str, str]) -> str:
    """Fill {placeholders}; unknown ones (or stray braces) are left as typed."""
    return PLACEHOLDER.sub(lambda m: ctx.get(m.group(1), m.group(0)), body).strip()


def wa_number(phone: str) -> str:
    """Digits for wa.me: country code + number. A bare 10-digit (or 0-prefixed) number is taken as Indian."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    return digits


def link(phone: str, text: str) -> str:
    return f"https://wa.me/{wa_number(phone)}?text={quote(text)}"
