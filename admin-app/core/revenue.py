"""Revenue figures for the password-protected Revenue page.

Room revenue is counted per night stayed: a stay's total is spread evenly over its nights, so a
3-night ₹4,500 stay adds ₹1,500 to each night (and to that night's week, month and room type).
Nights already stayed are "earned"; later nights of current and upcoming stays are "booked ahead".

Money: guests who have checked in owe "balance pending" (bill − paid). What they paid counts as "received" night by
night: each night's share of the bill counts once that night is stayed (the next morning), in the month of that night —
a 10-night ₹10,000 stay has received ₹2,000 after 2 nights, and a 29 Oct → 2 Nov stay counts 3 nights in October and 1
in November. A monthly guest's rent is spread over the days of their month (₹15,000 for a 30-day month = ₹500 a day).
Only money actually paid counts: a guest who paid a ₹2,000 advance and stayed 5 nights of ₹1,000 has ₹2,000 received
and ₹3,000 balance pending; more counts as they pay. Whatever is still left (extra charges, early check-out) counts on
the check-out day. Paid but not counted yet is "held" (paid by guests staying now, for nights still to come).
Bookings not checked in yet are "yet to receive" in full (advances included).
Received + held + balance pending + yet to receive = the full amount of all bookings.
Profit = received − expenses. Cancelled and transferred stays are left out: whatever was paid on them is treated
as refunded.

Settlements are per month: each month's profit (received − expenses in that month) is shared out, in one go or
in parts, and the page shows what is still left to settle for every month.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q

from .models import Expense, Payment, Room, Settlement, Stay
from .services import today

ZERO = Decimal(0)


@dataclass
class Bucket:
    earned: Decimal = ZERO
    ahead: Decimal = ZERO
    nights: int = 0
    received: Decimal = ZERO  # see received_on()
    expenses: Decimal = ZERO
    settled: Decimal = ZERO  # profit shared out for this month (month buckets only)
    items: list = field(default_factory=list)  # month buckets: Received, one per payment counted in the month

    @property
    def left(self) -> Decimal:
        """Profit not settled yet."""
        return self.profit - self.settled

    @property
    def total(self) -> Decimal:
        return self.earned + self.ahead

    @property
    def profit(self) -> Decimal:
        return self.received - self.expenses

    @property
    def avg_rate(self) -> Decimal:
        return self.total / self.nights if self.nights else ZERO


@dataclass
class Period:
    key: str
    label: str
    start: date | None
    end: date | None  # exclusive


def periods(t: date) -> list[Period]:
    month = t.replace(day=1)
    last_month = (month - timedelta(days=1)).replace(day=1)
    week = t - timedelta(days=t.weekday())
    return [
        Period("week", "This week", week, week + timedelta(days=7)),
        Period("month", "This month", month, _add_months(month, 1)),
        Period("last_month", "Last month", last_month, month),
        Period("year", "This year", t.replace(month=1, day=1), t.replace(year=t.year + 1, month=1, day=1)),
        Period("all", "All time", None, None),
    ]


def month_period(month: date) -> Period:
    return Period(f"{month:%Y-%m}", month.strftime("%B %Y"), month, _add_months(month, 1))


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def _same_day_months_later(d: date, n: int) -> date:
    """14 Oct + 1 → 14 Nov; 31 Jan + 1 → 28/29 Feb."""
    first = _add_months(d, n)
    nxt = _add_months(d, n + 1)
    return first.replace(day=min(d.day, (nxt - timedelta(days=1)).day))


CENT = Decimal("0.01")


@dataclass
class Received:
    """Money that counts as received for the nights start → end of the stay (all in one month), on `day`."""
    day: date
    stay: Stay
    amount: Decimal
    what: str
    start: date
    end: date
    parts: list = field(default_factory=list)  # (night, amount) — so weeks and months get each night's share
    label: str = ""


def night_values(stay: Stay, until: date):
    """Yield (night, value, label) for each night of the stay before `until`: the bill spread evenly over the nights;
    a monthly guest's rent spread over the days of each of their months. Monthly with no rent set: nothing (what they
    paid counts when they leave)."""
    if stay.check_out:
        until = min(until, stay.check_out)
    if stay.kind == Stay.Kind.MONTHLY:
        if not stay.monthly_rent or stay.monthly_rent <= 0:
            return
        k, start = 1, stay.check_in
        while start < until:
            nxt = _same_day_months_later(stay.check_in, k)
            rate = stay.monthly_rent / (nxt - start).days
            d = start
            while d < min(nxt, until):
                yield d, rate, f"Month {k} rent"
                d += timedelta(days=1)
            start, k = nxt, k + 1
    elif stay.check_out and (nights := (stay.check_out - stay.check_in).days) > 0:
        rate = (stay.total_amount or ZERO) / nights
        d = stay.check_in
        while d < until:
            yield d, rate, ""
            d += timedelta(days=1)


def received_on(stay: Stay, t: date) -> list[Received]:
    """The money this stay has paid that counts as received by today (see module doc), one item per month (and per
    rent month). The rest of amount_paid is still held."""
    paid = stay.amount_paid or ZERO
    if stay.status not in (Stay.Status.CHECKED_IN, Stay.Status.CHECKED_OUT) or paid <= 0:
        return []
    done = stay.status == Stay.Status.CHECKED_OUT
    out, cum, counted = [], ZERO, ZERO
    # Nights fully stayed: before today, or every night once checked out.
    for d, rate, label in night_values(stay, (stay.check_out or t) if done else t):
        cum += rate
        part = min(paid, cum.quantize(CENT)) - counted
        if part <= 0:
            break  # all that was paid is counted; the rest is balance pending
        counted += part
        x = out[-1] if out else None
        if not x or x.label != label or (x.day.year, x.day.month) != (d.year, d.month):
            x = Received(d, stay, ZERO, label, d, d, label=label)
            out.append(x)
        x.amount += part
        x.parts.append((d, part))
        x.day = x.end = d
    for x in out:
        n = len(x.parts)
        x.what = f"{x.label} · {n} day{'s' if n > 1 else ''}" if x.label else f"{n} night{'s' if n > 1 else ''}"
    if done and paid > counted:
        end = stay.check_out or t
        start = min(out[-1].end + timedelta(days=1), end) if out else min(stay.check_in, end)
        out.append(Received(end, stay, paid - counted, "Final payment" if out else "Checked out", start, end,
                            parts=[(end, paid - counted)]))
    return out


def months_started(stay: Stay, t: date) -> int:
    """How many of a monthly guest's months have begun by today (joined 1 Oct → 1 in Oct, 2 from 1 Nov)."""
    n = 0
    while _same_day_months_later(stay.check_in, n) <= t:
        n += 1
    return n


