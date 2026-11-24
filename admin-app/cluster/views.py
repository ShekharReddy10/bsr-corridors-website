"""Cluster pages: the home page (one card per cluster item the user may open) and each item's theme."""

import logging
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import current
from .models import THEMES, Activity, Item

log = logging.getLogger(__name__)


def admin_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            messages.error(request, "Only the main admin can do that.")
            return redirect("cluster_home")
        return view(request, *args, **kwargs)
    return wrapper


def theme_list():
    return [(k, *v) for k, v in THEMES.items()]


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
            if item.kind != Item.Kind.HOTEL:
                return _pg_snapshot(t)
            live = Stay.objects.live()
            rooms = Room.objects.filter(is_active=True)
            busy = set(live.overlapping(t, t + timedelta(days=1)).values_list("room_id", flat=True))
            taken = len(busy)
            free = rooms.exclude(pk__in=busy).exclude(status=Room.Status.MAINTENANCE).count()
            rooms = rooms.count()
            return {
                "units": "rooms", "taken": taken, "total": rooms,
                "arriving": live.filter(status=Stay.Status.UPCOMING, check_in=t).count(),
                "departing": live.filter(status=Stay.Status.CHECKED_IN, check_out=t).count(),
                "verify": payments.to_verify().count(),
                "vacant": free,
            }
    except Exception:  # its database can't be reached or isn't up to date: the card says so, others still show
        log.exception("Snapshot failed for %s", item.code)
        return {"error": True}


