"""PG / co-living pages (§8.7): Floors → Rooms → Beds → Bed details, residents, move-in / notice / shift / move-out."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, ProtectedError, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from cluster import current
from core.services import today

from . import services
from .complaints import open_complaints
from .messages import due_rule_text
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
        "complaints": _aged(open_complaints()), **_rent_today(t),
    }
    return render(request, "pg/home.html", ctx)


def _aged(complaints) -> list:
    out = list(complaints)
    for c in out:
        c.age = (today() - c.raised_on).days
    return out


def _rent_today(t) -> dict:
    from . import money
    owing = money.owing(t)
    due_today = [d for d in money.due_on(t) if d.tenancy.status in (T.STAYING, T.NOTICE)]
    still_owing = {o.tenancy.pk for o in owing}
    from .models import PgMessage
    return {"wa_rent_due": PgMessage.objects.filter(use="rent_due"), "wa_overdue": PgMessage.objects.filter(use="overdue"),
            "overdue": [o for o in owing if o.overdue], "due_today": [d for d in due_today if d.tenancy_id in still_owing],
            "verify": list(money.to_verify()), "owed_total": sum((o.amount for o in owing), Decimal(0))}


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
        v.next = services.next_due(v.tenancy) if v.tenancy is not None and v.state != "reserved" else None
    removed = rm.beds.filter(removed_on__isnull=False).annotate(n=Count("spells")).order_by("label")
    return render(request, "pg/room.html", {"room": rm, "beds": beds, "removed": removed,
                                            "counts": services.counts(beds),
                                            "complaints": _aged(open_complaints().filter(room=rm))})


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
                    services.move_in(tenancy, by=_who(request), deposit_mode=tform.cleaned_data["deposit_mode"])
            except ValidationError as exc:
                tform.add_error(None, exc)
            else:
                nxt = reverse("pg_tenancy", args=[tenancy.pk])
                if tenancy.status == T.RESERVED:
                    messages.success(request, f"Bed {b} reserved for {resident.name} from {tenancy.move_in:%d-%m-%Y}.")
                    return _open_message(request, tenancy, "", nxt)
                messages.success(request, f"{resident.name} moved in to bed {b}.")
                return _open_message(request, tenancy, "welcome", nxt)
    if request.method == "POST" and request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": False})  # the page script then sends the form normally, to show the errors
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
    from . import money
    from .models import PgMessage, RentPayment
    money.sync(t)
    nxt = services.next_due(t) if t.status in (T.STAYING, T.NOTICE) else None
    owed = money.balance(t)
    ctx = {
        "owed": owed, "lines": list(reversed(money.lines(t)))[:12], "payments": t.payments.all()[:30],
        "verify": t.requests.filter(status="claimed"), "open_link": t.requests.filter(status="open").first(),
        "msgs": PgMessage.objects.all(), "upi_ready": money.upi_ready(), "since": money.unpaid_since(t),
        "kinds": RentPayment.Kind.choices, "modes": [m for m in RentPayment.Mode.choices if m[0] not in RentPayment.SETTLEMENT_MODES],
        "today": today(), "due_text": due_rule_text(t),
        "t": t, "r": t.resident, "spells": t.spells.select_related("bed__room").order_by("start"), "next": nxt,
        "notice_form": NoticeForm(prefix="n"), "shift_form": ShiftForm(beds=_vacant_beds(t.bed), prefix="s"),
        "refund_form": RefundForm(prefix="f"),
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
    from . import money
    t = get_object_or_404(Tenancy.objects.select_related("resident", "bed__room"), pk=pk)
    item = current.get()
    if t.status not in (T.STAYING, T.NOTICE):
        messages.info(request, "This tenancy isn't current.")
        return redirect("pg_tenancy", pk=t.pk)
    rows = services.standard_deductions(item)
    errors = []
    day = parse_date(request.POST.get("day", "") or request.GET.get("day", "")) or t.last_day or today()
    if request.method == "POST":
        reasons, amounts = request.POST.getlist("reason"), request.POST.getlist("amount")
        rows = [{"reason": r, "amount": a} for r, a in zip(reasons, amounts) if r.strip() or a.strip()]
        try:
            ded = [{"reason": r["reason"], "amount": _amount(r["amount"])} for r in rows]
            if any(d["amount"] > 0 and not d["reason"].strip() for d in ded):
                raise ValidationError("Give a reason for each deduction.")
            waive = _amount(request.POST.get("waive", "") or "0")
            services.move_out(t, day, deductions=ded, by=_who(request), waive=waive,
                              waive_reason=request.POST.get("waive_reason", ""))
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
            return _open_message(request, t, "move_out", reverse("pg_tenancy", args=[t.pk]))
        if request.headers.get("X-Requested-With") == "fetch":
            return JsonResponse({"ok": False})
    money.sync(t)
    return render(request, "pg/move_out.html", {
        "t": t, "rows": rows + [{"reason": "", "amount": ""}] * 2, "errors": errors, "day": day.isoformat(),
        "owed": money.owed_if_leaving(t, day), "refund_days": getattr(item, "refund_days", 7),
        "waive": request.POST.get("waive", ""), "waive_reason": request.POST.get("waive_reason", ""),
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
    from core import files
    f = request.FILES.get("file")
    if not f:
        raise ValidationError("Choose a file first (photo, PDF, video…).")
    if not gdrive.is_configured():
        raise ValidationError("Google Drive isn't connected for this property, so files can't be saved yet.")
    try:
        prepared = files.prepare(f)  # photos made smaller, as for guest IDs; PDFs and videos kept as they are
    except files.BadFile as exc:
        raise ValidationError(str(exc)) from exc
    stem = (f.name.rsplit(".", 1)[0] if "." in f.name else f.name)[:80]
    file_id = gdrive.upload_file(prepared.data, f"{what}-{obj.pk}-{stem}.{prepared.ext}", prepared.mime, "pg")
    if what == "agreement":
        obj.agreement, obj.agreement_kind = file_id, prepared.kind
        obj.save(update_fields=["agreement", "agreement_kind"])
    else:
        obj.id_photo, obj.id_kind = file_id, prepared.kind
        obj.save(update_fields=["id_photo", "id_kind"])
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
    from core import files
    return files.response(gdrive.fetch(file_id), f"{what}-{pk}")


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


# ── Rent and money (phase 4) ──────────────────────────────────────────────────


def _base(request) -> str:
    from cluster.current import local
    return request.build_absolute_uri(local("/"))


def _wa_url(request, phone: str, text: str) -> str:
    from core import whatsapp
    from core.views.whatsapp import MOBILE
    return whatsapp.link(phone, text, bool(MOBILE.search(request.headers.get("User-Agent", ""))))


def _open_message(request, t, use: str, fallback, **kw):
    """After Move in / Move out: the page script opened a window and asked for JSON; send the message's WhatsApp
    address there and the next page here. Without the script, just go to the next page."""
    from . import messages as pgm
    wa = ""
    msg = pgm.for_use(use)
    if msg is not None and t.resident.phone:
        try:
            wa = _wa_url(request, t.resident.phone, pgm.build(t, msg, _base(request), by=_who(request), **kw))
        except ValidationError:
            wa = ""
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": True, "next": fallback, "wa": wa})
    return redirect(fallback)


def _parse_amount(raw) -> Decimal | None:
    try:
        value = Decimal(str(raw or "").replace(",", "").replace("₹", "").strip())
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"))


@require_POST
def rent_action(request, pk, action):
    """Collect a payment, delete one, send a payment link or a message, check a UPI payment."""
    from core.views.whatsapp import open_chat

    from . import messages as pgm
    from . import money
    from .models import PgMessage, RentPayment, RentRequest
    t = get_object_or_404(Tenancy.objects.select_related("resident", "bed__room"), pk=pk)
    back = redirect("pg_tenancy", pk=t.pk)
    try:
        if action == "pay":
            amount = _parse_amount(request.POST.get("amount"))
            kind = request.POST.get("kind", RentPayment.Kind.RENT)
            mode = request.POST.get("mode", RentPayment.Mode.CASH)
            if kind not in RentPayment.Kind.values or mode not in RentPayment.Mode.values or mode in RentPayment.SETTLEMENT_MODES:
                raise ValidationError("Pick what it is for and how it was paid.")
            p = money.record(t, amount, kind=kind, mode=mode, paid_on=parse_date(request.POST.get("paid_on", "")) or today(),
                             reference=request.POST.get("reference", ""), note=request.POST.get("note", ""),
                             by=_who(request))
            messages.success(request, f"₹{p.amount:,.0f} recorded.")
            if request.POST.get("send_receipt") and (msg := pgm.for_use(PgMessage.Use.RECEIPT)) and t.resident.phone:
                return open_chat(request, t.resident.phone, pgm.build(t, msg, _base(request), payment=p))
        elif action == "pay-delete":
            p = get_object_or_404(RentPayment, pk=request.POST.get("payment"), tenancy=t)
            if p.from_settlement:
                raise ValidationError("This was settled at move-out and can't be deleted.")
            money.delete(p)
            messages.success(request, "Payment deleted.")
        elif action in ("link-ok", "link-no"):
            req = get_object_or_404(RentRequest, pk=request.POST.get("request"), tenancy=t)
            if action == "link-ok":
                money.confirm(req, by=_who(request))
                messages.success(request, f"₹{req.amount:,.0f} by UPI recorded.")
            else:
                money.not_received(req)
                messages.info(request, "Marked as not received — the link is open again.")
        elif action == "message":
            msg = get_object_or_404(PgMessage, pk=request.POST.get("message"))
            if not t.resident.phone:
                raise ValidationError("Add the resident's phone number first.")
            amount = _parse_amount(request.POST.get("amount")) if request.POST.get("amount") else None
            text = pgm.build(t, msg, _base(request), by=_who(request), amount=amount)
            return open_chat(request, t.resident.phone, text)
        else:
            raise Http404
    except ValidationError as exc:
        _error(request, exc)
    return back


def receipt(request, pk):
    from . import receipt as rc
    from . import money
    from .models import RentPayment
    p = get_object_or_404(RentPayment.objects.select_related("tenancy__resident", "tenancy__bed__room"), pk=pk)
    resp = HttpResponse(rc.pdf(p), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="receipt-{money.receipt_no(p)}.pdf"'
    return resp


def public_receipt(request, token):
    """Public (signed link sent on WhatsApp): the resident's receipt."""
    from . import receipt as rc
    from . import money
    p = money.payment_for_token(token)
    if p is None:
        raise Http404
    resp = HttpResponse(rc.pdf(p), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="receipt-{money.receipt_no(p)}.pdf"'
    return resp


def pay_page(request, token):
    """Public (no login): the resident's UPI payment page. A wrong or altered link is a plain 404."""
    from urllib.parse import quote

    from core.payments import hotel_whatsapp
    from . import money
    from .models import RentRequest
    req = money.request_for_token(token)
    if req is None:
        raise Http404
    t = req.tenancy
    if request.method == "POST" and req.status == RentRequest.Status.OPEN and request.POST.get("paid") == "1":
        money.claim(req, request.POST.get("reference", ""))
        return redirect("pg_pay_page", token=token)
    confirm = ""
    if req.status == RentRequest.Status.CLAIMED and (number := hotel_whatsapp()):
        text = (f"Hello {current.name()}, I have paid ₹{req.amount:,.0f} by UPI for bed {t.bed} ({t.resident.name})."
                + (f" UPI reference: {req.reference}." if req.reference else ""))
        confirm = f"https://wa.me/{number}?text={quote(text)}"
    open_ = req.status == RentRequest.Status.OPEN
    upi_id, upi_name = current.upi()
    return render(request, "pg/pay.html", {
        "req": req, "t": t, "open": open_, "confirm": confirm, "upi_id": upi_id, "upi_name": upi_name,
        "upi": money.upi_uri(req) if open_ else "", "qr": money.qr_png(req) if open_ else "",
        "first_name": t.resident.name.split()[0],
    })


# ── Messages page ─────────────────────────────────────────────────────────────


def message_list(request):
    from . import messages as pgm
    from .models import PgMessage
    return render(request, "pg/messages.html", {"msgs": PgMessage.objects.all(), "fields": pgm.FIELDS})


def message_edit(request, pk=None):
    from django import forms as dj

    from .models import PgMessage

    class MessageForm(dj.ModelForm):
        class Meta:
            model = PgMessage
            fields = ["name", "use", "body", "sort_order"]
            widgets = {"body": dj.Textarea(attrs={"rows": 10})}

        def clean_use(self):
            use = self.cleaned_data["use"]
            if use and PgMessage.objects.filter(use=use).exclude(pk=self.instance.pk).exists():
                raise dj.ValidationError("Another message already has this use — clear it there first.")
            return use

    obj = get_object_or_404(PgMessage, pk=pk) if pk else None
    if request.method == "POST" and request.POST.get("action") == "delete" and obj:
        obj.delete()
        messages.success(request, "Message deleted.")
        return redirect("pg_messages")
    form = MessageForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Message saved.")
        return redirect("pg_messages")
    return render(request, "pg/form.html", {"form": form, "title": "Edit message" if obj else "New message",
                                            "back": reverse("pg_messages"), "can_delete": obj is not None,
                                            "help": "Placeholders like {first_name}, {amount_due}, {payment_link} — "
                                                    "see the list on the Messages page."})


# ── Money (behind the money password) ─────────────────────────────────────────


def revenue(request):
    from datetime import date as d_

    from core.models import Expense
    from core.views.revenue import _unlocked
    from . import money
    if not _unlocked(request):
        from cluster.current import local
        return redirect(local("/revenue/") + "?next=" + request.path)
    this = today().replace(day=1)
    try:
        y, m = (int(x) for x in request.GET.get("month", "").split("-"))
        first = d_(y, m, 1)
    except ValueError:
        first = this
    first = min(first, this)
    mo = money.month(first)
    expenses = Expense.objects.filter(date__gte=mo.start, date__lt=mo.end).aggregate(s=Sum("amount"))["s"] or Decimal(0)
    first_due = Tenancy.objects.order_by("move_in").values_list("move_in", flat=True).first() or this
    months, x = [], this
    while x >= first_due.replace(day=1) and len(months) < 36:
        months.append(x)
        x = (x - timedelta(days=1)).replace(day=1)
    from core.models import Settlement
    from core.services import partner_shares, share_text
    profit = mo.received - expenses
    settled = list(Settlement.objects.filter(month=first))
    left = max(profit - sum((s.amount for s in settled), Decimal(0)), Decimal(0))
    shares = partner_shares()
    return render(request, "pg/revenue.html", {
        "m": mo, "expenses": expenses, "profit": profit, "months": months,
        "held_deposits": money.deposits_held(), "owing": money.owing(), "settled": settled, "left": left,
        "settle": {"amount": round(left), "note": share_text(shares), "split": [(s, s.of(round(left))) for s in shares]},
    })


@require_POST
def settle(request):
    """Share out (part of) one month's profit, as on the hotel's money page."""
    from core.forms import SettlementForm
    from core.models import AuditLog, Expense
    from core.views.revenue import _unlocked
    from . import money
    if not _unlocked(request):
        return redirect("pg_revenue")
    form = SettlementForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter the amount settled (₹0 or more) for a month that has started.")
        return redirect("pg_revenue")
    first = form.cleaned_data["month"]
    mo = money.month(first)
    expenses = Expense.objects.filter(date__gte=mo.start, date__lt=mo.end).aggregate(s=Sum("amount"))["s"] or Decimal(0)
    s = form.save(commit=False)
    s.received_total, s.expenses_total = mo.received, expenses
    s.save()
    AuditLog.record("settlement", f"Settled ₹{s.amount:,.0f} for {first:%B %Y} (received ₹{mo.received:,.0f} − "
                                  f"expenses ₹{expenses:,.0f}){': ' + s.notes if s.notes else ''}")
    messages.success(request, f"Settled ₹{s.amount:,.0f} for {first:%B %Y}.")
    return redirect(reverse("pg_revenue") + f"?month={first:%Y-%m}")


@require_POST
def settlement_delete(request, pk):
    from core.models import AuditLog, Settlement
    from core.views.revenue import _unlocked
    if not _unlocked(request):
        return redirect("pg_revenue")
    s = get_object_or_404(Settlement, pk=pk)
    AuditLog.record("settlement", f"Deleted settlement for {s.month:%B %Y} (made {s.at:%d-%m-%Y}): ₹{s.amount:,.0f}")
    s.delete()
    messages.success(request, "Settlement deleted.")
    return redirect(reverse("pg_revenue") + f"?month={s.month:%Y-%m}")


# ── Complaints (§8.9) ─────────────────────────────────────────────────────────


def _places():
    """Choices for "Where": each room, then each bed in use with who is in it."""
    views = services.bed_views(_all_beds().in_use())
    rooms = {}
    for v in views:
        rooms.setdefault(v.bed.room, []).append(v)
    out = []
    for rm, beds in rooms.items():
        out.append((f"r{rm.pk}", f"Room {rm.number} (whole room)"))
        for v in beds:
            who = f" — {v.tenancy.resident.name}" if v.tenancy is not None and v.state in ("occupied", "notice") else ""
            out.append((f"b{v.bed.pk}", f"Bed {v.bed}{who}"))
    return out


def _place(value: str):
    """(room, bed) from "r12" / "b34"."""
    value = value or ""
    kind, pk = value[:1], value[1:]
    if kind == "b" and pk.isdigit():
        b = Bed.objects.select_related("room").filter(pk=pk, removed_on__isnull=True).first()
        if b:
            return b.room, b
    if kind == "r" and pk.isdigit():
        rm = Room.objects.filter(pk=pk).first()
        if rm:
            return rm, None
    raise ValidationError("Pick the room or bed.")


def complaints(request):
    from .models import Complaint, PgMessage
    show = request.GET.get("show", "open")
    qs = Complaint.objects.select_related("room", "bed__room", "tenancy__resident")
    if show == "open":
        qs = qs.exclude(status=Complaint.Status.FIXED).order_by("raised_on", "pk")
    elif show == "fixed":
        qs = qs.filter(status=Complaint.Status.FIXED).order_by("-fixed_on", "-pk")
    if (cat := request.GET.get("category", "")) in Complaint.Category.values:
        qs = qs.filter(category=cat)
    if (rm := request.GET.get("room", "")).isdigit():
        qs = qs.filter(room_id=rm)
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    for c in page:
        c.age = (today() - c.raised_on).days
    n_open = Complaint.objects.exclude(status=Complaint.Status.FIXED).count()
    return render(request, "pg/complaints.html", {
        "page": page, "show": show, "category": cat, "categories": Complaint.Category.choices, "n_open": n_open,
        "today": today(), "fixed_msg": PgMessage.objects.filter(use=PgMessage.Use.FIXED).first(),
    })


def complaint_new(request):
    from . import complaints as cs
    from .models import Complaint
    initial = request.GET.get("place", "")
    data = {"place": initial, "category": "", "details": "", "raised_on": today().isoformat(), "hold": ""}
    errors = []
    if request.method == "POST":
        data = {k: request.POST.get(k, "").strip() for k in data}
        try:
            room_, bed_ = _place(data["place"])
            if data["category"] not in Complaint.Category.values:
                raise ValidationError("Pick what the complaint is about.")
            hold = data["hold"] if data["hold"] in Complaint.Hold.values else ""
            tenancy = None
            if bed_ is not None and (v := services.bed_views([bed_])[0]).state in ("occupied", "notice"):
                tenancy = v.tenancy
            c = cs.raise_complaint(room=room_, bed=bed_, tenancy=tenancy, category=data["category"],
                                   details=data["details"], raised_on=parse_date(data["raised_on"]) or today(),
                                   hold=hold, by=_who(request))
        except ValidationError as exc:
            errors = exc.messages
        else:
            messages.success(request, f"Complaint noted: {c.get_category_display()}, {c.place.lower()}"
                                      + (" — under maintenance until fixed." if c.hold else "."))
            nxt = request.POST.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "pg_complaints")
    return render(request, "pg/complaint_form.html", {
        "places": _places(), "categories": Complaint.Category.choices, "holds": Complaint.Hold.choices[1:],
        "data": data, "errors": errors, "next": request.GET.get("next", request.POST.get("next", "")),
    })


