from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


def inr_format(value):
    """1234567.5 -> '\u20b912,34,567.50' (Indian digit grouping, 2 decimals); None/'' -> '\u2013'."""
    if value is None or value == "":
        return "\u2013"
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return "\u2013"
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}\u20b9{whole}.{frac}"


@register.filter
def inr(value):
    return inr_format(value)
