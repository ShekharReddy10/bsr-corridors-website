"""Short links for guests: https://<website>/s/Kp7xQ2a instead of a long signed address.

The code is random (7 characters, no look-alike letters), one per target page, so sending the same invoice or payment
link again gives the same short link. The target pages keep their own checks (signed tokens, expiry)."""

import secrets

from django.db import IntegrityError

from cluster.current import short_base, short_prefix

from .db import atomic
from .models import ShortLink

ALPHABET = "23456789abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O, 1/l/I
LENGTH = 7


def shorten(url: str) -> str:
    """https://app/pay/<token>/ → https://<SHORT_LINK_BASE or app>/s/<code>."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    link = ShortLink.objects.filter(path=path).first()
    while link is None:
        try:
            with atomic():
                link = ShortLink.objects.create(path=path, code="".join(secrets.choice(ALPHABET) for _ in range(LENGTH)))
        except IntegrityError:  # code taken (or the same path saved at the same moment): try again
            link = ShortLink.objects.filter(path=path).first()
    base = short_base() or f"{parts.scheme}://{parts.netloc}"
    return f"{base}/s/{short_prefix()}{link.code}"
