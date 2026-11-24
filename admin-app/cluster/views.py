"""Cluster pages: the home page (one card per cluster item the user may open) and each item's theme."""

import logging
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from . import current
from .models import THEMES, Item

log = logging.getLogger(__name__)


def admin_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            messages.error(request, "Only the main admin can do that.")
            return redirect("cluster_home")
        return view(request, *args, **kwargs)
    return wrapper


def items_for(user):
    """The cluster items this person may open (the main admin: all, archived ones last)."""
    qs = Item.objects.all() if user.is_superuser else Item.objects.filter(memberships__user=user, archived=False)
    return list(qs.order_by("archived", "sort_order", "name"))


def snapshot(item) -> dict:
    """Today at a glance, no money (decision D6): rooms taken tonight, arrivals, departures, things to check."""
    from core import payments, services
    from core.models import Room, Stay
    try:
        with current.using(item):
            t = services.today()
            live = Stay.objects.live()
            rooms = Room.objects.filter(is_active=True).count()
            taken = live.overlapping(t, t + timedelta(days=1)).values("room_id").distinct().count()
            return {
                "units": "rooms", "taken": taken, "total": rooms,
                "arriving": live.filter(status=Stay.Status.UPCOMING, check_in=t).count(),
                "departing": live.filter(status=Stay.Status.CHECKED_IN, check_out=t).count(),
                "verify": payments.to_verify().count(),
            }
    except Exception:  # its database can't be reached or isn't up to date: the card says so, others still show
        log.exception("Snapshot failed for %s", item.code)
        return {"error": True}


def home(request):
    items = items_for(request.user)
    if not request.user.is_superuser and len(items) == 1 and request.GET.get("all") is None:
        return redirect(items[0].prefix)  # only one: straight in (S1)
    cards = [{"item": i, "snap": snapshot(i) if not i.archived else None} for i in items]
    extra = {}
    if request.user.is_superuser:  # database space (S7) and failed backups: the main admin's concern
        per_item = schema_usage()
        extra = {"db": db_usage(), "usage": [(i.name, per_item[i.code]) for i in items if i.code in per_item],
                 "backup_failures": backup_failures()}
    return render(request, "cluster/home.html", {"cards": cards, "cluster_name": settings.CLUSTER_NAME, **extra})


def backup_failures() -> list[tuple[str, str]]:
    """Items whose latest nightly backup failed: (name, reason)."""
    from core.models import BackupRun
    out = []
    for item in Item.objects.filter(archived=False):
        try:
            with current.using(item):
                last = BackupRun.objects.order_by("-pk").first()
        except Exception:
            continue
        if last is not None and not last.ok:
            out.append((item.name, last.message[:160]))
    return out


@admin_only
def theme(request, code):
    item = get_object_or_404(Item, code=code)
    if request.method == "POST":
        chosen = request.POST.get("theme", "")
        if chosen in THEMES:
            item.theme = chosen
            item.save(update_fields=["theme"])
            messages.success(request, f"{item.name}: theme changed to {THEMES[chosen][0]}.")
            return redirect("cluster_home")
        messages.error(request, "Pick one of the themes.")
    return render(request, "cluster/theme.html", {
        "obj": item, "themes": [(k, *v) for k, v in THEMES.items()], "cluster_name": settings.CLUSTER_NAME,
    })



# ── Add / edit cluster items (admin) ──


def _set_money_password(item, password: str) -> None:
    from django.contrib.auth.hashers import make_password
    from core.models import AppSetting, AuditLog
    from core.views.revenue import HASH_KEY
    with current.using(item):
        AppSetting.put(HASH_KEY, make_password(password))
        AuditLog.record("revenue_password", "Money password set by the main admin")


