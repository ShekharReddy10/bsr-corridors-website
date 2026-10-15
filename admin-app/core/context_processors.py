from functools import cache

from django.conf import settings

from .models import MessageTemplate


def app(request):
    # Callable (and cached per request), so the query runs only on pages that show a Check-in button.
    @cache
    def wa_welcome():
        return MessageTemplate.objects.filter(on_check_in=True).exists()

    return {"HOTEL_NAME": settings.HOTEL_NAME, "wa_welcome": wa_welcome}
