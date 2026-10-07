from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def inr(value):
    """1234567.5 → '₹12,34,568' (Indian digit grouping, no paise)."""
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


@register.inclusion_tag("partials/field.html")
def field(bound_field, css=""):
    return {"f": bound_field, "css": css}
