from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator

from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from cluster.current import local

from ..db import atomic
from .. import gdrive, invoice, checks, payments, services, whatsapp
from ..fields import mask
from ..forms import ExtendForm, GuestForm, PaymentForm, ShortenForm, StayForm, other_guests_formset
from ..models import AuditLog, Guest, MessageTemplate, Payment, PaymentRequest, Room, Stay
from .photos import locked as photos_locked, photo_rows


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
    clash = None
    if request.method == "POST":
        # Room clash popup: "move" = move the stays in the way to other rooms; "room:<pk>" = use that room instead.
        resolve = request.POST.get("resolve", "")
        data = request.POST
        if resolve.startswith("room:") and resolve[5:].isdigit():
            data = request.POST.copy()
            data["s-room"] = resolve[5:]
        guest = _guest_instance(request, stay)
        gform = GuestForm(data, instance=guest, prefix="g", require_contact=require_contact)
        sform = StayForm(data, instance=stay or Stay(), prefix="s")
        others = other_guests_formset(sform.instance, data)
        # Only rows for guests 2…N count; rows beyond the number of guests are ignored.
        n = request.POST.get("s-num_guests", "1")
        wanted = max(0, int(n) - 1) if n.isdigit() else 0
        others_ok = wanted == 0 or (others.management_form.is_valid() and all(f.is_valid() for f in others.forms[:wanted]))
        if gform.is_valid() and sform.is_valid() and others_ok:
            new_guest, moved = gform.instance.pk is None, []
            try:
                with atomic():
                    guest = gform.save()
                    new = sform.save(commit=False)
                    new.guest = guest
                    if resolve == "move":
                        moved = services.move_blocking(new)
                    services.save_stay(new, action="update" if stay else "create",
                                       summary=f"{'Updated' if stay else 'Created'} stay: {new.room}, "
                                               f"{new.dates_label}, ₹{new.amount_paid:,.0f} paid")
                    _save_other_guests(new, others.forms if others.management_form.is_valid() else [], wanted)
                    if checking_in and new.status == Stay.Status.UPCOMING:
                        services.check_in(new)
                messages.success(request, f"{guest.name} checked in to {new.room}." if checking_in else f"Stay for {guest.name} saved.")
                for b, room in moved:
                    messages.info(request, f"{b.guest.name} ({b.dates_label}) moved to {room}.")
                for reason in checks.check_stay(new):
                    messages.warning(request, f"Please double-check: {reason}")
                if checking_in:
                    return redirect(local(f"/stays/{new.pk}/?welcome=1"))
                return redirect("stay_detail", pk=new.pk)
            except services.StayConflict as exc:
                c = sform.cleaned_data
                clash = services.plan_clash(c["room"], c["check_in"], c.get("check_out"), stay.pk if stay else None)
                sform.add_error("room", str(exc))
                if new_guest:  # its save was rolled back
                    gform.instance.pk = None
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
                   "today": services.today(), "clash": clash})


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


def _payment_message():
    """The thank-you message for a payment: the one named Payment done, else any with {amount}."""
    with_amount = [t for t in MessageTemplate.objects.all() if "{amount}" in t.body]
    return next((t for t in with_amount if t.name.strip().lower() == "payment done"),
                with_amount[0] if with_amount else None)


