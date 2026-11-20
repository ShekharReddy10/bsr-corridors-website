"""Logins: one per person for the whole cluster (Cluster → Users, admin only), with a role at each cluster item, and
"Change my password" for everyone. Money pages still need the item's money password on top of the login."""

from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.shortcuts import get_object_or_404, redirect, render

from cluster.models import Item, Membership

from ..models import AuditLog

MIN_LENGTH = 10


def admin_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            messages.error(request, "Only the main admin can manage logins.")
            return redirect("cluster_home")
        return view(request, *args, **kwargs)
    return wrapper


def _password_problem(password: str, confirm: str) -> str:
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters for the password."
    if password != confirm:
        return "The two passwords don’t match."
    return ""


@admin_only
def users(request):
    User = get_user_model()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            username = request.POST.get("username", "").strip().lower()
            name = request.POST.get("name", "").strip()[:150]
            problem = _password_problem(request.POST.get("password", ""), request.POST.get("confirm", ""))
            if not username or not username.replace(".", "").replace("_", "").isalnum():
                messages.error(request, "Pick a username with letters and numbers only, e.g. satish.")
            elif User.objects.filter(username=username).exists():
                messages.error(request, f"“{username}” is already taken.")
            elif problem:
                messages.error(request, problem)
            else:
                u = User.objects.create_user(username, password=request.POST["password"], first_name=name)
                AuditLog.record("users", f"Login created for {name or username} ({username})")
                messages.success(request, f"Login created for {u.get_full_name() or username}. Share the password with them privately.")
        elif action == "access":
            u = get_object_or_404(User, pk=request.POST.get("user"), is_superuser=False)
            _save_access(u, request.POST)
            messages.success(request, f"Access saved for {u.get_full_name() or u.username}.")
        elif action in {"reset", "toggle"}:
            u = get_object_or_404(User, pk=request.POST.get("user"))
            if u == request.user:
                messages.error(request, "Use “Change my password” for your own login.")
            elif action == "reset":
                problem = _password_problem(request.POST.get("password", ""), request.POST.get("confirm", ""))
                if problem:
                    messages.error(request, problem)
                else:
                    u.set_password(request.POST["password"])
                    u.save()
                    AuditLog.record("users", f"Password reset for {u.get_full_name() or u.username}")
                    messages.success(request, f"New password set for {u.get_full_name() or u.username}.")
            else:
                u.is_active = not u.is_active
                u.save(update_fields=["is_active"])
                state = "switched on" if u.is_active else "switched off"
                AuditLog.record("users", f"Login {state} for {u.get_full_name() or u.username}")
                messages.success(request, f"Login {state} for {u.get_full_name() or u.username}.")
        return redirect("users")
    items = list(Item.objects.order_by("archived", "sort_order", "name"))
    people = list(User.objects.order_by("-is_active", "username"))
    held = {(m.user_id, m.item_id): m for m in Membership.objects.all()}
    for u in people:
        u.access = [(i, held.get((u.pk, i.pk))) for i in items]
        u.access_count = sum(1 for _, m in u.access if m)
    return render(request, "cluster/users.html", {"users": people, "items": items, "roles": Membership.Role.choices,
                                                  "min_length": MIN_LENGTH})


def _save_access(user, data) -> None:
    """One row per cluster item: a role (or no access) and the three switches."""
    roles = dict(Membership.Role.choices)
    summary = []
    for item in Item.objects.all():
        role = data.get(f"role-{item.pk}", "")
        if role not in roles:
            Membership.objects.filter(user=user, item=item).delete()
            continue
        Membership.objects.update_or_create(user=user, item=item, defaults={
            "role": role, "money_access": data.get(f"money-{item.pk}") == "1",
            "see_full_ids": data.get(f"ids-{item.pk}") == "1", "can_delete": data.get(f"delete-{item.pk}") == "1",
        })
        summary.append(f"{item.name}: {roles[role]}")
    AuditLog.record("users", f"Access for {user.get_full_name() or user.username}: {'; '.join(summary) or 'none'}")


def my_password(request):
    if request.method == "POST":
        current = request.POST.get("current", "")
        problem = _password_problem(request.POST.get("password", ""), request.POST.get("confirm", ""))
        if not request.user.check_password(current):
            messages.error(request, "Your current password is wrong.")
        elif problem:
            messages.error(request, problem)
        else:
            request.user.set_password(request.POST["password"])
            request.user.save()
            update_session_auth_hash(request, request.user)  # stay signed in
            AuditLog.record("users", "Changed own password")
            messages.success(request, "Your password is changed.")
            return redirect("cluster_home")
        return redirect("my_password")
    return render(request, "core/my_password.html", {"min_length": MIN_LENGTH})
