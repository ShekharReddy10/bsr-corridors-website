"""PG / co-living pages (§8.7): Floors → Rooms → Beds → Bed details, residents, move-in / notice / shift / move-out."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, ProtectedError, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from cluster import current
from core.services import today

from . import services
from .forms import (BedForm, FloorForm, NoticeForm, RefundForm, ResidentForm, RoomForm, RoomTypeForm, ShiftForm,
                    TenancyForm)
from .models import Bed, BedSpell, Floor, Resident, Room, RoomType, Tenancy

T = Tenancy.Status


def _who(request) -> str:
    return request.user.get_full_name() or request.user.username


def _error(request, exc: ValidationError) -> None:
    for m in exc.messages:
        messages.error(request, m)


def _all_beds():
    return Bed.objects.select_related("room", "room__floor", "room__room_type")


# ── Home: floors and today ────────────────────────────────────────────────────


def home(request):
    t = today()
    views = services.bed_views(_all_beds().in_use(), t)
    by_floor = defaultdict(list)
    for v in views:
        by_floor[v.bed.room.floor_id].append(v)
    floors = []
    for f in Floor.objects.annotate(n_rooms=Count("rooms")).order_by("sort_order", "name"):
        floors.append({"floor": f, "rooms": f.n_rooms, **services.counts(by_floor.get(f.pk, []))})
    week = t + timedelta(days=7)
    open_ = Tenancy.objects.select_related("resident", "bed__room")
    ctx = {
        "floors": floors, "all": services.counts(views),
        "moving_in": open_.filter(status=T.RESERVED, move_in__lte=t).order_by("move_in"),
        "moving_out": open_.filter(status=T.NOTICE, last_day__lte=t).order_by("last_day"),
        "notice_week": open_.filter(status=T.NOTICE, last_day__gt=t, last_day__lte=week).order_by("last_day"),
        "reserved": open_.filter(status=T.RESERVED, move_in__gt=t).order_by("move_in")[:10],
        "refunds": [x for x in open_.filter(status=T.MOVED_OUT, refund__gt=0, refund_paid_on__isnull=True)
                    .order_by("refund_due_by")],
        "vacant": [v.bed for v in views if v.state == "vacant"], "set_up": Room.objects.exists(), "today": t,
    }
    return render(request, "pg/home.html", ctx)


def floor(request, pk):
    fl = get_object_or_404(Floor, pk=pk)
    views = services.bed_views(_all_beds().in_use().filter(room__floor=fl))
    rooms = defaultdict(list)
    for v in views:
        rooms[v.bed.room].append(v)
    tiles = [{"room": r, "beds": rooms.get(r, []), **services.counts(rooms.get(r, []))}
             for r in fl.rooms.select_related("room_type")]
    return render(request, "pg/floor.html", {"floor": fl, "tiles": tiles, "floors": Floor.objects.all()})


def room(request, pk):
    rm = get_object_or_404(Room.objects.select_related("floor", "room_type"), pk=pk)
    if request.method == "POST":
        if "manage" not in request.caps:
            raise Http404
        try:
            n = max(1, min(int(request.POST.get("n", 1)), 10))
        except ValueError:
            n = 1
        made = services.add_beds(rm, n)
        messages.success(request, f"Added bed{'s' if n > 1 else ''} {', '.join(b.label for b in made)}.")
        return redirect("pg_room", pk=rm.pk)
    beds = services.bed_views(rm.beds.in_use().select_related("room"))
    for v in beds:
        v.rent = v.bed.rent_for("monthly")
        v.next = services.next_due(v.tenancy) if v.tenancy is not None and v.state != "reserved" else None
    removed = rm.beds.filter(removed_on__isnull=False).annotate(n=Count("spells")).order_by("label")
    return render(request, "pg/room.html", {"room": rm, "beds": beds, "removed": removed,
                                            "counts": services.counts(beds)})


def bed(request, pk):
    b = get_object_or_404(_all_beds(), pk=pk)
    view = services.bed_views([b])[0]
    history = (BedSpell.objects.filter(bed=b).exclude(tenancy__status=T.CANCELLED)
               .select_related("tenancy__resident").order_by("-start"))
    upcoming = None
    if view.state not in ("reserved", "removed"):
        upcoming = (b.spells.live().filter(start__gt=today()).select_related("tenancy__resident").first())
    return render(request, "pg/bed.html", {
        "bed": b, "view": view, "history": history, "upcoming": upcoming,
        "rent": [(c, b.rent_for(c)) for c in ("monthly", "weekly", "daily")],
        "next": services.next_due(view.tenancy) if view.tenancy and view.state != "reserved" else None,
    })


def bed_edit(request, pk):
    b = get_object_or_404(_all_beds(), pk=pk)
    form = BedForm(request.POST or None, instance=b)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Bed {b} saved.")
        return redirect("pg_bed", pk=b.pk)
    return render(request, "pg/form.html", {"form": form, "title": f"Bed {b}", "back": reverse("pg_bed", args=[b.pk])})


@require_POST
def bed_action(request, pk, action):
    b = get_object_or_404(_all_beds(), pk=pk)
    try:
        if action == "remove":
            services.remove_bed(b, request.POST.get("reason", ""), by=_who(request))
            messages.success(request, f"Bed {b} removed. It is kept under Removed beds with its history.")
        elif action == "restore":
            services.restore_bed(b)
            messages.success(request, f"Bed {b} is back in use.")
        else:
            raise Http404
    except ValidationError as exc:
        _error(request, exc)
    return redirect("pg_room", pk=b.room_id)


# ── Move-in ───────────────────────────────────────────────────────────────────


def move_in(request, pk):
    b = get_object_or_404(_all_beds(), pk=pk)
    if b.is_removed:
        messages.error(request, "That bed has been removed — restore it first.")
        return redirect("pg_room", pk=b.room_id)
    existing = None
    if request.method == "POST" and request.POST.get("resident_id"):
        existing = get_object_or_404(Resident, pk=request.POST["resident_id"])
    rform = ResidentForm(request.POST or None, instance=existing, prefix="r")
    tform = TenancyForm(request.POST or None, bed=b, prefix="t")
    if request.method == "POST" and rform.is_valid() and tform.is_valid():
        tenancy = tform.save(commit=False)
        tenancy.bed = b
        resident = rform.save(commit=False)
        if tenancy.move_in <= today() and not (resident.address.strip() and resident.id_number):
            rform.add_error("address" if not resident.address.strip() else "id_number",
                            "Needed at move-in (it can wait for a reservation).")
        else:
            try:
                from core.db import atomic
                with atomic():
                    resident.save()
                    tenancy.resident = resident
                    services.move_in(tenancy, by=_who(request))
            except ValidationError as exc:
                tform.add_error(None, exc)
            else:
                if tenancy.status == T.RESERVED:
                    messages.success(request, f"Bed {b} reserved for {resident.name} from {tenancy.move_in:%d-%m-%Y}.")
                else:
                    messages.success(request, f"{resident.name} moved in to bed {b}.")
                return redirect("pg_tenancy", pk=tenancy.pk)
    return render(request, "pg/move_in.html", {"bed": b, "rform": rform, "tform": tform, "existing": existing})


def resident_lookup(request):
    """Returning resident: their saved details by phone (last 10 digits)."""
    r = services.find_resident(request.GET.get("phone", ""))
    if r is None:
        return JsonResponse({"found": False})
    current_t = r.tenancies.open().select_related("bed__room").first()
    return JsonResponse({
        "found": True, "id": r.pk, "name": r.name, "address": r.address, "id_type": r.id_type,
        "id_masked": r.id_masked, "organisation": r.organisation, "occupation": r.occupation,
        "emergency_name": r.emergency_name, "emergency_relation": r.emergency_relation,
        "emergency_phone": r.emergency_phone, "vehicle": r.vehicle,
        "open": f"{current_t.get_status_display()} in bed {current_t.bed}" if current_t else "",
    })


# ── Tenancy ───────────────────────────────────────────────────────────────────


def _vacant_beds(exclude=None):
    views = services.bed_views(_all_beds().in_use())
    return [v.bed for v in views if v.state == "vacant" and v.bed.pk != getattr(exclude, "pk", None)]


def tenancy(request, pk):
    t = get_object_or_404(Tenancy.objects.select_related("resident", "bed__room__floor"), pk=pk)
    item = current.get()
    nxt = services.next_due(t) if t.status in (T.STAYING, T.NOTICE) else None
    ctx = {
        "t": t, "r": t.resident, "spells": t.spells.select_related("bed__room").order_by("start"), "next": nxt,
        "notice_form": NoticeForm(prefix="n"), "shift_form": ShiftForm(beds=_vacant_beds(t.bed), prefix="s"),
        "refund_form": RefundForm(prefix="f"), "schedule": services.periods(t, today())[-6:] if t.move_in <= today() else [],
        "standard": services.standard_deductions(item),
    }
    return render(request, "pg/tenancy.html", ctx)


@require_POST
def tenancy_action(request, pk, action):
    t = get_object_or_404(Tenancy.objects.select_related("resident", "bed__room"), pk=pk)
    try:
        if action == "confirm":
            services.confirm_move_in(t, parse_date(request.POST.get("day", "")) or today())
            messages.success(request, f"{t.resident.name} has moved in.")
        elif action == "cancel":
            services.cancel(t, request.POST.get("reason", "").strip())
            messages.success(request, "Reservation cancelled; the bed is free again.")
        elif action == "notice":
            form = NoticeForm(request.POST, prefix="n")
            if not form.is_valid():
                raise ValidationError("Enter the date the notice was given.")
            services.give_notice(t, form.cleaned_data["given_on"], form.cleaned_data["last_day"])
            messages.success(request, f"On notice — last day {t.last_day:%d-%m-%Y}.")
        elif action == "withdraw-notice":
            services.withdraw_notice(t)
            messages.success(request, "Notice withdrawn; they are staying on.")
        elif action == "shift":
            form = ShiftForm(request.POST, beds=_vacant_beds(t.bed), prefix="s")
            if not form.is_valid():
                raise ValidationError("Pick a vacant bed and the date.")
            services.shift(t, form.cleaned_data["bed"], form.cleaned_data["day"])
            messages.success(request, f"{t.resident.name} is now in bed {t.bed}.")
        elif action == "refund":
            form = RefundForm(request.POST, prefix="f")
            if not form.is_valid():
                raise ValidationError("Enter the date and how it was paid.")
            services.refund_paid(t, form.cleaned_data["paid_on"], form.cleaned_data["mode"])
            messages.success(request, "Deposit refund recorded.")
        elif action == "agreement":
            _upload(request, t, "agreement")
        else:
            raise Http404
    except ValidationError as exc:
        _error(request, exc)
    return redirect("pg_tenancy", pk=t.pk)


def _amount(raw: str) -> Decimal:
    try:
        value = Decimal((raw or "0").replace(",", "").replace("₹", "").strip() or "0")
    except InvalidOperation as exc:
        raise ValidationError(f"“{raw}” isn't an amount.") from exc
    if value < 0:
        raise ValidationError("Amounts can't be negative.")
    return value


def move_out(request, pk):
    t = get_object_or_404(Tenancy.objects.select_related("resident", "bed__room"), pk=pk)
    item = current.get()
    if t.status not in (T.STAYING, T.NOTICE):
        messages.info(request, "This tenancy isn't current.")
        return redirect("pg_tenancy", pk=t.pk)
    rows = services.standard_deductions(item)
    errors = []
    if request.method == "POST":
        reasons, amounts = request.POST.getlist("reason"), request.POST.getlist("amount")
        rows = [{"reason": r, "amount": a} for r, a in zip(reasons, amounts) if r.strip() or a.strip()]
        try:
            day = parse_date(request.POST.get("day", "")) or today()
            unpaid = _amount(request.POST.get("unpaid_rent", "0"))
            ded = [{"reason": r["reason"], "amount": _amount(r["amount"])} for r in rows]
            if any(d["amount"] > 0 and not d["reason"].strip() for d in ded):
                raise ValidationError("Give a reason for each deduction.")
            services.move_out(t, day, unpaid_rent=unpaid, deductions=ded, by=_who(request))
        except ValidationError as exc:
            errors = exc.messages
        else:
            if t.refund > 0:
                messages.success(request, f"{t.resident.name} moved out. Deposit to refund: ₹{t.refund:,.0f} "
                                          f"by {t.refund_due_by:%d-%m-%Y}.")
            elif t.refund < 0:
                messages.warning(request, f"{t.resident.name} moved out and still owes ₹{-t.refund:,.0f}.")
            else:
                messages.success(request, f"{t.resident.name} moved out. Nothing to refund.")
            return redirect("pg_tenancy", pk=t.pk)
    return render(request, "pg/move_out.html", {
        "t": t, "rows": rows + [{"reason": "", "amount": ""}] * 2, "errors": errors,
        "day": request.POST.get("day") or (t.last_day or today()).isoformat(),
        "unpaid_rent": request.POST.get("unpaid_rent", "0"), "refund_days": getattr(item, "refund_days", 7),
    })


# ── Residents ─────────────────────────────────────────────────────────────────


def residents(request):
    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "current")
    qs = Resident.objects.all()
    if q:
        digits = "".join(c for c in q if c.isdigit())
        cond = Q(name__icontains=q) | Q(tenancies__bed__room__number__iexact=q)
        if len(digits) >= 4:
            cond |= Q(phone_key__contains=digits[-10:])
        qs = qs.filter(cond)
    if show == "current":
        qs = qs.filter(tenancies__status__in=[T.RESERVED, T.STAYING, T.NOTICE])
    qs = qs.distinct().order_by("name")
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    now = {t.resident_id: t for t in Tenancy.objects.open().filter(resident__in=page.object_list)
           .select_related("bed__room")}
    rows = [(r, now.get(r.pk)) for r in page.object_list]
    return render(request, "pg/residents.html", {"page": page, "rows": rows, "q": q, "show": show})


def resident(request, pk):
    r = get_object_or_404(Resident, pk=pk)
    form = ResidentForm(request.POST or None, instance=r)
    if request.method == "POST":
        if request.POST.get("action") == "id-photo":
            try:
                _upload(request, r, "id")
            except ValidationError as exc:
                _error(request, exc)
            return redirect("pg_resident", pk=r.pk)
        if form.is_valid():
            form.save()
            messages.success(request, f"{r.name} saved.")
            return redirect("pg_resident", pk=r.pk)
    return render(request, "pg/resident.html", {
        "r": r, "form": form, "tenancies": r.tenancies.select_related("bed__room").order_by("-move_in"),
    })


@require_POST
def reveal_id(request, pk):
    """A resident's full ID number (logged), for those allowed to see full IDs."""
    from django.utils.html import escape

    from core.models import AuditLog
    r = get_object_or_404(Resident, pk=pk)
    AuditLog.record("reveal_id", f"Viewed full ID number of resident {r.name}")
    return HttpResponse(f'<span class="id-full">{escape(r.id_number or "—")}</span>')