@require_POST
def complaint_action(request, pk, action):
    from . import complaints as cs
    from . import messages as pgm
    from core.views.whatsapp import open_chat
    from .models import Complaint, PgMessage
    c = get_object_or_404(Complaint.objects.select_related("room", "bed__room", "tenancy__resident"), pk=pk)
    back = request.POST.get("next", "")
    back = redirect(back if back.startswith("/") and not back.startswith("//") else "pg_complaints")
    try:
        if action == "start":
            cs.start(c)
            messages.success(request, f"{c.get_category_display()}, {c.place.lower()}: being fixed.")
        elif action == "fix":
            cs.fix(c, fixed_by=request.POST.get("fixed_by", ""), note=request.POST.get("note", ""),
                   day=parse_date(request.POST.get("day", "")) or today())
            messages.success(request, f"{c.get_category_display()}, {c.place.lower()}: fixed.")
        elif action == "reopen":
            cs.reopen(c)
            messages.info(request, f"{c.get_category_display()}, {c.place.lower()}: open again.")
        elif action == "message":
            msg = PgMessage.objects.filter(use=PgMessage.Use.FIXED).first()
            if msg is None or c.tenancy is None or not c.tenancy.resident.phone:
                raise ValidationError("No resident's phone number for this complaint.")
            return open_chat(request, c.tenancy.resident.phone,
                             pgm.build(c.tenancy, msg, _base(request), by=_who(request), complaint=c))
        else:
            raise Http404
    except ValidationError as exc:
        _error(request, exc)
    return back


# ── Vacancy view: to share with people asking for a bed (§8.7 #7) ────────────


def vacancy(request):
    from urllib.parse import quote
    from core.whatsapp import with_footer
    groups = services.vacancies()
    item = current.get()
    text = services.vacancy_text(groups, current.name(), item)
    return render(request, "pg/vacancy.html", {
        "groups": groups, "text": text, "share": f"https://wa.me/?text={quote(with_footer(text))}", "today": today(),
        "now": sum(len(g.now) for g in groups), "soon": sum(len(g.soon) for g in groups),
    })
