"""Short links for guests: https://<website>/s/Kp7xQ2a instead of a long signed address.

The code is random (7 characters, no look-alike letters), one per target page, so sending the same invoice or payment
link again gives the same short link. The target pages keep their own checks (signed tokens, expiry)."""

import secrets

from django.conf import settings
from django.db import IntegrityError, transaction

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
            with transaction.atomic():
                link = ShortLink.objects.create(path=path, code="".join(secrets.choice(ALPHABET) for _ in range(LENGTH)))
        except IntegrityError:  # code taken (or the same path saved at the same moment): try again
            link = ShortLink.objects.filter(path=path).first()
    base = settings.SHORT_LINK_BASE or f"{parts.scheme}://{parts.netloc}"
    return f"{base}/s/{link.code}"