def rent_not_added(t: date) -> list[tuple[Stay, int, Decimal]]:
    """Monthly guests staying now whose bill is short of the months started × rent ("Add month's rent" forgotten):
    (stay, months started, amount missing)."""
    out = []
    for s in (Stay.objects.filter(status=Stay.Status.CHECKED_IN, kind=Stay.Kind.MONTHLY, monthly_rent__gt=0)
              .select_related("guest", "room")):
        n = months_started(s, min(t, s.check_out - timedelta(days=1)) if s.check_out else t)
        missing = n * s.monthly_rent - s.total
        if missing > 0:
            out.append((s, n, missing))
    return out


def nightly(stay: Stay, t: date):
    """Yield (night, amount, earned?) for every night of the stay.
    Open (monthly) stays: the rent added so far is spread over the nights up to today."""
    end = stay.check_out or max(t, stay.check_in + timedelta(days=1))
    nights = (end - stay.check_in).days
    if nights <= 0:
        return
    rate = (stay.total_amount or ZERO) / nights
    d = stay.check_in
    while d < end:
        # Earned: every night of a finished stay; for a guest in-house, nights up to and including tonight.
        # An upcoming guest who hasn't arrived has earned nothing yet.
        if stay.status == Stay.Status.CHECKED_OUT:
            earned = True
        elif stay.status == Stay.Status.CHECKED_IN:
            earned = d <= t
        else:
            earned = False
        yield d, rate, earned
        d += timedelta(days=1)


