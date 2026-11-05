"""Logins: one per person (Settings → Users, admin only), and "Change my password" for everyone.
Money pages still need the revenue password on top of the login."""

from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.shortcuts import get_object_or_404, redirect, render

from ..models import AuditLog

MIN_LENGTH = 10


def admin_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            messages.error(request, "Only the main admin can manage logins.")
            return redirect("settings")
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
    return render(request, "core/users.html", {"users": User.objects.order_by("-is_active", "username"),
                                               "min_length": MIN_LENGTH})


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
            return redirect("settings")
        return redirect("my_password")
    return render(request, "core/my_password.html", {"min_length": MIN_LENGTH})
