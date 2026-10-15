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

    return {"HOTEL_NAME": settings.HOTEL_NAME, "wa_welcome": wa_welcome, "wa_departure": wa_departure}
