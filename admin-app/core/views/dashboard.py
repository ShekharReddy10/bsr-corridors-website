from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import checks, revenue
from ..models import AuditLog, BackupRun, Room, Stay
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
        s for s in live.filter(Q(check_out__gte=t - timedelta(days=365)) | Q(check_out__isnull=True))
        .exclude(status=Stay.Status.UPCOMING, check_in__gt=t)
        if s.balance > 0
    ]
    arrivals = list(live.filter(status=Stay.Status.UPCOMING, check_in=t).order_by("room__number"))
    late_arrivals = list(live.filter(status=Stay.Status.UPCOMING, check_in__lt=t, check_out__gt=t).order_by("check_in"))
    in_house = list(in_house)
    departures = [s for s in in_house if s.check_out == t]
    overdue_departures = [s for s in in_house if s.check_out and s.check_out < t]
    day_after = t + timedelta(days=2)
    upcoming = live.filter(status=Stay.Status.UPCOMING)
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
        "day_after": day_after,
        "arriving_tomorrow": list(upcoming.filter(check_in=tomorrow).order_by("room__number")),
        "arriving_day_after": list(upcoming.filter(check_in=day_after).order_by("room__number")),
        "upcoming_count": upcoming.filter(check_in__gte=t).count(),
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
        # Money that could slip through: monthly rent not added to the bill, and bookings whose dates have passed
        # without a check-in (their advance sits in "yet to receive" until they're checked in or cancelled).
        "rent_due": revenue.rent_not_added(t),
        "flags": checks.stay_flags(t),
        "missed": list(upcoming.filter(check_out__lte=t).order_by("check_in")),
        "last_backup": BackupRun.objects.filter(kind=BackupRun.Kind.BACKUP).first(),
    }
    return render(request, "core/dashboard.html", context)


@require_POST
def confirm_check(request):
    """ "Looks right" on a double-check warning: don't ask again unless the figures change."""
    key = request.POST.get("key", "")
    if key:
        checks.confirm(key)
        AuditLog.record("check", f"Marked as correct: {request.POST.get('title', key)}")
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect("dashboard")
