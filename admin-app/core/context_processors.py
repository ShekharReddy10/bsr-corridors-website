from django.conf import settings


def app(request):
    return {"HOTEL_NAME": settings.HOTEL_NAME}
