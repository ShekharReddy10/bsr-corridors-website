"""Cluster pages: the home page (one card per cluster item the user may open) and each item's theme."""

import logging
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from . import current
from .models import THEMES, Item

log = logging.getLogger(__name__)


def admin_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            messages.error(request, "Only the main admin can do that.")
            return redirect("cluster_home")
        return view(request, *args, **kwargs)
    return wrapper


def items_for(user):
    """The cluster items this person may open (the main admin: all, archived ones last)."""
    qs = Item.objects.all() if user.is_superuser else Item.objects.filter(memberships__user=user, archived=False)
    return list(qs.order_by("archived", "sort_order", "name"))


def snapshot(item) -> dict:
    """Today at a glance, no money (decision D6): rooms taken tonight, arrivals, departures, things to check."""
    from core import payments, services
    from core.models import Room, Stay
    try:
        with current.using(item):
            t = services.today()
            live = Stay.objects.live()
            rooms = Room.objects.filter(is_active=True).count()
            taken = live.overlapping(t, t + timedelta(days=1)).values("room_id").distinct().count()
            return {
                "units": "rooms", "taken": taken, "total": rooms,
                "arriving": live.filter(status=Stay.Status.UPCOMING, check_in=t).count(),
                "departing": live.filter(status=Stay.Status.CHECKED_IN, check_out=t).count(),
                "verify": payments.to_verify().count(),
            }
    except Exception:  # its database can't be reached or isn't up to date: the card says so, others still show
        log.exception("Snapshot failed for %s", item.code)
        return {"error": True}


def home(request):
    items = items_for(request.user)
    if not request.user.is_superuser and len(items) == 1 and request.GET.get("all") is None:
        return redirect(items[0].prefix)  # only one: straight in (S1)
    cards = [{"item": i, "snap": snapshot(i) if not i.archived else None} for i in items]
    return render(request, "cluster/home.html", {"cards": cards, "cluster_name": settings.CLUSTER_NAME})


@admin_only
def theme(request, code):
    item = get_object_or_404(Item, code=code)
    if request.method == "POST":
        chosen = request.POST.get("theme", "")
        if chosen in THEMES:
            item.theme = chosen
            item.save(update_fields=["theme"])
            messages.success(request, f"{item.name}: theme changed to {THEMES[chosen][0]}.")
            return redirect("cluster_home")
        messages.error(request, "Pick one of the themes.")
    return render(request, "cluster/theme.html", {
        "obj": item, "themes": [(k, *v) for k, v in THEMES.items()], "cluster_name": settings.CLUSTER_NAME,
    })

