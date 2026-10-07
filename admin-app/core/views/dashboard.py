from datetime import timedelta
from decimal import Decimal

from django.shortcuts import render

from ..models import BackupRun, Room, Stay
from ..services import today


def dashboard(request):
    t = today()
    live = Stay.objects.live().select_related("guest", "room", "room__room_type")
    in_house = live.filter(status=Stay.Status.CHECKED_IN).order_by("check_out", "room__number")
    tomorrow = t + timedelta(days=1)
    occupied = set(live.overlapping(t, tomorrow).values_list("room_id", flat=True))
    occupied_tomorrow = set(live.overlapping(tomorrow, tomorrow + timedelta(days=1)).values_list("room_id", flat=True))
    rooms = list(Room.objects.filter(is_active=True).select_related("room_type"))

    unpaid = [
        s for s in live.filter(check_out__gte=t - timedelta(days=365)).exclude(status=Stay.Status.UPCOMING, check_in__gt=t)
        if s.balance > 0
    ]
    arrivals = list(live.filter(status=Stay.Status.UPCOMING, check_in=t).order_by("room__number"))
    late_arrivals = list(live.filter(status=Stay.Status.UPCOMING, check_in__lt=t, check_out__gt=t).order_by("check_in"))
    in_house = list(in_house)
    departures = [s for s in in_house if s.check_out == t]
    overdue_departures = [s for s in in_house if s.check_out < t]
    context = {
        "today": t,
        "arrivals": arrivals,
        "late_arrivals": late_arrivals,
        "arriving_count": len(arrivals) + len(late_arrivals),
        "in_house": in_house,
        "departures": departures,
        "overdue_departures": overdue_departures,
        "departing_count": len(departures) + len(overdue_departures),
        "vacant": [r for r in rooms if r.pk not in occupied and r.status == Room.Status.ACTIVE],
        "tomorrow": tomorrow,
        "departing_tomorrow": list(
            live.filter(status__in=[Stay.Status.UPCOMING, Stay.Status.CHECKED_IN], check_out=tomorrow).order_by("room__number")
        ),
        "vacant_tomorrow": [r for r in rooms if r.pk not in occupied_tomorrow and r.status == Room.Status.ACTIVE],
        "maintenance": [r for r in rooms if r.status == Room.Status.MAINTENANCE],
        "occupied_count": len(occupied),
        "room_count": len(rooms),
        "unpaid": sorted(unpaid, key=lambda s: s.check_in),
        "unpaid_total": sum((s.balance for s in unpaid), Decimal(0)),
        "form_c_due": [s for s in in_house if s.form_c_due],
        "last_backup": BackupRun.objects.filter(kind=BackupRun.Kind.BACKUP).first(),
    }
    return render(request, "core/dashboard.html", context)
