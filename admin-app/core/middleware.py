from contextvars import ContextVar
from urllib.parse import quote

from django.shortcuts import redirect

# Paths reachable without signing in.
PUBLIC_PREFIXES = ("/login/", "/static/", "/healthz/", "/backup/run/")


class LoginRequiredMiddleware:
    """Every page requires the admin to be signed in, except the few public paths above."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not request.path.startswith(PUBLIC_PREFIXES):
            return redirect(f"/login/?next={quote(request.get_full_path())}")
        return self.get_response(request)


class NoIndexMiddleware:
    """Tell search engines never to index the admin app."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


_user_name: ContextVar[str] = ContextVar("user_name", default="")


def user_name(user) -> str:
    return (user.get_full_name() or user.username) if user and user.is_authenticated else ""


def current_user_name() -> str:
    """Who is signed in for the request being handled ("" in commands and the nightly job)."""
    return _user_name.get()


class CurrentUserMiddleware:
    """Remember who is signed in, so the activity log can say who did what."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _user_name.set(user_name(request.user))
        try:
            return self.get_response(request)
        finally:
            _user_name.reset(token)
