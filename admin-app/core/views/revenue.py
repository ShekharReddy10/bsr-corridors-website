"""Revenue page, behind a second password (separate from the login).

The password is stored only as a hash in AppSetting — never in the code (the repo is public).
The first visit asks to set it. If REVENUE_PASSWORD is set in the environment it wins, so a
forgotten password can be reset from Render's settings (the free plan has no shell).
"""

import hmac
import os
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .. import revenue
from ..models import AppSetting, AuditLog
from .auth import _client_ip

HASH_KEY = "revenue_password_hash"
SESSION_KEY = "revenue_unlocked_until"
UNLOCK_SECONDS = 30 * 60
MIN_LENGTH = 6


def _has_password() -> bool:
    return bool(os.environ.get("REVENUE_PASSWORD") or AppSetting.get(HASH_KEY))


def _password_ok(password: str) -> bool:
    env = os.environ.get("REVENUE_PASSWORD", "")
    if env:
        return hmac.compare_digest(password.encode(), env.encode())
    stored = AppSetting.get(HASH_KEY)
    return bool(stored) and check_password(password, stored)


def _unlocked(request) -> bool:
    return request.session.get(SESSION_KEY, 0) > time.time()


def _fail_key(request) -> str:
    return f"revenue-fail:{_client_ip(request)}"


def revenue_page(request):
    has_password = _has_password()
    if request.method == "POST":
        password = request.POST.get("password", "")
        if not has_password:
            if len(password) < MIN_LENGTH:
                messages.error(request, f"Use at least {MIN_LENGTH} characters.")
            elif password != request.POST.get("confirm", ""):
                messages.error(request, "The two passwords don’t match.")
            else:
                AppSetting.put(HASH_KEY, make_password(password))
                AuditLog.record("revenue_password", "Revenue password set")
                request.session[SESSION_KEY] = time.time() + UNLOCK_SECONDS
                messages.success(request, "Revenue password set.")
            return redirect("revenue")
        key = _fail_key(request)
        if cache.get(key, 0) >= settings.LOGIN_MAX_ATTEMPTS:
            messages.error(request, f"Too many wrong passwords. Try again in {settings.LOGIN_LOCKOUT_SECONDS // 60} minutes.")
        elif _password_ok(password):
            cache.delete(key)
            request.session[SESSION_KEY] = time.time() + UNLOCK_SECONDS
            return redirect(request.get_full_path())
        else:
            cache.set(key, cache.get(key, 0) + 1, settings.LOGIN_LOCKOUT_SECONDS)
            messages.error(request, "Wrong password.")
        return redirect(request.get_full_path())

    if not _unlocked(request):
        return render(request, "core/revenue_lock.html", {"has_password": has_password, "min_length": MIN_LENGTH})

    r = revenue.build(request.GET.get("period", "month"))
    return render(request, "core/revenue.html", {
        "r": r,
        "periods": revenue.periods(r.t),
        "monthly": revenue.monthly_chart(r),
        "weekly": revenue.weekly_chart(r),
        "types": revenue.bar_rows(r.by_type),
        "sources": revenue.bar_rows(r.by_source),
        "env_password": bool(os.environ.get("REVENUE_PASSWORD")),
    })


@require_POST
def revenue_lock(request):
    request.session.pop(SESSION_KEY, None)
    return redirect("dashboard")


@require_POST
def revenue_change_password(request):
    if not _unlocked(request):
        return redirect("revenue")
    new = request.POST.get("new_password", "")
    if not _password_ok(request.POST.get("current_password", "")):
        messages.error(request, "Current password is wrong.")
    elif len(new) < MIN_LENGTH:
        messages.error(request, f"Use at least {MIN_LENGTH} characters.")
    elif new != request.POST.get("confirm", ""):
        messages.error(request, "The two new passwords don’t match.")
    else:
        AppSetting.put(HASH_KEY, make_password(new))
        AuditLog.record("revenue_password", "Revenue password changed")
        messages.success(request, "Revenue password changed.")
    return redirect("revenue")
