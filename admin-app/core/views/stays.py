from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.utils.html import escape
from django.views.decorators.http import require_POST

from .. import services
from ..fields import mask
from ..forms import ExtendForm, GuestForm, PaymentForm, ShortenForm, StayForm, other_guests_formset
from ..models import AuditLog, Guest, Room, Stay


def stay_list(request):
    stays = Stay.objects.select_related("guest", "room", "room__room_type")
    q = request.GET.get("q", "").strip()
    if q:
        digits = "".join(c for c in q if c.isdigit())
        cond = Q(guest__name__icontains=q)
        if len(digits) >= 3:
            cond |= Q(guest__phone__icontains=digits[-10:]) | Q(guest__phone__icontains=q)
        stays = stays.filter(cond)
    status = request.GET.get("status", "")
    if status in Stay.Status.values:
        stays = stays.filter(status=status)
    room = request.GET.get("room", "")
    if room.isdigit():
        stays = stays.filter(room_id=int(room))
    source = request.GET.get("source", "")
    if source in Stay.Source.values:
        stays = stays.filter(source=source)
    start, end = parse_date(request.GET.get("start") or ""), parse_date(request.GET.get("end") or "")
    if start:
        stays = stays.filter(Q(check_out__gt=start) | Q(check_out__isnull=True))
    if end:
        stays = stays.filter(check_in__lte=end)

    page = Paginator(stays.order_by("-check_in", "room__number"), 25).get_page(request.GET.get("page"))
    context = {"page": page, "q": q, "status": status, "room": room, "source": source, "start": start, "end": end,
               "statuses": Stay.Status.choices, "sources": Stay.Source.choices, "rooms": Room.objects.all()}
    template = "partials/stay_rows.html" if request.headers.get("HX-Request") else "core/stay_list.html"
    return render(request, template, context)


def guest_lookup(request):
    """Returning-guest autofill: the guest saved under this phone number (last 10 digits)."""
    phone = request.GET.get("phone", "")
    if sum(c.isdigit() for c in phone) < 10:
        return JsonResponse({"found": False})
    g = Guest.objects.filter(phone_key=Guest.key_for(phone)).first()
    if g:
        last = g.stays.order_by("-check_in").first()
        return JsonResponse({
            "found": True,
            "guest_id": g.pk,
            "name": g.name,
            "address": g.address,
            "nationality": g.nationality,
            "id_type": g.id_type,
            "id_masked": mask(g.id_number),
            "passport_masked": mask(g.passport_number),
            "visa_number": g.visa_number,
            "visa_type": g.visa_type,
            "arrival_in_india": g.arrival_in_india.isoformat() if g.arrival_in_india else "",
            "stays": g.stays.count(),
            "last_stay": f"{last.check_in:%d-%m-%Y}" if last else "",
        })
    return JsonResponse({"found": False})


def _guest_instance(request, stay: Stay | None):
    """Edit the stay's guest, or a returning guest picked by phone autofill, or a new guest."""
    if stay:
        return stay.guest
    guest_id = request.POST.get("g-guest_id", "")
    if guest_id.isdigit():
        return Guest.objects.filter(pk=int(guest_id)).first()
    return None


def stay_form(request, pk=None):
    stay = get_object_or_404(Stay.objects.select_related("guest", "room"), pk=pk) if pk else None
    # Checking in (or already in-house): the guest's phone and address are required.
    checking_in = (request.GET.get("checkin") or request.POST.get("checkin")) == "1"
    require_contact = checking_in or (stay is not None and stay.status == Stay.Status.CHECKED_IN)
    if request.method == "POST":
        guest = _guest_instance(request, stay)
        gform = GuestForm(request.POST, instance=guest, prefix="g", require_contact=require_contact)
        sform = StayForm(request.POST, instance=stay or Stay(), prefix="s")
        others = other_guests_formset(sform.instance, request.POST)
        # Only rows for guests 2…N count; rows beyond the number of guests are ignored.
        n = request.POST.get("s-num_guests", "1")
        wanted = max(0, int(n) - 1) if n.isdigit() else 0
        others_ok = wanted == 0 or (others.management_form.is_valid() and all(f.is_valid() for f in others.forms[:wanted]))
        if gform.is_valid() and sform.is_valid() and others_ok:
            try:
                with transaction.atomic():
                    guest = gform.save()
                    new = sform.save(commit=False)
                    new.guest = guest
                    services.save_stay(new, action="update" if stay else "create",
                                       summary=f"{'Updated' if stay else 'Created'} stay: {new.room}, "
                                               f"{new.dates_label}, ₹{new.amount_paid:,.0f} paid")
                    _save_other_guests(new, others.forms if others.management_form.is_valid() else [], wanted)
                    if checking_in and new.status == Stay.Status.UPCOMING:
                        services.check_in(new)
                messages.success(request, f"{guest.name} checked in to {new.room}." if checking_in else f"Stay for {guest.name} saved.")
                return redirect("stay_detail", pk=new.pk)
            except services.StayConflict as exc:
                sform.add_error("room", str(exc))
    else:
        initial = {}
        if not stay:
            room = Room.objects.filter(pk=request.GET.get("room") or 0).select_related("room_type").first()
            check_in = parse_date(request.GET.get("check_in") or "") or services.today()
            initial = {"check_in": check_in, "check_out": check_in + timedelta(days=1), "num_guests": 1}
            if room:
                initial["room"] = room
        guest = stay.guest if stay else Guest.objects.filter(pk=request.GET.get("guest") or 0).first()
        gform = GuestForm(instance=guest, prefix="g", require_contact=require_contact)
        sform = StayForm(instance=stay, initial=initial, prefix="s")
        others = other_guests_formset(stay or Stay())
    return render(request, "core/stay_form.html",
                  {"stay": stay, "gform": gform, "sform": sform, "others": others, "checking_in": checking_in,
                   "today": services.today()})