def _pg_snapshot(t) -> dict:
    """PG / co-living: beds taken today, move-ins and last days today, deposits waiting to be refunded."""
    from pg import money, services as pg
    from pg.models import Bed, Complaint, Tenancy
    c = pg.counts(pg.bed_views(Bed.objects.in_use().select_related("room"), t))
    open_ = Tenancy.objects
    return {
        "units": "beds", "taken": c["taken"], "total": c["total"],
        "arriving": open_.filter(status=Tenancy.Status.RESERVED, move_in__lte=t).count(),
        "departing": open_.filter(status=Tenancy.Status.NOTICE, last_day__lte=t).count(),
        "verify": money.to_verify().count(),
        "refunds": open_.filter(status=Tenancy.Status.MOVED_OUT, refund__gt=0, refund_paid_on__isnull=True).count(),
        "complaints": Complaint.objects.exclude(status=Complaint.Status.FIXED).count(),
        "vacant": c["vacant"], "maintenance": c["maintenance"],
    }


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
            Activity.record("property", f"{item.name}: theme changed to {THEMES[chosen][0]}", item)
            messages.success(request, f"{item.name}: theme changed to {THEMES[chosen][0]}.")
            return redirect("cluster_home")
        messages.error(request, "Pick one of the themes.")
    return render(request, "cluster/theme.html", {
        "obj": item, "themes": theme_list(), "cluster_name": settings.CLUSTER_NAME,
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
    from .forms import partner_formset
    form = ItemForm(request.POST or None, initial={"theme": "teal", "kind": Item.Kind.PG})
    partners = partner_formset(request.POST or None, instance=Item())
    ctx = {"form": form, "obj": None, "themes": theme_list(), "partners": partners}
    if request.method == "POST" and form.is_valid() and partners.is_valid():
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
            return render(request, "cluster/item_form.html", ctx)
        _set_money_password(item, form.cleaned_data["money_password"])
        partners.instance = item
        partners.save()
        Activity.record("property", f"{item.name} added ({item.get_kind_display()}, code {item.code})"
                                    + (f"; partners: {_partners_text(item)}" if item.partners.exists() else ""), item)
        manager = item.managed_by
        if manager and not manager.is_superuser:
            Membership.objects.get_or_create(user=manager, item=item, defaults={"role": Membership.Role.MANAGER})
        messages.success(request, f"{item.name} is ready. Next: connect its Google Drive, then add its rooms.")
        return redirect("item_edit", code=item.code)
    return render(request, "cluster/item_form.html", ctx)


def _partners_text(item) -> str:
    rows = list(item.partners.all())
    if not rows:
        return "none"
    if all(p.share is not None for p in rows):
        return ", ".join(f"{p.name} {p.share.normalize():f}%" for p in rows)
    return ", ".join(p.name for p in rows) + " (equal shares)"


def _old_partner_names(item) -> list[str]:
    """Partners from the old Settings list (before they were set on the property's page), to start the form with."""
    from core.models import AppSetting
    from core.services import DEFAULT_PARTNERS, PARTNERS_KEY
    try:
        with current.using(item):
            raw = AppSetting.get(PARTNERS_KEY, DEFAULT_PARTNERS if item.is_default else "")
    except Exception:
        return []
    return [n.strip() for n in raw.split(",") if n.strip()]


@admin_only
def item_edit(request, code):
    from .forms import ItemForm
    from .forms import partner_formset
    item = get_object_or_404(Item, code=code)
    form = ItemForm(request.POST or None, instance=item)
    start = [] if item.partners.exists() else [{"name": n} for n in _old_partner_names(item)]
    partners = partner_formset(request.POST or None, instance=item, initial=start, extra=max(2, len(start) + 1))
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "archive":
            if item.is_default:
                messages.error(request, "BSR Corridors can’t be archived.")
            else:
                item.archived = not item.archived
                item.save(update_fields=["archived"])
                Activity.record("property", f"{item.name} {'archived' if item.archived else 'brought back'}", item)
                messages.success(request, f"{item.name} {'archived — hidden and read-only' if item.archived else 'is back'}.")
            return redirect("item_edit", code=code)
        if action == "drive-disconnect":
            item.drive_token, item.drive_email = "", ""
            item.save(update_fields=["drive_token", "drive_email"])
            Activity.record("drive", f"{item.name}: Google Drive disconnected", item)
            messages.success(request, "Google Drive disconnected. Files already in that Drive stay there.")
            return redirect("item_edit", code=code)
        if action in {"cover", "logo"}:
            return _upload_photo(request, item, action)
        if action == "move-db":
            from .move import MoveFailed, move
            try:
                copied = move(item, request.POST.get("db_url", "").strip())
            except MoveFailed as exc:
                Activity.record("database", f"{item.name}: move to its own database failed — {exc}"[:500], item)
                messages.error(request, f"Not moved: {exc}")
            else:
                Activity.record("database", f"{item.name} moved to its own database ({sum(copied.values())} records "
                                            "copied and checked)", item)
                messages.success(request, f"{item.name} moved to its own database — {sum(copied.values())} records "
                                          f"copied and checked. The old copy is kept as a fallback.")
            return redirect("item_edit", code=code)
        if form.is_valid() and partners.is_valid():
            changed = [str(form.fields[n].label or n).lower() for n in form.changed_data
                       if n not in ("money_password", "money_confirm")]
            form.save()
            before = _partners_text(item)
            partners.save()
            if (after := _partners_text(item)) != before:
                Activity.record("property", f"{item.name}: partners {before} → {after}", item)
            if changed:
                Activity.record("property", f"{item.name}: changed {', '.join(changed)}", item)
            if form.cleaned_data.get("money_password"):
                _set_money_password(item, form.cleaned_data["money_password"])
                Activity.record("money_password", f"{item.name}: money password reset", item)
                messages.success(request, f"{item.name}: saved, and the money password is changed.")
            else:
                messages.success(request, f"{item.name}: saved.")
            return redirect("item_edit", code=code)
    return render(request, "cluster/item_form.html", {
        "form": form, "obj": item, "web_drive_ready": bool(settings.GOOGLE_WEB_CLIENT_ID), "themes": theme_list(),
        "usage": schema_usage().get(item.code), "partners": partners,
    })


def _upload_photo(request, item, which):
    from core import gdrive, photos
    upload = request.FILES.get("photo")
    if not upload:
        messages.error(request, "Choose a photo first.")
        return redirect("item_edit", code=item.code)
    with current.using(item):
        if not gdrive.is_configured():
            messages.error(request, "Connect this property’s Google Drive first — photos are kept there.")
            return redirect("item_edit", code=item.code)
        try:
            data = photos.prepare(upload)
            file_id = gdrive.upload_photo(data, f"{item.code}-{which}.jpg", "item")
        except photos.BadPhoto as exc:
            messages.error(request, str(exc))
            return redirect("item_edit", code=item.code)
    setattr(item, f"{which}_id", file_id)
    item.save(update_fields=[f"{which}_id"])
    Activity.record("property", f"{item.name}: {'cover photo' if which == 'cover' else 'logo'} changed", item)
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
    Activity.record("drive", f"{item.name}: Google Drive connected", item)
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


# ── Group-wide pages (§11, admin only): overview, person search, activity log ──


def _last_backup(item):
    from core.models import BackupRun
    try:
        with current.using(item):
            return BackupRun.objects.filter(kind=BackupRun.Kind.BACKUP).order_by("-pk").first()
    except Exception:
        return None


@admin_only
def overview(request):
    """Each property's occupancy, vacancies, things needing attention, backups and space — no money (D6)."""
    from django.db.models import Count
    from .models import Membership
    items = Item.objects.order_by("archived", "sort_order", "name")
    usage = schema_usage()
    people = dict(Membership.objects.values_list("item_id").annotate(n=Count("pk")))
    rows, total = [], {"rooms": [0, 0], "beds": [0, 0]}
    for i in items:
        snap = snapshot(i) if not i.archived else None
        if snap and not snap.get("error"):
            total[snap["units"]][0] += snap["taken"]
            total[snap["units"]][1] += snap["total"]
        alerts = []
        if snap and not snap.get("error"):
            for key, text in (("verify", "UPI payment{s} to check"), ("refunds", "deposit{s} to refund"),
                              ("complaints", "open complaint{s}"), ("maintenance", "under maintenance")):
                if snap.get(key):
                    alerts.append(f"{snap[key]} " + text.format(s="" if snap[key] == 1 else "s"))
        backup = _last_backup(i) if not i.archived else None
        rows.append({"item": i, "snap": snap, "alerts": alerts, "backup": backup, "people": people.get(i.pk, 0),
                     "mb": usage.get(i.code), "own_db": bool(i.db_url), "drive": bool(i.drive_token) or i.is_default})
    return render(request, "cluster/overview.html", {
        "rows": rows, "db": db_usage(), "total": total, "cluster_name": settings.CLUSTER_NAME,
        "failures": backup_failures(),
    })


def _digits(q: str) -> str:
    return "".join(c for c in q if c.isdigit())


@admin_only
def search(request):
    """Find a guest or resident by phone (or name) in every property (S20)."""
    from core.models import Guest
    q = request.GET.get("q", "").strip()
    digits = _digits(q)
    results, problems = [], []
    usable = len(digits) >= 4 or (not digits and len(q) >= 3)
    if q and usable:
        for item in Item.objects.order_by("archived", "sort_order", "name"):
            try:
                with current.using(item):
                    results += _search_item(item, q, digits, Guest)
            except Exception:
                log.exception("Search failed in %s", item.code)
                problems.append(item.name)
    archived = []
    if q and usable:
        from . import archive
        try:
            archived = archive.find_people(q, digits)
        except Exception:
            log.exception("Archive search failed")
            problems.append("the archive")
        names = {i.code: i.name for i in Item.objects.all()}
        for a in archived:
            a["property_name"] = names.get(a["property"], a["property"])
    return render(request, "cluster/search.html", {
        "q": q, "results": results, "problems": problems, "too_short": bool(q) and not usable, "archived": archived,
        "cluster_name": settings.CLUSTER_NAME,
    })


def _search_item(item, q, digits, Guest) -> list[dict]:
    from django.db.models import Q
    from django.urls import reverse
    prefix = item.prefix.rstrip("/")
    out = []
    if item.kind == Item.Kind.HOTEL:
        cond = Q(phone_key__contains=digits[-10:]) if digits else Q(name__icontains=q)
        for g in Guest.objects.filter(cond).order_by("name")[:25]:
            last = g.stays.order_by("-check_in").first()
            out.append({"item": item, "name": g.name, "phone": g.phone, "who": "Guest",
                        "status": (f"{last.get_status_display()} · {last.check_in:%d %b %Y}" if last else "No stays"),
                        "url": prefix + reverse("guest_detail", args=[g.pk])})
    else:
        from pg.models import Resident, Tenancy
        cond = Q(phone_key__contains=digits[-10:]) if digits else Q(name__icontains=q)
        for r in Resident.objects.filter(cond).order_by("name")[:25]:
            t = r.tenancies.exclude(status=Tenancy.Status.CANCELLED).select_related("bed__room").order_by("-move_in").first()
            status = "No stays"
            if t is not None:
                when = (f"moved out {t.move_out:%d %b %Y}" if t.move_out else
                        f"moves in {t.move_in:%d %b %Y}" if t.status == Tenancy.Status.RESERVED else
                        f"since {t.move_in:%d %b %Y}")
                status = f"{t.get_status_display()} · bed {t.bed} · {when}"
            out.append({"item": item, "name": r.name, "phone": r.phone, "who": "Resident", "status": status,
                        "url": prefix + reverse("pg_resident", args=[r.pk])})
    return out


@admin_only
def activity(request):
    from django.core.paginator import Paginator
    qs = Activity.objects.select_related("item")
    what = request.GET.get("what", "")
    if what in Activity.ACTIONS:
        qs = qs.filter(action=what)
    if (code := request.GET.get("item", "")):
        qs = qs.filter(item__code=code)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "cluster/activity.html", {
        "page": page, "what": what, "code": code, "actions": Activity.ACTIONS.items(),
        "items": Item.objects.order_by("sort_order", "name"), "cluster_name": settings.CLUSTER_NAME,
    })


# ── The archive database (admin): connect, move old records, read them back ──


@admin_only
def archive_page(request):
    from datetime import date as d_, timedelta as td
    from django.utils.dateparse import parse_date
    from . import archive
    from .models import Activity
    items = list(Item.objects.order_by("archived", "sort_order", "name"))
    action = request.POST.get("action", "")
    preview = None
    code = request.POST.get("item", request.GET.get("item", ""))
    before = parse_date(request.POST.get("before", "") or "") or d_.today().replace(day=1) - td(days=730)
    if request.method == "POST":
        try:
            if action == "connect":
                archive.connect(request.POST.get("db_url", ""), by=request.user.get_full_name() or request.user.username)
                Activity.record("archive", "Archive database connected")
                messages.success(request, "Archive database connected and ready.")
                return redirect("cluster_archive")
            if action == "disconnect":
                archive.disconnect()
                Activity.record("archive", "Archive database disconnected (its records stay in it)")
                messages.success(request, "Archive database disconnected. What was moved there stays there.")
                return redirect("cluster_archive")
            item = get_object_or_404(Item, code=code)
            if action == "preview":
                preview = {"item": item, "plan": archive.plan(item, before)}
            elif action == "move":
                p = archive.move(item, before)
                what = ", ".join(f"{n} {label.lower()}" for label, n in p.counts) or "nothing"
                Activity.record("archive", f"{item.name}: moved to the archive (before {before:%d-%m-%Y}) — {what}", item)
                if p.total:
                    messages.success(request, f"{item.name}: {p.total} old records moved to the archive and removed "
                                              f"from the main database." + (" More are left — press Move again." if p.more else ""))
                else:
                    messages.info(request, f"{item.name}: nothing finished before {before:%d-%m-%Y} to move.")
                return redirect(reverse("cluster_archive") + f"?item={item.code}")
        except archive.ArchiveError as exc:
            messages.error(request, str(exc))
    return render(request, "cluster/archive.html", {
        "store": archive.store(), "usage": archive.usage(), "main": db_usage(), "items": items, "code": code,
        "before": before.isoformat(), "preview": preview, "cluster_name": settings.CLUSTER_NAME,
        "names": {i.code: i.name for i in items},
    })


@admin_only
def archive_person(request, rid):
    from django.http import Http404
    from . import archive
    try:
        p = archive.person(rid)
    except archive.ArchiveError as exc:
        messages.error(request, str(exc))
        return redirect("cluster_archive")
    if p is None:
        raise Http404
    rooms = {}
    if p["property"] is not None:  # room / bed numbers: standard records, still in the main database
        try:
            with current.using(p["property"]):
                if p["model"] == "core.guest":
                    from core.models import Room
                    rooms = {r.pk: r.number for r in Room.objects.all()}
                else:
                    from pg.models import Bed
                    rooms = {b.pk: str(b) for b in Bed.objects.select_related("room")}
        except Exception:
            rooms = {}
    from django.utils.dateparse import parse_date
    if p["model"] == "core.guest":
        from core.models import Stay
        labels = {**dict(Stay.Status.choices), **dict(Stay.Source.choices)}
    else:
        from pg.models import Tenancy
        labels = {**dict(Tenancy.Status.choices), **dict(Tenancy.Cycle.choices)}
    for c in p["children"]:
        f = c["fields"]
        for k in ("status", "source", "cycle"):
            if f.get(k):
                f[k] = str(labels.get(f[k], f[k]))
        for k in ("check_in", "check_out", "move_in", "move_out", "refund_paid_on"):
            if f.get(k) and (d := parse_date(f[k])):
                f[k] = f"{d:%d-%m-%Y}"
        c["place"] = rooms.get(f.get("room") or f.get("bed"), "—")
        c["paid"] = sum(float(x.get("amount") or 0) for x in c["payments"])
    return render(request, "cluster/archive_person.html", {"p": p, "cluster_name": settings.CLUSTER_NAME})
