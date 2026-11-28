from functools import cache

from django.conf import settings

from cluster.current import name as hotel_name

from .models import MessageTemplate


def app(request):
    # Callables (cached per request), so the queries run only on pages with a Check-in / Check-out button.
    @cache
    def wa_welcome():
        return MessageTemplate.objects.filter(on_check_in=True).exists()

    @cache
    def wa_departure():
        return MessageTemplate.objects.filter(on_check_out=True).exists()

    @cache
    def wa_buttons():
        """Messages shown as one-tap buttons in the Today page lists: {"departing": template, "balance": …}."""
        return {t.shortcut: t for t in MessageTemplate.objects.exclude(shortcut="")}

    @cache
    def my_items():
        """For the switcher in the header (S19): the other cluster items this person may open."""
        from cluster.views import items_for
        return [i for i in items_for(request.user) if not i.archived] if request.user.is_authenticated else []

    @cache
    def open_complaints():
        """PG / co-living menu: how many complaints are open."""
        from pg.models import Complaint
        return Complaint.objects.exclude(status=Complaint.Status.FIXED).count()

    item = getattr(request, "item", None)
    from cluster.branding import logo_url

    @cache
    def money_until():
        from cluster import money
        return money.open_until(request, item) if item is not None else None

    return {"item_logo": logo_url(item), "money_until": money_until,"HOTEL_NAME": hotel_name(), "wa_welcome": wa_welcome, "wa_departure": wa_departure,
            "open_complaints": open_complaints if item is not None and item.kind != "hotel" else 0,
            "wa_buttons": wa_buttons, "item": item, "caps": getattr(request, "caps", set()),
            "theme": item.colors if item else None, "my_items": my_items, "CLUSTER_NAME": settings.CLUSTER_NAME}