# ── Files in Drive (agreement, ID photo) ──────────────────────────────────────


def _upload(request, obj, what: str) -> None:
    from core import gdrive
    f = request.FILES.get("file")
    if not f:
        raise ValidationError("Choose a photo or PDF first.")
    if f.size > 8 * 1024 * 1024:
        raise ValidationError("That file is over 8 MB.")
    if not gdrive.is_configured():
        raise ValidationError("Google Drive isn't connected for this item, so files can't be saved yet.")
    from core import photos
    head = f.read(4)
    f.seek(0)
    if head == b"%PDF":
        data, mime = f.read(), "application/pdf"
    else:
        try:
            data, mime = photos.prepare(f), "image/jpeg"  # made smaller, as for guest ID photos
        except photos.BadPhoto as exc:
            raise ValidationError(str(exc)) from exc
    name = f"{what}-{obj.pk}-{f.name}"[:120]
    file_id = gdrive.upload_file(data, name, mime, "pg")
    if what == "agreement":
        obj.agreement = file_id
        obj.save(update_fields=["agreement"])
    else:
        obj.id_photo = file_id
        obj.save(update_fields=["id_photo"])
    messages.success(request, "Saved in Google Drive.")


def file(request, what, pk):
    from core import gdrive
    if what == "agreement":
        file_id = get_object_or_404(Tenancy, pk=pk).agreement
    elif what == "id":
        file_id = get_object_or_404(Resident, pk=pk).id_photo
    else:
        raise Http404
    if not file_id:
        raise Http404
    data = gdrive.fetch(file_id)
    mime = "application/pdf" if data[:4] == b"%PDF" else "image/jpeg"
    resp = HttpResponse(data, content_type=mime)
    resp["Cache-Control"] = "private, max-age=600"
    return resp


