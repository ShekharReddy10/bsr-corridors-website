from functools import cache

from django.conf import settings

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

    return {"HOTEL_NAME": settings.HOTEL_NAME, "wa_welcome": wa_welcome, "wa_departure": wa_departure,
            "wa_buttons": wa_buttons}
