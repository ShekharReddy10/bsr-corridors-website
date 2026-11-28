"""Helpers for tests: open a property's money the way a person does (money access, their authenticator set up, the
money password set, unlocked)."""

import time

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.utils import timezone

from . import current, money
from .models import Item, Membership, MoneyAuth


def open_money(client, item=None, password: str = "secret1", user=None, owner: bool = True):
    """The signed-in person (or `user`) gets money access at `item` (default BSR Corridors) and has it unlocked."""
    from core.models import AppSetting
    from core.views.revenue import HASH_KEY
    item = item or Item.objects.get(is_default=True)
    user = user or get_user_model().objects.get(pk=client.session["_auth_user_id"])
    m, _ = Membership.objects.update_or_create(user=user, item=item, defaults={
        "role": Membership.Role.OWNER if owner else Membership.Role.ACCOUNTANT})
    m.grant("money")
    MoneyAuth.objects.update_or_create(user=user, item=item, defaults={
        "secret": money.new_secret(), "confirmed_at": timezone.now()})
    with current.using(item):
        if not AppSetting.get(HASH_KEY):
            AppSetting.put(HASH_KEY, make_password(password))
    session = client.session
    session[f"money_unlocked:{item.code}"] = time.time() + money.UNLOCK_SECONDS
    session.save()
    return user


def code_now(user, item) -> str:
    auth = MoneyAuth.objects.get(user=user, item=item)
    return money.code_at(auth.secret, money.now_step())