def upcoming(request):
    """Every future booking, grouped by arrival date."""
    t = services.today()
    stays = (Stay.objects.filter(status=Stay.Status.UPCOMING).select_related("guest", "room", "room__room_type")
             .order_by("check_in", "room__number"))
    return render(request, "core/upcoming.html", {
        "late": [s for s in stays if s.check_in < t],
        "future": [s for s in stays if s.check_in >= t],
        "today": t,
    })


def _save_other_guests(stay: Stay, forms, wanted: int) -> None:
    """Save rows for guests 2…(wanted+1); remove blank rows and any beyond the number of guests."""
    kept, position = [], 2
    for f in forms[:wanted]:
        if f.is_blank:
            continue
        obj = f.save(commit=False)
        obj.stay, obj.position = stay, position
        obj.save()
        kept.append(obj.pk)
        position += 1
    stay.other_guests.exclude(pk__in=kept).delete()


def stay_detail(request, pk):
    stay = get_object_or_404(Stay.objects.select_related("guest", "room", "room__room_type", "linked_to"), pk=pk)
    chain = stay.chain()
    context = {
        "stay": stay,
        "chain": chain if len(chain) > 1 else [],
        "chain_total": sum(s.total for s in chain),
        "chain_paid": sum(s.amount_paid for s in chain),
        "chain_balance": sum(s.balance for s in chain),
        "logs": AuditLog.objects.filter(stay__in=chain).order_by("-at")[:50],
        "payment_form": PaymentForm(),
        "shorten_form": ShortenForm(initial={"new_check_out": services.today()}),
        "today": services.today(),
    }
    if stay.status == Stay.Status.CHECKED_OUT:
        context["booked_check_out"] = services.booked_check_out(stay)
    return render(request, "core/stay_detail.html", context)


