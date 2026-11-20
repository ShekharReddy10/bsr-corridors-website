"""BSR Corridors becomes the first cluster item. Its data stays exactly where it is (is_default: main database,
site root). Everyone who could sign in before keeps doing everything they could (Owner, all switches on)."""

from django.conf import settings
from django.db import migrations


def forwards(apps, schema_editor):
    Item = apps.get_model("cluster", "Item")
    Membership = apps.get_model("cluster", "Membership")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    item, _ = Item.objects.get_or_create(is_default=True, defaults={
        "code": "bsrc", "name": settings.HOTEL_NAME, "kind": "hotel", "theme": "classic"})
    for user in User.objects.filter(is_superuser=False):
        Membership.objects.get_or_create(user=user, item=item, defaults={
            "role": "owner", "money_access": True, "see_full_ids": True, "can_delete": True})


def backwards(apps, schema_editor):
    apps.get_model("cluster", "Item").objects.filter(is_default=True).delete()


class Migration(migrations.Migration):
    dependencies = [("cluster", "0001_initial"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.RunPython(forwards, backwards)]
