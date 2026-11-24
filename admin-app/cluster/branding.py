"""A property's look on paper and screen: its logo (kept in its own Google Drive) and its business details.

The logo is fetched from Drive once and kept in the cache for a day (a failed fetch for 10 minutes), so pages,
invoices and receipts stay fast; a new upload clears it.
"""

from django.core.cache import cache

from . import current

DAY, FAILED = 24 * 3600, 600


def cache_key(item, which: str) -> str:
    return f"item-photo:{item.code}:{which}"


def photo(item, which: str = "logo") -> bytes | None:
    """The property's logo / cover photo as JPEG bytes, or None (none uploaded, or Drive can't be reached)."""
    file_id = getattr(item, f"{which}_id", "") if item is not None and which in {"cover", "logo"} else ""
    if not file_id:
        return None
    data = cache.get(cache_key(item, which))
    if data is None:
        from core import gdrive
        try:
            with current.using(item):
                data = gdrive.fetch(file_id)
        except Exception:
            data = b""
        cache.set(cache_key(item, which), data, DAY if data else FAILED)
    return data or None


def logo_url(item) -> str:
    """Where pages load the property's logo from ("" when it has none: the BSR icon is used)."""
    return photo_url(item, "logo")


def photo_url(item, which: str) -> str:
    """The photo's address, with its Drive file ID in it: a new upload gets a new address, so browsers never keep
    showing the old one."""
    file_id = getattr(item, f"{which}_id", "") if item is not None else ""
    return f"/properties/{item.code}/photo/{which}/?v={file_id[-12:]}" if file_id else ""


def business_lines(item) -> list[str]:
    """Legal name and registration numbers for invoices and receipts — only those filled in on the property's page."""
    if item is None:
        return []
    lines = []
    if item.legal_name and item.legal_name.strip().lower() != item.name.strip().lower():
        lines.append(item.legal_name.strip())
    ids = [f"{label}: {value.strip().upper()}" for label, value in
           (("GSTIN", item.gstin), ("PAN", item.pan), ("Udyam", item.udyam)) if value and value.strip()]
    if ids:
        lines.append(" · ".join(ids))
    return lines


def footer(item, default: str) -> str:
    return (item.invoice_footer or "").strip() if item is not None and (item.invoice_footer or "").strip() else default