@dataclass
class Report:
    t: date
    period: Period
    in_period: Bucket = field(default_factory=Bucket)
    by_type: dict = field(default_factory=lambda: defaultdict(Bucket))
    by_source: dict = field(default_factory=lambda: defaultdict(Bucket))
    by_month: dict = field(default_factory=lambda: defaultdict(Bucket))
    by_week: dict = field(default_factory=lambda: defaultdict(Bucket))
    ledger: dict = field(default_factory=lambda: defaultdict(Bucket))  # every month: received, expenses, settled
    collected: Decimal = ZERO  # received so far (see received_on)
    held_in_house: Decimal = ZERO  # paid by guests staying now, not counted as received yet
    held: list = field(default_factory=list)  # (stay, amount held)
    monthly_no_rent: list = field(default_factory=list)  # monthly guests staying now with no monthly rent set
    balance_in_house: Decimal = ZERO  # still owed by guests staying now
    due_checked_out: Decimal = ZERO  # still owed by guests who checked out
    due_upcoming: Decimal = ZERO  # full amount of bookings not checked in yet
    advance_upcoming: Decimal = ZERO  # part of that already paid as advance
    deposits_held: Decimal = ZERO
    expenses_all_time: Decimal = ZERO
    unpaid: list = field(default_factory=list)
    room_count: int = 0
    settlements: list = field(default_factory=list)  # newest first

    @property
    def settled_total(self) -> Decimal:
        return sum((x.amount for x in self.settlements), ZERO)

    @property
    def left_to_settle(self) -> Decimal:
        """Profit not shared out yet, over all months that are over (the current month is still running)."""
        month = self.t.replace(day=1)
        return sum((b.left for m, b in self.ledger.items() if m < month), ZERO)

    @property
    def net_cash(self) -> Decimal:
        return self.collected - self.expenses_all_time

    @property
    def to_receive(self) -> Decimal:
        return self.due_upcoming

    @property
    def balance_pending(self) -> Decimal:
        return self.balance_in_house + self.due_checked_out

    @property
    def booked_value(self) -> Decimal:
        return self.collected + self.held_in_house + self.balance_pending + self.to_receive

    # ── Overall page (all months together) ──

    @property
    def paid_total(self) -> Decimal:
        """Every rupee guests have paid on live bookings: received + held + advances for upcoming stays."""
        return self.collected + self.held_in_house + self.advance_upcoming

    @property
    def upcoming_to_pay(self) -> Decimal:
        """Upcoming bookings: the part not paid as advance."""
        return self.due_upcoming - self.advance_upcoming

    @property
    def still_to_receive(self) -> Decimal:
        return self.balance_pending + self.upcoming_to_pay

    @property
    def cash_in_hand(self) -> Decimal:
        """Everything guests paid, minus expenses and what was shared out (advances and held money included)."""
        return self.paid_total - self.expenses_all_time - self.settled_total

    @property
    def paid_pct(self) -> int:
        return round(self.paid_total / self.booked_value * 100) if self.booked_value > 0 else 0

    @property
    def unsettled_profit(self) -> Decimal:
        """All-time profit (received − expenses) not shared out yet."""
        return self.net_cash - self.settled_total

    @property
    def collected_pct(self) -> int:
        return round(self.collected / self.booked_value * 100) if self.booked_value > 0 else 0

    def months(self) -> list[tuple[date, Bucket]]:
        """Every month from the first one with money in it up to this one, newest first."""
        month = self.t.replace(day=1)
        first = min((m for m in self.ledger if m <= month), default=month)
        out = []
        while month >= first:
            out.append((month, self.ledger[month]))
            month = _add_months(month, -1)
        return out

    @property
    def occupancy(self) -> int | None:
        """Room-nights sold ÷ room-nights available in the period."""
        if not self.period.start or not self.room_count:
            return None
        days = (self.period.end - self.period.start).days
        return round(self.in_period.nights / (days * self.room_count) * 100)


