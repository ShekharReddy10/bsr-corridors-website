"""Seeing a property's money: unlock (money password + the person's own authenticator code), lock, and the Money access
page where the property's owner decides who may see it. The rules are in cluster/money.py."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from cluster import money
from cluster.current import local
from cluster.models import Activity, Membership, MoneyAuth

from ..models import AppSetting, AuditLog
from .auth import _client_ip
from .revenue import HASH_KEY, MIN_LENGTH, _has_password, _password_ok

R = Membership.Role


def _name(user) -> str:
    return user.get_full_name() or user.username


def _next(request) -> str:
    nxt = request.POST.get("next") or request.GET.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return local("/pg/revenue/" if request.item.kind != "hotel" else "/revenue/")


def unlock(request):
    """Ask for the property's money password and the person's authenticator code (setting the authenticator up the
    first time — and the money password, if the property has none yet and they are its owner)."""
    item, user = request.item, request.user
    if not money.has_access(user, item):
        return render(request, "core/money_unlock.html", {"no_access": True, "may_manage": money.may_manage(user, item)})
    if money.unlocked(request):
        return redirect(_next(request))
    auth = money.auth_for(user, item, create=True)
    setting_up = not auth.ready
    if setting_up and not auth.secret:
        auth.secret = money.new_secret()
        auth.save(update_fields=["secret"])
    has_password = _has_password()
    m = money.membership(user, item)
    may_set_password = not has_password and (m.role == R.OWNER or money.may_manage(user, item))
    if request.method == "POST":
        fail_key = f"money-fail:{_client_ip(request)}:{item.code}:{user.pk}"
        password, code = request.POST.get("password", ""), request.POST.get("code", "")
        problem = ""
        if cache.get(fail_key, 0) >= settings.LOGIN_MAX_ATTEMPTS:
            problem = f"Too many wrong tries. Try again in {settings.LOGIN_LOCKOUT_SECONDS // 60} minutes."
        elif not has_password and not may_set_password:
            problem = "This property has no money password yet — its owner sets it first."
        elif not has_password and (len(password) < MIN_LENGTH or password != request.POST.get("confirm", "")):
            problem = f"Choose a money password of at least {MIN_LENGTH} characters, typed the same twice."
        elif has_password and not _password_ok(password):
            problem = "Wrong money password or code."
        elif not money.check_code(auth, code):
            problem = "Wrong money password or code." if has_password else "That code isn’t right — try the current one."
        if problem:
            cache.set(fail_key, cache.get(fail_key, 0) + 1, settings.LOGIN_LOCKOUT_SECONDS)
            messages.error(request, problem)
            return redirect(request.get_full_path())
        cache.delete(fail_key)
        if not has_password:
            AppSetting.put(HASH_KEY, make_password(password))
            AuditLog.record("revenue_password", f"Money password set by {_name(user)} (owner)")
        if setting_up:
            auth.confirmed_at, auth.reset_by, auth.reset_at = timezone.now(), "", None
            auth.save(update_fields=["confirmed_at", "reset_by", "reset_at"])
            AuditLog.record("money_access", f"{_name(user)} set up their authenticator for the money pages")
        money.unlock(request, item)
        AuditLog.record("money_unlock", f"Amounts opened by {_name(user)} (for {money.UNLOCK_SECONDS // 3600} hours)")
        return redirect(_next(request))
    uri = money.setup_uri(auth) if setting_up else ""
    return render(request, "core/money_unlock.html", {
        "setting_up": setting_up, "has_password": has_password, "may_set_password": may_set_password,
        "qr": money.qr_svg(uri) if uri else "", "secret": " ".join(auth.secret[i:i + 4] for i in range(0, len(auth.secret), 4)),
        "reset": auth if auth.reset_at else None, "min_length": MIN_LENGTH, "next": _next(request),
        "hours": money.UNLOCK_SECONDS // 3600,
    })


@require_POST
def lock(request):
    money.lock(request, request.item)
    messages.info(request, "Amounts hidden again.")
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(local("/"))


def access(request):
    """Who may see this property's money (and who its owners are). Changed by an owner with their authenticator
    code; before the property has such an owner, by the main admin (to name the first one)."""
    item, user = request.item, request.user
    manage = money.may_manage(user, item)
    if not (manage or user.is_superuser or money.has_access(user, item)):
        messages.error(request, "Only the property’s owners can see who has money access.")
        return redirect(local("/"))
    controlled = money.controlled(item)
    members = list(Membership.objects.filter(item=item).select_related("user").order_by("user__username"))
    auths = {a.user_id: a for a in MoneyAuth.objects.filter(item=item)}
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "reset-auth":  # the main admin only (decided with the user): logged, and the person is told
            if not user.is_superuser:
                messages.error(request, "Only the main admin can reset an authenticator.")
                return redirect("money_access")
            a = get_object_or_404(MoneyAuth, pk=request.POST.get("auth"), item=item)
            money.reset(a, _name(user))
            AuditLog.record("money_access", f"Authenticator of {_name(a.user)} reset by the main admin ({_name(user)})")
            Activity.record("access", f"{item.name}: authenticator of {_name(a.user)} reset", item)
            messages.success(request, f"{_name(a.user)}’s authenticator is reset — they set it up again at their next "
                                      "unlock. This is in the property’s log.")
            return redirect("money_access")
        if not manage:
            messages.error(request, "Only an owner of this property (with their authenticator) can change this.")
            return redirect("money_access")
        if controlled:
            mine = money.auth_for(user, item)
            if mine is None or not money.check_code(mine, request.POST.get("code", "")):
                messages.error(request, "Confirm with the current code from your authenticator app.")
                return redirect("money_access")
        changes = []
        if action == "add-me" and user.is_superuser and not controlled:
            m, _ = Membership.objects.update_or_create(user=user, item=item, defaults={"role": R.OWNER})
            m.grant("money", by=_name(user))
            changes.append(f"{_name(user)}: owner with money access")
        else:
            plan = []
            for m in members:
                owner = request.POST.get(f"owner-{m.pk}") == "1"
                has_money = request.POST.get(f"money-{m.pk}") == "1" or owner  # owners always see the money
                role = R.OWNER if owner else (R.MANAGER if m.role == R.OWNER else m.role)
                if (role, has_money) != (m.role, m.money_access):
                    changes.append(f"{_name(m.user)}: {'owner' if owner else m.get_role_display().lower()}"
                                   f"{', money access' if has_money else ', no money access'}")
                plan.append((m, role, has_money))
            ready = {a.user_id for a in auths.values() if a.ready}
            if controlled and not any(role == R.OWNER and has_money and m.user_id in ready for m, role, has_money in plan):
                messages.error(request, "Keep at least one owner with money access and an authenticator.")
                return redirect("money_access")
            for m, role, has_money in plan:
                if m.role != role:
                    m.role = role
                    m.save(update_fields=["role"])
                m.grant("money", has_money, by=_name(user))
        if changes:
            AuditLog.record("money_access", f"Money access changed by {_name(user)}: {'; '.join(changes)}")
            Activity.record("access", f"{item.name}: money access — {'; '.join(changes)}", item)
            messages.success(request, "Money access saved.")
        else:
            messages.info(request, "Nothing changed.")
        return redirect("money_access")
    rows = [{"m": m, "auth": auths.get(m.user_id)} for m in members]
    return render(request, "core/money_access.html", {
        "rows": rows, "manage": manage, "controlled": controlled, "is_admin": user.is_superuser,
        "admin_member": any(m.user_id == user.pk for m in members),
    })
