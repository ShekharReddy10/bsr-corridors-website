"""Typo checks: amounts that look wrong compared with our own usual figures (an extra zero, too many nights for
the amount, paid more than the bill, …). Flagged entries are listed to double-check until someone edits them or
taps "Looks right" — a confirmation is tied to the exact figures, so changing them later asks again.

Usual figures are medians of past entries (so one odd stay doesn't move them); a check only runs once there are
enough past entries to compare with.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from django.urls import reverse

from .models import AppSetting, Expense, Stay

CONFIRMED_KEY = "checked_ok"
STAY_FACTOR = 3  # per-night price or monthly rent this many times above / below the usual
EXPENSE_FACTOR = 5  # expense this many times the usual for its category
EXPENSE_MIN = Decimal(1000)  # small expenses are never flagged
MIN_SAMPLES = 5
MAX_DAILY_NIGHTS = 31
RECENT_DAYS = 45  # only recent / current entries are checked


@dataclass
class Flag:
    key: str  # changes when the figures change, so a "Looks right" only covers what was seen
    title: str
    reason: str
    url: str


def _inr(v) -> str:
    return f"₹{v:,.0f}"


def _confirmed() -> set[str]:
    return set(filter(None, AppSetting.get(CONFIRMED_KEY).split(",")))


def confirm(key: str) -> None:
    keys = [k for k in AppSetting.get(CONFIRMED_KEY).split(",") if k and k != key] + [key]
    AppSetting.put(CONFIRMED_KEY, ",".join(keys[-500:]))


def _n(v) -> str:
    return f"{Decimal(v or 0):.2f}"  # same text whether the amount came from a form or the database


def _stay_key(s: Stay) -> str:
    return f"s{s.pk}-{_n(s.total_amount)}-{_n(s.amount_paid)}-{_n(s.monthly_rent)}-{s.check_in}-{s.check_out}"


def _expense_key(e: Expense) -> str:
    return f"e{e.pk}-{_n(e.amount)}-{e.category}"


def _nightly(s: Stay) -> Decimal | None:
    if s.kind != Stay.Kind.DAILY or not s.check_out or not s.total_amount:
        return None
    nights = (s.check_out - s.check_in).days
    return s.total_amount / nights if nights > 0 else None


class Usual:
    """Median per-night price per room type (and overall), median monthly rent, median expense per category."""

    def __init__(self, exclude_stay=None, exclude_expense=None):
        nightly_by_type, nightly_all, rents = {}, [], []
        for s in Stay.objects.live().exclude(pk=exclude_stay).select_related("room"):
            rate = _nightly(s)
            if rate:
                nightly_by_type.setdefault(s.room.room_type_id, []).append(rate)
                nightly_all.append(rate)
            if s.kind == Stay.Kind.MONTHLY and s.monthly_rent:
                rents.append(s.monthly_rent)
        self.nightly = {k: median(v) for k, v in nightly_by_type.items() if len(v) >= MIN_SAMPLES}
        self.nightly_all = median(nightly_all) if len(nightly_all) >= MIN_SAMPLES else None
        self.rent = median(rents) if len(rents) >= MIN_SAMPLES else None
        by_category = {}
        for cat, amount in Expense.objects.exclude(pk=exclude_expense).values_list("category", "amount"):
            by_category.setdefault(cat, []).append(amount)
        self.expense = {k: median(v) for k, v in by_category.items() if len(v) >= MIN_SAMPLES}


def stay_reasons(s: Stay, usual: Usual) -> list[str]:
    out = []
    total, paid = s.total_amount or Decimal(0), s.amount_paid or Decimal(0)
    if not total and not (s.kind == Stay.Kind.MONTHLY and s.monthly_rent):
        out.append("No amount entered for this stay.")
    if total and paid > total and s.kind == Stay.Kind.DAILY:
        out.append(f"Paid {_inr(paid)} is more than the bill {_inr(total)}.")
    rate = _nightly(s)
    usual_rate = usual.nightly.get(s.room.room_type_id, usual.nightly_all)
    if rate and usual_rate:
        nights = (s.check_out - s.check_in).days
        if rate >= usual_rate * STAY_FACTOR:
            out.append(f"{_inr(rate)} a night — about {rate / usual_rate:.0f}× the usual {_inr(usual_rate)} for "
                       f"{s.room.room_type.name}. Extra zero in the amount?")
        elif rate * STAY_FACTOR <= usual_rate:
            out.append(f"Only {_inr(rate)} a night for {nights} night{'s' if nights != 1 else ''} — the usual is "
                       f"{_inr(usual_rate)}. Wrong number of nights, or amount missing a digit?")
    if s.kind == Stay.Kind.DAILY and s.check_out and (s.check_out - s.check_in).days > MAX_DAILY_NIGHTS:
        out.append(f"{(s.check_out - s.check_in).days} nights as a daily stay — should it be a monthly stay, "
                   f"or is the check-out date wrong?")
    if s.kind == Stay.Kind.MONTHLY and s.monthly_rent and usual.rent:
        if s.monthly_rent >= usual.rent * STAY_FACTOR or s.monthly_rent * STAY_FACTOR <= usual.rent:
            out.append(f"Monthly rent {_inr(s.monthly_rent)} — the usual is {_inr(usual.rent)}.")
    return out


def expense_reasons(e: Expense, usual: Usual) -> list[str]:
    typical = usual.expense.get(e.category)
    if typical and e.amount >= EXPENSE_MIN and e.amount >= typical * EXPENSE_FACTOR:
        return [f"{_inr(e.amount)} — about {e.amount / typical:.0f}× the usual {_inr(typical)} for "
                f"{e.get_category_display().lower()}. Extra zero?"]
    return []


def stay_flags(t: date) -> list[Flag]:
    """Current and recent stays (upcoming, in-house, or ended in the last few weeks) that look wrong."""
    usual, ok = Usual(), _confirmed()
    stays = (Stay.objects.live().select_related("guest", "room", "room__room_type")
             .exclude(check_out__lt=t - timedelta(days=RECENT_DAYS)))
    out = []
    for s in stays:
        reasons = stay_reasons(s, usual)
        if reasons and _stay_key(s) not in ok:
            out.append(Flag(_stay_key(s), f"{s.guest.name} · Room {s.room.number} · {s.dates_label}",
                            " ".join(reasons), reverse("stay_edit", args=[s.pk])))
    return out


def expense_flags(t: date) -> list[Flag]:
    usual, ok = Usual(), _confirmed()
    out = []
    for e in Expense.objects.filter(date__gte=t - timedelta(days=RECENT_DAYS)):
        reasons = expense_reasons(e, usual)
        if reasons and _expense_key(e) not in ok:
            out.append(Flag(_expense_key(e), f"{e.date:%d-%m-%Y} · {e.reason}", " ".join(reasons),
                            reverse("expense_edit", args=[e.pk])))
    return out


def check_stay(s: Stay) -> list[str]:
    """Right after saving: anything that looks wrong with this stay (compared with the others)."""
    return stay_reasons(s, Usual(exclude_stay=s.pk))


def check_expense(e: Expense) -> list[str]:
    return expense_reasons(e, Usual(exclude_expense=e.pk))
