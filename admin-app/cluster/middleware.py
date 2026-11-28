"""Pick the cluster item for each request and check the user may do what they ask.

Addresses:
  /properties/…     the properties pages (home, add / edit a property, users) — no property
  /p/<code>/…       a cluster item's pages; the /p/<code> part is taken off so the usual pages answer, and every link
                    the page makes gets it back (script prefix)
  everything else   BSR Corridors, at the same addresses as before v2 (bookmarks and guest links keep working)
"""

from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import set_script_prefix

from . import access, current, money
from .models import Item, Membership

CLUSTER_PATHS = ("/properties/", "/login/", "/logout/", "/healthz/", "/backup/run/", "/static/")
GUEST_PATHS = ("/invoice/", "/pay/", "/s/")  # signed public links sent to guests
# Hotel pages a PG / co-living doesn't have (it has floors → rooms → beds instead): they open its PG home.
HOTEL_ONLY = {"dashboard", "confirm_check", "calendar", "room_month", "stay_list", "stay_new", "upcoming", "stay_detail",
              "stay_edit", "stay_extend", "stay_reveal_id", "stay_whatsapp", "payment_delete", "stay_action",
              "guest_list", "guest_detail", "guest_lookup", "rooms", "roomtype_new", "roomtype_edit", "room_new",
              "room_edit", "room_log", "maintenance_done", "maintenance_delete", "room_delete", "stay_invoice",
              "stay_pay_link", "wa_in_house", "id_photos_upload", "id_photos_use", "id_photo_unuse", "id_photo_person",
              "id_photo", "id_photo_delete", "whatsapp", "wa_template_new", "wa_template_edit",
              "wa_template_delete"}
NO_CHECK = {"pg_pay_page", "pg_public_receipt", "login", "logout", "healthz", "backup_hook", "my_password", "guest_invoice", "pay_page", "short_link"}


def split(path: str) -> tuple[str | None, str]:
    """'/p/abc/stays/1/' → ('abc', '/stays/1/'); other paths → (None, path)."""
    if path.startswith("/p/"):
        code, _, rest = path[3:].partition("/")
        return code, "/" + rest
    return None, path


def default_item() -> Item | None:
    return Item.objects.filter(is_default=True).first()


def membership(user, item) -> Membership | None:
    """The person's role at the item (the main admin too: his money access comes from it)."""
    if not user.is_authenticated:
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
        if item is not None and item.archived:
            request.caps = request.caps & {"view"}  # archived: read-only, even for the main admin
        token = current.set_item(item)
        request._money_token = None
        if code is not None:
            set_script_prefix(item.prefix)
        try:
            return self.get_response(request)
        finally:
            if request._money_token is not None:
                money.reset_visible(request._money_token)
            current.reset(token)
            if code is not None:
                set_script_prefix("/")

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        name = match.url_name if match else ""
        # Amounts on screen: hidden for signed-in people until they unlock this property's money (guest pages —
        # payment link, invoice, receipt — always show theirs).
        if request.user.is_authenticated and name not in NO_CHECK and not request.path_info.startswith(GUEST_PATHS):
            request._money_token = money.set_visible(money.unlocked(request))
        if request.item is not None and name:
            if request.item.kind != Item.Kind.HOTEL and name in HOTEL_ONLY:
                return redirect("pg_messages" if name == "whatsapp" or name.startswith("wa_") else "pg_home")
            if request.item.kind == Item.Kind.HOTEL and name.startswith("pg_"):
                raise Http404
        if request.item is None or not request.user.is_authenticated or (match and match.url_name in NO_CHECK):
            return None
        if not request.caps:
            if request.item.is_default and request.path_info == "/":
                return redirect("cluster_home")  # someone without BSR Corridors: their own cluster home
            return render(request, "cluster/no_access.html", {"item": request.item}, status=403)
        need = access.needed(request, match.url_name if match else "", view_kwargs)
        if "money" in need and "money" not in request.caps and request.method == "GET":
            from urllib.parse import quote
            return redirect(current.local("/money/unlock/") + f"?next={quote(request.get_full_path())}")  # explains who to ask
        if not need <= request.caps:
            return render(request, "cluster/no_access.html", {"item": request.item, "page": True}, status=403)
        return None
