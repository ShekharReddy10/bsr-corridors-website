"""Logins: one per person for the whole cluster (Cluster → Users, admin only), with a role at each cluster item, and
"Change my password" for everyone. Money pages still need the item's money password on top of the login."""

from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.shortcuts import get_object_or_404, redirect, render

from cluster.models import Activity, Item, Membership


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
                Activity.record("access", f"Login created for {name or username} ({username})")
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
                    Activity.record("access", f"Password reset for {u.get_full_name() or u.username}")
                    messages.success(request, f"New password set for {u.get_full_name() or u.username}.")
            else:
                u.is_active = not u.is_active
                u.save(update_fields=["is_active"])
                state = "switched on" if u.is_active else "switched off"
                Activity.record("access", f"Login {state} for {u.get_full_name() or u.username}")
                messages.success(request, f"Login {state} for {u.get_full_name() or u.username}.")
        return redirect("users")
    items = list(Item.objects.order_by("archived", "sort_order", "name"))
    people = list(User.objects.order_by("-is_active", "username"))
    held = {(m.user_id, m.item_id): m for m in Membership.objects.all()}
    for u in people:
        u.access = [(i, held.get((u.pk, i.pk))) for i in items]
        u.access_count = sum(1 for _, m in u.access if m)
    from cluster import money
    controlled = {i.pk for i in items if money.controlled(i)}
    return render(request, "cluster/users.html", {"users": people, "items": items, "roles": Membership.Role.choices,
                                                  "controlled": controlled,
                                                  "min_length": MIN_LENGTH})


def _access_text(m) -> str:
    if m is None:
        return "no access"
    extra = [w for w, on in (("money", m.money_access), ("full IDs", m.see_full_ids), ("delete", m.can_delete)) if on]
    return m.get_role_display() + (f" + {', '.join(extra)}" if extra else "")


def _save_access(user, data) -> None:
    """One row per cluster item: a role (or no access) and two switches. Money access is never set here (the
    property's owner gives it), and once a property's owner has an authenticator, its owners can't be changed here
    either. The group's log says what changed."""
    from cluster import money
    roles = dict(Membership.Role.choices)
    changes = []
    for item in Item.objects.all():
        existing = Membership.objects.filter(user=user, item=item).first()
        before = _access_text(existing)
        role = data.get(f"role-{item.pk}", "")
        if money.controlled(item) and (
                (existing is not None and existing.role == Membership.Role.OWNER) or role == Membership.Role.OWNER):
            continue  # owners of a property its owners control: only they change them (Money access page)
        if role not in roles:
            if existing is not None and existing.money_access and money.controlled(item):
                continue  # someone the owner gave money access to: the owner removes them
            Membership.objects.filter(user=user, item=item).delete()
            after = "no access"
        else:
            m, _ = Membership.objects.update_or_create(user=user, item=item, defaults={
                "role": role, "see_full_ids": data.get(f"ids-{item.pk}") == "1",
                "can_delete": data.get(f"delete-{item.pk}") == "1",
            })
            after = _access_text(m)
        if after != before:
            changes.append(f"{item.name}: {before} → {after}")
    if changes:
        Activity.record("access", f"Access for {user.get_full_name() or user.username} — {'; '.join(changes)}")


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
            Activity.record("access", "Changed own password")
            messages.success(request, "Your password is changed.")
            return redirect("cluster_home")
        return redirect("my_password")
    return render(request, "core/my_password.html", {"min_length": MIN_LENGTH})