@admin_only
def item_new(request):
    from .forms import ItemForm
    from .models import Membership
    from . import provision
    form = ItemForm(request.POST or None, initial={"theme": "teal", "kind": Item.Kind.PG})
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.schema = provision.schema_name(item.code)
        item.save()
        try:
            provision.provision(item)  # its own tables, for its type
        except Exception as exc:
            log.exception("Creating %s failed", item.code)
            try:
                provision.drop(item)
            finally:
                item.delete()
            messages.error(request, f"Couldn’t create the tables for {item.name}: {exc}. Nothing was saved.")
            return render(request, "cluster/item_form.html", {"form": form, "obj": None})
        _set_money_password(item, form.cleaned_data["money_password"])
        manager = item.managed_by
        if manager and not manager.is_superuser:
            Membership.objects.get_or_create(user=manager, item=item, defaults={"role": Membership.Role.MANAGER})
        messages.success(request, f"{item.name} is ready. Next: connect its Google Drive, then add its rooms.")
        return redirect("item_edit", code=item.code)
    return render(request, "cluster/item_form.html", {"form": form, "obj": None})


@admin_only
def item_edit(request, code):
    from .forms import ItemForm
    item = get_object_or_404(Item, code=code)
    form = ItemForm(request.POST or None, instance=item)
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "archive":
            if item.is_default:
                messages.error(request, "BSR Corridors can’t be archived.")
            else:
                item.archived = not item.archived
                item.save(update_fields=["archived"])
                messages.success(request, f"{item.name} {'archived — hidden and read-only' if item.archived else 'is back'}.")
            return redirect("item_edit", code=code)
        if action == "drive-disconnect":
            item.drive_token, item.drive_email = "", ""
            item.save(update_fields=["drive_token", "drive_email"])
            messages.success(request, "Google Drive disconnected. Files already in that Drive stay there.")
            return redirect("item_edit", code=code)
        if action in {"cover", "logo"}:
            return _upload_photo(request, item, action)
        if form.is_valid():
            form.save()
            if form.cleaned_data.get("money_password"):
                _set_money_password(item, form.cleaned_data["money_password"])
                messages.success(request, f"{item.name}: saved, and the money password is changed.")
            else:
                messages.success(request, f"{item.name}: saved.")
            return redirect("item_edit", code=code)
    return render(request, "cluster/item_form.html", {
        "form": form, "obj": item, "web_drive_ready": bool(settings.GOOGLE_WEB_CLIENT_ID),
        "usage": schema_usage().get(item.code),
    })


def _upload_photo(request, item, which):
    from core import gdrive, photos
    upload = request.FILES.get("photo")
    if not upload:
        messages.error(request, "Choose a photo first.")
        return redirect("item_edit", code=item.code)
    with current.using(item):
        if not gdrive.is_configured():
            messages.error(request, "Connect this item’s Google Drive first — photos are kept there.")
            return redirect("item_edit", code=item.code)
        try:
            data = photos.prepare(upload)
            file_id = gdrive.upload_photo(data, f"{item.code}-{which}.jpg", "item")
        except photos.BadPhoto as exc:
            messages.error(request, str(exc))
            return redirect("item_edit", code=item.code)
    setattr(item, f"{which}_id", file_id)
    item.save(update_fields=[f"{which}_id"])
    from django.core.cache import cache
    cache.delete(f"item-photo:{item.code}:{which}")
    messages.success(request, f"{'Cover photo' if which == 'cover' else 'Logo'} saved.")
    return redirect("item_edit", code=item.code)


