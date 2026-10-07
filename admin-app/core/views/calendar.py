import calendar as pycal
from datetime import date, timedelta

from django.shortcuts import get_object_or_404, render
from django.utils.dateparse import parse_date

from ..models import Room, RoomType, Stay
from ..services import today

RANGES = {"7": 7, "14": 14, "31": 31}


def _bar_class(stay: Stay) -> str:
    classes = [f"bar-{stay.status}"]
    if stay.balance > 0 and stay.status != Stay.Status.UPCOMING:
        classes.append("bar-unpaid")
    return " ".join(classes)


def tape_chart(request):
    """Rooms (rows) × dates (columns) with stays drawn as bars."""
    t = today()
    days = RANGES.get(request.GET.get("days", ""), 14)
    start = parse_date(request.GET.get("start") or "") or (t - timedelta(days=1))
    end = start + timedelta(days=days)
    type_id = request.GET.get("type", "")

    rooms = Room.objects.filter(is_active=True).select_related("room_type")
    if type_id.isdigit():
        rooms = rooms.filter(room_type_id=int(type_id))
    rooms = list(rooms)

    stays = (
        Stay.objects.live()
        .overlapping(start, end)
        .filter(room__in=rooms)
        .select_related("guest")
        .order_by("check_in")
    )
    bars: dict[int, list] = {r.pk: [] for r in rooms}
    for s in stays:
        first, last = max(s.check_in, start), min(s.check_out, end)
        bars[s.room_id].append(
            {
                "stay": s,
                "col": (first - start).days + 2,  # column 1 is the room label
                "span": (last - first).days,
                "cls": _bar_class(s),
                "cut_left": s.check_in < start,
                "cut_right": s.check_out > end,
            }
        )

    day_list = [
        {"date": d, "is_today": d == t, "is_weekend": d.weekday() >= 5, "col": i + 2}
        for i, d in enumerate(start + timedelta(days=n) for n in range(days))
    ]
    context = {
        "days": day_list,
        "day_count": days,
        "rows": [{"room": r, "bars": bars[r.pk]} for r in rooms],
        "start": start,
        "prev": start - timedelta(days=days),
        "next": start + timedelta(days=days),
        "today": t,
        "range": str(days),
        "ranges": RANGES,
        "types": RoomType.objects.filter(is_active=True),
        "type_id": type_id,
    }
    return render(request, "core/calendar.html", context)


def room_month(request, pk):
    """One room, one month — a phone-friendly month calendar."""
    room = get_object_or_404(Room.objects.select_related("room_type"), pk=pk)
    t = today()
    try:
        y, m = (int(x) for x in request.GET.get("month", "").split("-"))
        first = date(y, m, 1)
    except ValueError:
        first = t.replace(day=1)
    weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    grid_start, grid_end = weeks[0][0], weeks[-1][-1] + timedelta(days=1)
    stays = list(Stay.objects.live().overlapping(grid_start, grid_end).filter(room=room).select_related("guest"))

    def stay_on(d):
        return next((s for s in stays if s.check_in <= d < s.check_out), None)

    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    context = {
        "room": room,
        "month": first,
        "weeks": [
            [{"date": d, "in_month": d.month == first.month, "is_today": d == t, "stay": stay_on(d),
              "starts": (s := stay_on(d)) is not None and s.check_in == d} for d in week]
            for week in weeks
        ],
        "prev": prev_month.strftime("%Y-%m"),
        "next": next_month.strftime("%Y-%m"),
        "rooms": Room.objects.filter(is_active=True),
        "month_stays": [s for s in stays if s.check_in < next_month and s.check_out > first],
    }
    return render(request, "core/room_month.html", context)
