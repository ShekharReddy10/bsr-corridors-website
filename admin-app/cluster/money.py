"""Who may see a property's money, and when (decided with the user, 2026-11-28).

- A person sees amounts only for a property where they have **money access**. The main admin is no exception.
- Seeing them needs that property's **money password** (set by its owner) and a 6-digit code from the person's own
  **authenticator app** for that property. It then stays open for 30 minutes, for that property only.
- Money access (and who is an owner) is given on the property's Money access page: by an **owner** of the property who
  has set up their authenticator, confirming with a code. Until the property has such an owner, the main admin sets
  it up (that's how the first owner is named). The main admin can reset someone's authenticator; it is logged.
- Until unlocked, every amount on the screen shows as ₹ •••• (guest pages — payment link, invoice, receipt — never).
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from contextvars import ContextVar
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from .models import Membership, MoneyAuth

UNLOCK_SECONDS = 30 * 60
STEP = 30  # seconds per code
HIDDEN = "₹ ••••"

_visible: ContextVar = ContextVar("money_visible", default=True)  # PDFs, commands, guest pages: amounts shown


# ── Showing amounts on screen ─────────────────────────────────────────────────


def visible() -> bool:
    return _visible.get()


def set_visible(value: bool):
    return _visible.set(value)


def reset_visible(token) -> None:
    _visible.reset(token)


# ── Authenticator codes (TOTP, RFC 6238: what Google / Microsoft Authenticator use) ──


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def now_step() -> int:
    return int(time.time() // STEP)


def check_code(auth: MoneyAuth, code: str) -> bool:
    """Accept the current code (or the one just before / after, for a phone clock a little off). Each code works
    once: it can't be replayed by someone looking over a shoulder."""
    code = "".join(c for c in (code or "") if c.isdigit())
    if len(code) != 6 or not auth.secret:
        return False
    now = now_step()
    for step in (now - 1, now, now + 1):
        if step > auth.last_step and hmac.compare_digest(code_at(auth.secret, step), code):
            auth.last_step = step
            auth.save(update_fields=["last_step"])
            return True
    return False


def setup_uri(auth: MoneyAuth) -> str:
    """What the authenticator app scans: the property and the person in its name, so several properties don't mix."""
    issuer = settings.CLUSTER_NAME
    label = quote(f"{issuer}:{auth.item.name} ({auth.user.username})")
    return f"otpauth://totp/{label}?secret={auth.secret}&issuer={quote(issuer)}&digits=6&period={STEP}"


def qr_svg(uri: str) -> str:
    import segno
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2)


# ── Who ───────────────────────────────────────────────────────────────────────


def membership(user, item) -> Membership | None:
    if not getattr(user, "is_authenticated", False) or item is None:
        return None
    return Membership.objects.filter(user=user, item=item).first()


def has_access(user, item) -> bool:
    m = membership(user, item)
    return bool(m and m.money_access)


def auth_for(user, item, create: bool = False) -> MoneyAuth | None:
    if create:
        auth, _ = MoneyAuth.objects.get_or_create(user=user, item=item)
        return auth
    return MoneyAuth.objects.filter(user=user, item=item).first()


def controllers(item):
    """Owners with money access and their authenticator set up: they decide who sees this property's money."""
    ready = MoneyAuth.objects.filter(item=item, confirmed_at__isnull=False).exclude(secret="").values("user_id")
    return Membership.objects.filter(item=item, role=Membership.Role.OWNER, money_access=True, user_id__in=ready)


def controlled(item) -> bool:
    return controllers(item).exists()


def may_manage(user, item) -> bool:
    """Who may change this property's money access: its owners once one has an authenticator; until then the admin."""
    if controlled(item):
        return controllers(item).filter(user=user).exists()
    return bool(getattr(user, "is_superuser", False))


# ── Unlocked (per person, per property, for 30 minutes) ───────────────────────


def _key(item) -> str:
    return f"money_unlocked:{item.code}"


def unlocked(request, item=None) -> bool:
    item = item or getattr(request, "item", None)
    if item is None or not has_access(request.user, item):
        return False
    return request.session.get(_key(item), 0) > time.time()


def unlock(request, item) -> None:
    request.session[_key(item)] = time.time() + UNLOCK_SECONDS


def lock(request, item) -> None:
    request.session.pop(_key(item), None)


def reset(auth: MoneyAuth, by: str) -> None:
    auth.secret, auth.confirmed_at, auth.last_step = "", None, 0
    auth.reset_by, auth.reset_at = by[:60], timezone.now()
    auth.save()