@require_POST
def stay_action(request, pk, action):
    stay = get_object_or_404(Stay.objects.select_related("guest", "room"), pk=pk)
    try:
        if action == "check-in":
            if not stay.guest.has_contact_details:
                messages.info(request, f"Add {stay.guest.name}’s phone and address to check in.")
                return redirect(f"/stays/{stay.pk}/edit/?checkin=1")
            services.check_in(stay)
            messages.success(request, f"{stay.guest.name} checked in to {stay.room}.")
        elif action == "add-rent":
            services.add_month_rent(stay)
            messages.success(request, f"Added a month’s rent ₹{stay.monthly_rent:,.0f}. Total is now ₹{stay.total:,.0f}.")
        elif action == "check-out":
            services.check_out(stay)
            messages.success(request, f"{stay.guest.name} checked out of {stay.room}.")
        elif action == "cancel":
            services.cancel(stay)
            messages.success(request, "Stay cancelled. The room is free again.")
        elif action == "undo-check-out":
            services.undo_check_out(stay, parse_date(request.POST.get("check_out") or ""))
            messages.success(request, f"Check-out undone. {stay.guest.name} is checked in to {stay.room} again.")
        elif action == "undo-check-in":
            services.undo_check_in(stay)
            messages.success(request, "Check-in undone. The stay is upcoming again.")
        elif action == "transfer":
            room = stay.room
            services.transfer_out(stay, request.POST.get("hotel", ""), request.POST.get("reason", ""))
            messages.success(request, f"{stay.guest.name} transferred to {stay.transferred_to}. {room} is free again.")
        elif action == "undo-cancel":
            was = "Transferred" if stay.status == Stay.Status.TRANSFERRED else "Cancelled"
            stay.status = Stay.Status.UPCOMING
            stay.transferred_to = stay.transfer_reason = ""
            services.save_stay(stay, action="restore", summary=f"{was} stay restored")
            messages.success(request, "Stay restored.")
        elif action == "form-c":
            stay.form_c_filed = not stay.form_c_filed
            services.save_stay(stay, action="form_c", summary=f"Form C marked {'filed' if stay.form_c_filed else 'not filed'}")
        elif action == "payment":
            form = PaymentForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Enter a valid payment amount.")
            else:
                services.add_payment(stay, form.cleaned_data["amount"], form.cleaned_data["mode"])
                messages.success(request, f"Payment of ₹{form.cleaned_data['amount']:,.0f} recorded.")
        elif action == "shorten":
            form = ShortenForm(request.POST)
            if form.is_valid():
                services.shorten(stay, form.cleaned_data["new_check_out"])
                messages.success(request, "Stay shortened.")
            else:
                messages.error(request, "Pick a valid date.")
        elif action == "delete":
            label = str(stay)
            AuditLog.record("delete", f"Deleted stay: {label}")
            stay.delete()
            messages.success(request, "Stay deleted.")
            return redirect("stay_list")
        else:
            raise Http404
    except (services.StayConflict, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect("stay_detail", pk=stay.pk)


@require_POST
def reveal_id(request, pk):
    """Show a guest's full ID number (logged)."""
    stay = get_object_or_404(Stay.objects.select_related("guest"), pk=pk)
    field = request.POST.get("field", "id_number")
    if field.startswith("other:"):
        other = get_object_or_404(stay.other_guests, pk=field.split(":", 1)[1])
        AuditLog.record("reveal_id", f"Viewed full ID number of guest {other.position} ({other.name or 'no name'})", stay)
        return HttpResponse(f'<span class="id-full">{escape(other.id_number)}</span>')
    if field not in {"id_number", "passport_number"}:
        raise Http404
    AuditLog.record("reveal_id", f"Viewed full {field.replace('_', ' ')} of {stay.guest.name}", stay)
    return HttpResponse(f'<span class="id-full">{escape(getattr(stay.guest, field))}</span>')


def extend(request, pk):
    stay = get_object_or_404(Stay.objects.select_related("guest", "room", "room__room_type"), pk=pk)
    if not stay.is_active:
        messages.error(request, "Only upcoming or checked-in stays can be extended.")
        return redirect("stay_detail", pk=pk)
    if stay.is_open_ended:
        messages.info(request, "This is a monthly stay with no check-out yet — use “Add month’s rent”, or set a leaving date.")
        return redirect("stay_detail", pk=pk)

    plan = None
    if request.method == "POST":
        form = ExtendForm(request.POST, stay=stay)
        if form.is_valid():
            new_co = form.cleaned_data["new_check_out"]
            money = (form.cleaned_data["amount"], form.cleaned_data["paid_now"] or 0, form.cleaned_data["payment_mode"])
            choice = request.POST.get("choice", "")
            try:
                if choice in {"split", "move"}:
                    room = get_object_or_404(Room, pk=request.POST.get("room"))
                    if choice == "split":
                        cont = services.extend_split(stay, new_co, room, *money)
                        messages.success(request, f"Extended: {stay.guest.name} moves to {room} on {cont.check_in:%d-%m-%Y}.")
                    else:
                        move_start = min(max(stay.check_in, services.today()), stay.check_out)
                        services.extend_move(stay, new_co, room, move_start, *money)
                        messages.success(request, f"Extended: {stay.guest.name} moved to {room}, until {new_co:%d-%m-%Y}.")
                    return redirect("stay_detail", pk=stay.root.pk)
                plan = services.plan_extension(stay, new_co)
                if plan.can_extend_in_place:
                    nights = f"+{plan.extra_nights} night{'s' if plan.extra_nights != 1 else ''}"
                    if plan.free:
                        services.extend_in_place(stay, new_co, *money)
                        messages.success(request, f"Extended in {stay.room} until {new_co:%d-%m-%Y} ({nights}).")
                    else:
                        moved = [f"{b.guest.name}’s booking moved from {b.room} to {r}" for b, r in plan.moves]
                        services.extend_with_moves(stay, new_co, plan.moves, *money)
                        messages.success(request, f"Extended in {stay.room} until {new_co:%d-%m-%Y} ({nights}). "
                                                  + "; ".join(moved) + ".")
                    return redirect("stay_detail", pk=stay.pk)
            except (services.StayConflict, ValueError) as exc:
                messages.error(request, f"{exc} Please choose again.")
                plan = services.plan_extension(stay, new_co)
    else:
        form = ExtendForm(stay=stay)
    quick = [(n, stay.check_out + timedelta(days=n)) for n in (1, 2, 7)]
    return render(request, "core/extend.html", {"stay": stay, "form": form, "plan": plan, "quick": quick})