def item_photo(request, code, which):
    """The cover photo / logo (for cards and headers): from the item's Drive, kept in the cache for a day."""
    from django.core.cache import cache
    from django.http import Http404, HttpResponse
    item = get_object_or_404(Item, code=code)
    file_id = getattr(item, f"{which}_id", "") if which in {"cover", "logo"} else ""
    if not file_id or item not in items_for(request.user):
        raise Http404
    key = f"item-photo:{code}:{which}"
    data = cache.get(key)
    if data is None:
        from core import gdrive
        try:
            with current.using(item):
                data = gdrive.fetch(file_id)
        except Exception:
            raise Http404
        cache.set(key, data, 24 * 3600)
    response = HttpResponse(data, content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=3600"
    return response


# ── Connect Google Drive (each item its own Google account) ──

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _flow(request, state=None):
    from google_auth_oauthlib.flow import Flow
    from django.urls import reverse
    flow = Flow.from_client_config(
        {"web": {"client_id": settings.GOOGLE_WEB_CLIENT_ID, "client_secret": settings.GOOGLE_WEB_CLIENT_SECRET,
                 "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                 "token_uri": "https://oauth2.googleapis.com/token"}},
        scopes=DRIVE_SCOPES, state=state)
    flow.redirect_uri = request.build_absolute_uri(reverse("drive_callback"))
    return flow


@admin_only
def drive_connect(request, code):
    from django.core import signing
    item = get_object_or_404(Item, code=code, is_default=False)
    if not (settings.GOOGLE_WEB_CLIENT_ID and settings.GOOGLE_WEB_CLIENT_SECRET):
        messages.error(request, "Add GOOGLE_WEB_CLIENT_ID and GOOGLE_WEB_CLIENT_SECRET in Render first (see the note "
                                "on this page).")
        return redirect("item_edit", code=code)
    state = signing.dumps({"item": item.code, "user": request.user.pk}, salt="drive-connect")
    url, _ = _flow(request, state).authorization_url(access_type="offline", prompt="consent",
                                                     include_granted_scopes="true")
    return redirect(url)


@admin_only
def drive_callback(request):
    from django.core import signing
    try:
        state = signing.loads(request.GET.get("state", ""), salt="drive-connect", max_age=900)
    except signing.BadSignature:
        messages.error(request, "That Google sign-in link expired — try Connect Google Drive again.")
        return redirect("cluster_home")
    item = get_object_or_404(Item, code=state["item"], is_default=False)
    if request.GET.get("error") or state.get("user") != request.user.pk:
        messages.error(request, "Google Drive wasn’t connected (cancelled).")
        return redirect("item_edit", code=item.code)
    flow = _flow(request, request.GET.get("state"))
    try:
        flow.fetch_token(code=request.GET.get("code", ""))
    except Exception as exc:
        log.exception("Drive connect failed for %s", item.code)
        messages.error(request, f"Google Drive wasn’t connected: {exc}")
        return redirect("item_edit", code=item.code)
    creds = flow.credentials
    if not creds.refresh_token:
        messages.error(request, "Google didn’t give lasting access — remove the app at myaccount.google.com → "
                                "Security → Third-party access, then connect again.")
        return redirect("item_edit", code=item.code)
    item.drive_token = creds.refresh_token
    try:
        from googleapiclient.discovery import build
        about = build("drive", "v3", credentials=creds, cache_discovery=False).about().get(fields="user").execute()
        item.drive_email = about["user"].get("emailAddress", "")
    except Exception:
        item.drive_email = ""
    item.save(update_fields=["drive_token", "drive_email"])
    messages.success(request, f"{item.name}: Google Drive connected{f' ({item.drive_email})' if item.drive_email else ''}.")
    return redirect("item_edit", code=item.code)


# ── Database usage (S7: required) ──

DB_LIMIT_MB = 500  # Supabase free plan


def db_usage() -> dict | None:
    """The main database's size against the free plan's 500 MB. None on SQLite (local tests)."""
    from django.db import connections
    conn = connections["default"]
    if conn.vendor != "postgresql":
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        used = cur.fetchone()[0] / 1024 / 1024
    limit = getattr(settings, "DB_LIMIT_MB", DB_LIMIT_MB)
    pct = round(used / limit * 100)
    return {"used": round(used, 1), "limit": limit, "pct": min(pct, 100), "warn": pct >= 80}


def schema_usage() -> dict:
    """MB used by each cluster item's tables (BSR Corridors: the public schema)."""
    from django.db import connections
    conn = connections["default"]
    if conn.vendor != "postgresql":
        return {}
    schemas = {i.schema or "public": i.code for i in Item.objects.all() if not i.db_url}
    with conn.cursor() as cur:
        cur.execute("""SELECT n.nspname, sum(pg_total_relation_size(c.oid)) FROM pg_class c
                       JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname = ANY(%s)
                       GROUP BY n.nspname""", [list(schemas)])
        return {schemas[name]: round(size / 1024 / 1024, 1) for name, size in cur.fetchall()}