def build(period_key: str = "month", month: date | None = None) -> Report:
    """Totals for one period: a named one ("month", "all", …) or, with `month`, that calendar month."""
    t = today()
    period = month_period(month) if month else next((p for p in periods(t) if p.key == period_key), periods(t)[1])
    r = Report(t=t, period=period, room_count=Room.objects.filter(is_active=True, status=Room.Status.ACTIVE).count())
    # Charts: the last 12 months / weeks plus the next 2 (booked ahead).
    month0, month_end = _add_months(t.replace(day=1), -11), _add_months(t.replace(day=1), 3)
    week0 = t - timedelta(days=t.weekday() + 7 * 11)
    week_end = week0 + timedelta(days=7 * 14)

    def add_received(d, amount):
        buckets = []
        if month0 <= d < month_end:
            buckets.append(r.by_month[d.replace(day=1)])
        if week0 <= d < week_end:
            buckets.append(r.by_week[d - timedelta(days=d.weekday())])
        if (period.start is None or d >= period.start) and (period.end is None or d < period.end):
            buckets.append(r.in_period)
        buckets.append(r.ledger[d.replace(day=1)])
        for b in buckets:
            b.received += amount

    stays = Stay.objects.live().select_related("guest", "room", "room__room_type")
    for s in stays:
        for d, amount, earned in nightly(s, t):
            buckets = []
            if month0 <= d < month_end:
                buckets.append(r.by_month[d.replace(day=1)])
            if week0 <= d < week_end:
                buckets.append(r.by_week[d - timedelta(days=d.weekday())])
            if (period.start is None or d >= period.start) and (period.end is None or d < period.end):
                buckets += [r.in_period, r.by_type[s.room.room_type.name], r.by_source[s.get_source_display()]]
            for b in buckets:
                b.nights += 1
                if earned:
                    b.earned += amount
                else:
                    b.ahead += amount

        paid, due = s.amount_paid or ZERO, max(s.balance, ZERO)

        if s.status == Stay.Status.UPCOMING:
            # Check-in hasn't happened: the whole booking is still to come, even if an advance was paid.
            r.due_upcoming += max(s.total, paid)
            r.advance_upcoming += paid
            r.deposits_held += s.deposit_amount or ZERO
        else:
            got = ZERO
            for x in received_on(s, t):
                for d, amount in x.parts:
                    add_received(d, amount)
                r.ledger[x.day.replace(day=1)].items.append(x)
                got += x.amount
            r.collected += got
            if s.status == Stay.Status.CHECKED_IN:
                r.held_in_house += paid - got
                if paid > got:
                    r.held.append((s, paid - got))
                if s.kind == Stay.Kind.MONTHLY and not s.monthly_rent:
                    r.monthly_no_rent.append(s)
                r.balance_in_house += due
                r.deposits_held += s.deposit_amount or ZERO
            else:
                r.due_checked_out += due
        if due > 0 and s.status != Stay.Status.UPCOMING:
            r.unpaid.append(s)

    r.unpaid.sort(key=lambda s: (s.status != Stay.Status.CHECKED_OUT, -s.balance))

    for d, amount in Expense.objects.values_list("date", "amount"):
        r.expenses_all_time += amount
        r.ledger[d.replace(day=1)].expenses += amount
        if month0 <= d < month_end:
            r.by_month[d.replace(day=1)].expenses += amount
        if week0 <= d < week_end:
            r.by_week[d - timedelta(days=d.weekday())].expenses += amount
        if (period.start is None or d >= period.start) and (period.end is None or d < period.end):
            r.in_period.expenses += amount
    for b in r.ledger.values():
        b.items.sort(key=lambda x: x.day, reverse=True)
    r.held.sort(key=lambda x: -x[1])
    r.settlements = list(Settlement.objects.all())
    for x in r.settlements:
        r.ledger[x.month].settled += x.amount
    return r


# ── Chart geometry (plain HTML/CSS bars, so labels stay readable on phones) ──


def _nice_max(v: Decimal) -> int:
    """Axis top = 4 × a clean step (1, 2, 2.5 or 5 × 10ⁿ), so every tick is a round number."""
    quarter = float(v) / 4
    if quarter <= 0:
        return 1000
    mag = 10 ** (len(str(int(quarter))) - 1)
    step = next((s * mag for s in (1, 2, 2.5, 5) if quarter <= s * mag), 10 * mag)
    return int(step * 4)


