from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


def _hidden() -> bool:
    from cluster import money
    return not money.visible()


@register.filter
def inr(value):
    """1234567.5 → '₹12,34,568' (Indian digit grouping, no paise). Money locked: '₹ ••••'."""
    if _hidden() and value not in (None, ""):
        from cluster.money import HIDDEN
        return HIDDEN
    try:
        n = int(Decimal(value).quantize(Decimal("1")))
    except (InvalidOperation, TypeError, ValueError):
        return value
    sign, s = ("-" if n < 0 else ""), str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{sign}₹{s}"


@register.filter
def invoice_pending(stay):
    """The stay has an invoice to make that isn't generated yet (it's made at check-out / with the message)."""
    from ..invoice import pending
    return pending(stay)


@register.filter
def invoice_no(stay):
    """The invoice number if the stay has a generated invoice, else ""."""
    from ..invoice import has_invoice, number
    return number(stay) if has_invoice(stay) else ""


@register.filter
def inr_short(value):
    """Chart axes: 150000 → '₹1.5L', 45000 → '₹45K', 2.5 crore → '₹2.5Cr'."""
    if _hidden():
        from cluster.money import HIDDEN
        return HIDDEN
    try:
        n = float(value)
    except (TypeError, ValueError):
        return value
    for size, unit in ((1e7, "Cr"), (1e5, "L"), (1e3, "K")):
        if abs(n) >= size:
            return f"₹{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return f"₹{n:.0f}"


@register.inclusion_tag("partials/field.html")
def field(bound_field, css=""):
    return {"f": bound_field, "css": css}


@register.filter
def amt(value):
    """A plain whole number for a form field or a page script (e.g. 4500); "" while money is locked."""
    if _hidden():
        return ""
    try:
        return str(int(Decimal(value).quantize(Decimal("1"))))
    except (InvalidOperation, TypeError, ValueError):
        return ""


MONEY_TEXT = None


@register.filter
def hide_money(text):
    """Logs and messages: '₹4,500 received' → '₹ •••• received' while money is locked."""
    global MONEY_TEXT
    if not _hidden() or not text:
        return text
    import re
    from cluster.money import HIDDEN
    if MONEY_TEXT is None:
        MONEY_TEXT = re.compile(r"(?:₹|Rs\.?)\s?-?[\d,]+(?:\.\d+)?")
    return MONEY_TEXT.sub(HIDDEN, str(text))


@register.simple_tag
def money_shown():
    """In templates: {% money_shown as shown %} — True when amounts are shown."""
    return not _hidden()
