from django.conf import settings
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")


class ThrottledLoginView(LoginView):
    """Django login + lockout after repeated failures (per IP and per username)."""

    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def _keys(self):
        username = self.request.POST.get("username", "").strip().lower()
        return [f"login-fail:ip:{_client_ip(self.request)}", f"login-fail:user:{username}"]

    def _locked(self) -> bool:
        return any(cache.get(k, 0) >= settings.LOGIN_MAX_ATTEMPTS for k in self._keys())

    def post(self, request, *args, **kwargs):
        if self._locked():
            form = self.form_class(request)  # unbound: do not even try the password
            return self.render_to_response(
                self.get_context_data(form=form, locked=True, lock_minutes=settings.LOGIN_LOCKOUT_SECONDS // 60)
            )
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        for key in self._keys():
            cache.set(key, cache.get(key, 0) + 1, settings.LOGIN_LOCKOUT_SECONDS)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete_many(self._keys())
        return super().form_valid(form)


def healthz(request):
    """For the uptime ping: wakes the app and touches the database so neither goes to sleep."""
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")