def _invoice_message():
    """The WhatsApp message sent with the "WhatsApp invoice" button: the one named Invoice, else any with the link."""
    with_link = [t for t in MessageTemplate.objects.all() if "{invoice_link}" in t.body]
    return next((t for t in with_link if t.name.strip().lower() == "invoice"), with_link[0] if with_link else None)


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
        "payments": list(stay.payments.all()),
        "id_photo_rows": (rows := photo_rows(stay)),
        "has_earlier_photos": any(r["earlier"] for r in rows),
        "photos_locked": photos_locked(stay),
        "has_stay_photos": stay.photo_links.exists(),
        "hosts": services.host_names(),
        "collector": request.user.first_name if request.user.first_name in services.host_names() else stay.host,
        "shorten_form": ShortenForm(initial={"new_check_out": services.today()}),
        "today": services.today(),
        "swap_with": list(services.swap_candidates(stay)),
        "pay_ready": payments.configured(),
        "pay_requests": list(stay.payment_requests.all()[:5]),
        "pay_verify": [r for r in stay.payment_requests.all() if r.status == PaymentRequest.Status.CLAIMED],
        "invoice_ok": not stay.is_void,  # any stay: automatic for Walk-in / Direct, by hand for platform bookings
        "invoice_auto": invoice.eligible(stay),
        "invoice_pending": invoice.pending(stay),
        "invoice_msgs": [t.pk for t in MessageTemplate.objects.all() if "{invoice_link}" in t.body or "{invoice_no}" in t.body],
        "invoice_no": invoice.number(stay, invoice.parts(stay, manual=True)),
        "invoice_drive": invoice.drive_link(stay),
        "invoice_at": invoice.generated_at(stay),
        "wa_invoice": _invoice_message(),
        # After check-in: the caretaker's contact as a second message (the WhatsApp message named "Caretaker …").
        "wa_caretaker": next((t for t in MessageTemplate.objects.all() if "caretaker" in t.name.lower()), None),
        "drive_ready": gdrive.is_configured(),
        "wa_templates": MessageTemplate.objects.all(),
        "extended": whatsapp.extension(stay) is not None,  # the “Stay extended” button shows once it was extended
        "last_payment": stay.payments.order_by("-date", "-pk").first(),
        "just_paid": int(request.GET["paid"]) if request.GET.get("paid", "").isdigit() else None,
        "just_checked_in": request.GET.get("welcome") == "1" and stay.status == Stay.Status.CHECKED_IN,
        "just_checked_out": request.GET.get("departed") == "1" and stay.status == Stay.Status.CHECKED_OUT,
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
                return redirect(local(f"/stays/{stay.pk}/edit/?checkin=1"))
            services.check_in(stay)
            messages.success(request, f"{stay.guest.name} checked in to {stay.room}.")
            return redirect(local(f"/stays/{stay.pk}/?welcome=1"))
        elif action == "add-rent":
            services.add_month_rent(stay)
            messages.success(request, f"Added a month’s rent ₹{stay.monthly_rent:,.0f}. Total is now ₹{stay.total:,.0f}.")
        elif action == "check-out":
            services.check_out(stay)
            messages.success(request, f"{stay.guest.name} checked out of {stay.room}.")
            invoice.on_check_out(request, stay)
            return redirect(local(f"/stays/{stay.pk}/?departed=1"))
        elif action == "cancel":
            services.cancel(stay)
            invoice.remove_from_drive(stay)
            messages.success(request, "Stay cancelled. The room is free again.")
            if stay.amount_paid:
                messages.info(request, f"₹{stay.amount_paid:,.0f} was paid on this booking — it is treated as refunded "
                                       f"and no longer counts in revenue.")
        elif action == "release-room":
            services.release_room(stay)
            messages.success(request, f"{stay.room} is free again for tonight — you can book another guest into it.")
        elif action == "undo-check-out":
            services.undo_check_out(stay, parse_date(request.POST.get("check_out") or ""))
            messages.success(request, f"Check-out undone. {stay.guest.name} is checked in to {stay.room} again.")
        elif action == "undo-check-in":
            services.undo_check_in(stay)
            messages.success(request, "Check-in undone. The stay is upcoming again.")
        elif action == "transfer":
            room = stay.room
            services.transfer_out(stay, request.POST.get("hotel", ""), request.POST.get("reason", ""))
            invoice.remove_from_drive(stay)
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
                if request.headers.get("X-Requested-With") == "fetch":
                    return JsonResponse({"ok": False, "wa": ""})
            else:
                c = form.cleaned_data
                services.add_payment(stay, c["amount"], c["mode"], c["collected_by"], c["date"], c["note"])
                messages.success(request, f"Payment of ₹{c['amount']:,.0f} recorded.")
                req = stay.payment_requests.filter(pk=request.POST.get("pay_request") or 0).first()
                if req:  # the guest's "I have paid", now checked in the UPI app
                    payments.mark_paid(req)
                if request.headers.get("X-Requested-With") == "fetch":  # stay page: WhatsApp opens with the thanks
                    t = _payment_message()
                    wa = (local(f"/stays/{stay.pk}/whatsapp/?t={t.pk}&amount={c['amount']:.0f}")
                          if t and stay.guest.phone else "")
                    return JsonResponse({"ok": True, "wa": wa})
                if not request.POST.get("next"):
                    return redirect(local(f"/stays/{stay.pk}/?paid={c['amount']:.0f}#payment"))
        elif action == "pay-not-received":
            req = get_object_or_404(PaymentRequest, pk=request.POST.get("pay_request") or 0, stay=stay)
            payments.not_received(req)
            messages.info(request, "Marked as not received — the guest’s payment link works again.")
        elif action == "swap":
            other = get_object_or_404(Stay.objects.select_related("guest", "room"), pk=request.POST.get("other") or 0)
            services.swap_rooms(stay, other)
            messages.success(request, f"Rooms swapped: {stay.guest.name} is now in {stay.room}, "
                                      f"{other.guest.name} in {other.room}.")
        elif action == "make-invoice":
            if stay.is_void:
                messages.error(request, "A cancelled or transferred stay has no invoice.")
            else:
                again = bool(invoice.generated_at(stay))
                result = invoice.generate(stay, manual=True)  # platform bookings: the whole stay, by hand
                no = invoice.number(stay)
                AuditLog.record("invoice", f"Invoice {no} {'updated' if again else 'generated'}", stay)
                done = "updated" if again else "generated"
                if result == "saved":
                    messages.success(request, f"Invoice {no} {done} and saved in Google Drive"
                                              f"{' (same file)' if again else ''}.")
                elif result == "no-drive":
                    messages.success(request, f"Invoice {no} {done}. (Google Drive isn’t connected, so it isn’t "
                                              f"saved there.)")
                else:
                    messages.warning(request, f"Invoice {no} {done}, but couldn’t be saved to Google Drive — "
                                              f"please try Update invoice again.")
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
    nxt = request.POST.get("next", "")  # e.g. "Add month's rent" tapped on the Today page
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
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
    fetch = request.headers.get("X-Requested-With") == "fetch"  # page script: opens the "Stay extended" message
    if request.method == "POST":
        form = ExtendForm(request.POST, stay=stay)
        old_co, before = stay.check_out, whatsapp.due(stay)
        if not form.is_valid() and fetch:
            return JsonResponse({"ok": False})  # the script sends the form again normally, to show what's wrong
        if form.is_valid():
            new_co = form.cleaned_data["new_check_out"]
            money = (form.cleaned_data["amount"], form.cleaned_data["paid_now"] or 0, form.cleaned_data["payment_mode"],
                     form.cleaned_data.get("via", "direct") == "direct")
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
                    return _extended(request, stay, old_co, new_co, money[0], before, stay.root.pk, fetch)
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
                    return _extended(request, stay, old_co, new_co, money[0], before, stay.pk, fetch)
                if fetch:
                    return JsonResponse({"ok": False})  # choices to make (other room): shown by the normal page
            except (services.StayConflict, ValueError) as exc:
                if fetch:
                    return JsonResponse({"ok": False})
                messages.error(request, f"{exc} Please choose again.")
                plan = services.plan_extension(stay, new_co)
    else:
        form = ExtendForm(stay=stay)
    quick = [(n, stay.check_out + timedelta(days=n)) for n in (1, 2, 7)]
    return render(request, "core/extend.html", {"stay": stay, "form": form, "plan": plan, "quick": quick})


@require_POST
def _extended(request, stay, old_co, new_co, amount, before, show_pk, fetch):
    """After an extension: remember it for the “Stay extended” message, and open that message (page script)."""
    stay.refresh_from_db()
    whatsapp.record_extension(stay, old_co, new_co, amount, before)
    template = whatsapp.extension_message()
    last = [s for s in stay.chain() if not s.is_void][-1]  # after a room move, the room the guest is in now
    wa = local(f"/stays/{last.pk}/whatsapp/?t={template.pk}") if template and stay.guest.phone else ""
    if fetch:
        return JsonResponse({"ok": True, "next": local(f"/stays/{show_pk}/"), "wa": wa})
    if wa:
        messages.info(request, "Tap “Stay extended” under WhatsApp to send the guest the new dates and amount.")
    return redirect("stay_detail", pk=show_pk)


def payment_delete(request, pk, payment_pk):
    payment = get_object_or_404(Payment.objects.select_related("stay"), pk=payment_pk, stay_id=pk)
    services.delete_payment(payment)
    messages.success(request, f"Payment of ₹{payment.amount:,.0f} from {payment.date:%d-%m-%Y} deleted.")
    return redirect("stay_detail", pk=pk)