def column_chart(points: list[tuple[str, str, Bucket, bool]]) -> dict:
    """points: (short label, full label, bucket, is_current) → columns with % heights and 4 y-ticks."""
    top = _nice_max(max((b.total for _, _, b, _ in points), default=ZERO))
    cols = [{
        "label": label, "full": full, "b": b, "current": current,
        "pct": float(b.total) / top * 100,
        "ahead_share": float(b.ahead / b.total * 100) if b.total else 0,  # of the bar's height
    } for label, full, b, current in points]
    ticks = [{"value": top * i // 4, "pct": i * 25} for i in range(4, -1, -1)]
    return {"cols": cols, "ticks": ticks, "has_ahead": any(b.ahead for _, _, b, _ in points)}


def monthly_chart(r: Report) -> dict:
    m, months = _add_months(r.t.replace(day=1), -11), []
    while m <= _add_months(r.t.replace(day=1), 2):
        months.append((m.strftime("%b"), m.strftime("%B %Y"), r.by_month[m], m == r.t.replace(day=1)))
        m = _add_months(m, 1)
    return column_chart(months)


def weekly_chart(r: Report) -> dict:
    this_week = r.t - timedelta(days=r.t.weekday())
    weeks = []
    for i in range(-11, 3):
        w = this_week + timedelta(days=7 * i)
        weeks.append((f"{w.day}/{w.month}", f"Week of {w:%d %b %Y} – {w + timedelta(days=6):%d %b}", r.by_week[w], i == 0))
    return column_chart(weeks)


def bar_rows(groups: dict) -> list[dict]:
    rows = sorted(groups.items(), key=lambda kv: -kv[1].total)
    top = max((b.total for _, b in rows), default=ZERO) or Decimal(1)
    return [{"name": n, "b": b, "earned_pct": float(b.earned / top * 100), "ahead_pct": float(b.ahead / top * 100)}
            for n, b in rows]


def rooms_for_month(r: Report, month: date) -> list[dict]:
    """Per room for one month: money received in it (nights stayed and paid for), and what is still due in it from
    guests not checked out yet — their nights in this month (stayed or to come) not counted as received yet."""
    next_month = _add_months(month, 1)
    rows = {}

    def row(room):
        return rows.setdefault(room.pk, {"room": room, "received": ZERO, "items": [], "due": [], "due_total": ZERO})

    for room in Room.objects.filter(is_active=True):
        row(room)
    got = defaultdict(Decimal)
    for item in r.ledger[month].items:
        x = row(item.stay.room)
        x["received"] += item.amount
        x["items"].append(item)
        got[item.stay.pk] += item.amount

    stays = (Stay.objects.filter(status__in=[Stay.Status.UPCOMING, Stay.Status.CHECKED_IN], check_in__lt=next_month)
             .filter(Q(check_out__gt=month) | Q(check_out__isnull=True)).select_related("guest", "room"))
    for s in stays:
        nights = [(d, v) for d, v, _ in night_values(s, next_month) if d >= month]
        due = sum((v for _, v in nights), ZERO).quantize(CENT) - got[s.pk]
        if due > 0:
            x = row(s.room)
            n = len(nights)
            x["due"].append({"stay": s, "day": nights[0][0], "amount": due,
                             "what": f"{n} night{'s' if n > 1 else ''} in {month:%b}"})
            x["due_total"] += due
    for x in rows.values():
        x["due"].sort(key=lambda d: d["day"])
    return sorted(rows.values(), key=lambda x: (x["room"].sort_order, x["room"].floor, x["room"].number))


def collected(start: date | None = None, end: date | None = None) -> dict:
    """Money actually collected, by payment date (cash flow — not the "received" rule above): total, per mode and per
    person who collected it. Payments on cancelled / transferred stays are left out (treated as refunded)."""
    pays = Payment.objects.exclude(stay__status__in=Stay.VOID_STATUSES)
    if start:
        pays = pays.filter(date__gte=start)
    if end:
        pays = pays.filter(date__lt=end)
    modes = dict(Stay.PaymentMode.choices)
    by_mode, by_person, total = defaultdict(Decimal), defaultdict(Decimal), ZERO
    for mode, person, amount in pays.values_list("mode", "collected_by", "amount"):
        by_mode[modes.get(mode, "Not given")] += amount
        by_person[person or "Not given"] += amount
        total += amount
    order = lambda d: sorted(d.items(), key=lambda kv: -kv[1])  # noqa: E731
    return {"total": total, "by_mode": order(by_mode), "by_person": order(by_person)}
