"""Pick the cluster item for each request and check the user may do what they ask.

Addresses:
  /cluster/…        cluster pages (home, items, users) — no item
  /p/<code>/…       a cluster item's pages; the /p/<code> part is taken off so the usual pages answer, and every link
                    the page makes gets it back (script prefix)
  everything else   BSR Corridors, at the same addresses as before v2 (bookmarks and guest links keep working)
"""

from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import set_script_prefix

from . import access, current
from .models import Item, Membership

CLUSTER_PATHS = ("/cluster/", "/login/", "/logout/", "/healthz/", "/backup/run/", "/static/")
GUEST_PATHS = ("/invoice/", "/pay/", "/s/")  # signed public links sent to guests
NO_CHECK = {"login", "logout", "healthz", "backup_hook", "my_password", "guest_invoice", "pay_page", "short_link"}


def split(path: str) -> tuple[str | None, str]:
    """'/p/abc/stays/1/' → ('abc', '/stays/1/'); other paths → (None, path)."""
    if path.startswith("/p/"):
        code, _, rest = path[3:].partition("/")
        return code, "/" + rest
    return None, path


def default_item() -> Item | None:
    return Item.objects.filter(is_default=True).first()


def membership(user, item) -> Membership | None:
    if not user.is_authenticated or user.is_superuser:
        return None
    return Membership.objects.filter(user=user, item=item).first()


class ClusterMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        code, rest = split(request.path_info)
        if code is not None and rest.startswith(CLUSTER_PATHS):  # a cluster link made on an item page
            if request.method == "GET":
                query = request.META.get("QUERY_STRING", "")
                return redirect(rest + (f"?{query}" if query else ""))
            code, request.path_info = None, rest
        if code is not None:
            item = Item.objects.filter(code=code).first()
            if item is None or item.is_default or (item.archived and not request.user.is_superuser):
                raise Http404
            request.path_info = rest
        elif request.path_info.startswith(CLUSTER_PATHS):
            item = None
        else:
            item = default_item()
        request.item = item
        request.membership = membership(request.user, item) if item else None
        request.caps = access.caps(request.user, request.membership) if item else set()
        token = current.set_item(item)
        if code is not None:
            set_script_prefix(item.prefix)
        try:
            return self.get_response(request)
        finally:
            current.reset(token)
            if code is not None:
                set_script_prefix("/")

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if request.item is None or not request.user.is_authenticated or (match and match.url_name in NO_CHECK):
            return None
        if not request.caps:
            if request.item.is_default and request.path_info == "/":
                return redirect("cluster_home")  # someone without BSR Corridors: their own cluster home
            return render(request, "cluster/no_access.html", {"item": request.item}, status=403)
        need = access.needed(request, match.url_name if match else "", view_kwargs)
        if not need <= request.caps:
            return render(request, "cluster/no_access.html", {"item": request.item, "page": True}, status=403)
        return None