# ── Set-up: floors, room types, rooms ─────────────────────────────────────────


def setup(request):
    return render(request, "pg/setup.html", {
        # (counts drop the models' own order, so it is given again)
        "floors": Floor.objects.annotate(n=Count("rooms")).order_by("sort_order", "name"),
        "types": RoomType.objects.annotate(n=Count("rooms")).order_by("name"),
        "rooms": Room.objects.select_related("floor", "room_type").annotate(
            n=Count("beds", filter=Q(beds__removed_on__isnull=True))).order_by("floor__sort_order", "number"),
    })


FORMS = {"floor": (Floor, FloorForm, "floor"), "type": (RoomType, RoomTypeForm, "room type"),
         "room": (Room, RoomForm, "room")}


def setup_edit(request, kind, pk=None):
    if kind not in FORMS:
        raise Http404
    model, form_class, label = FORMS[kind]
    obj = get_object_or_404(model, pk=pk) if pk else None
    if kind == "room" and not obj and not (Floor.objects.exists() and RoomType.objects.exists()):
        messages.info(request, "Add a floor and a room type first.")
        return redirect("pg_setup")
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and request.POST.get("action") == "delete" and obj:
        try:
            obj.delete()
            messages.success(request, f"The {label} was deleted.")
        except ProtectedError:
            messages.error(request, f"This {label} is in use (it has rooms, beds or history), so it is kept.")
            return redirect(request.path)
        return redirect("pg_setup")
    if request.method == "POST" and form.is_valid():
        from core.db import atomic
        with atomic():
            saved = form.save()
            if kind == "room" and not obj:
                services.add_beds(saved, form.cleaned_data["beds"])
        messages.success(request, f"{label.capitalize()} saved.")
        return redirect("pg_room", pk=saved.pk) if kind == "room" else redirect("pg_setup")
    return render(request, "pg/form.html", {
        "form": form, "title": (f"Edit {label}" if obj else f"Add {label}"), "back": reverse("pg_setup"),
        "can_delete": obj is not None and kind != "room" or (kind == "room" and obj is not None and not obj.beds.exists()),
    })
