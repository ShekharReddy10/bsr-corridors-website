"""WhatsApp messages: ready-made texts, hotel info (Wi-Fi, rules, timings) and click-to-send links."""

import re
from urllib.parse import urlencode

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import whatsapp
from ..forms import MessageTemplateForm
from ..models import AuditLog, MessageTemplate, Stay

MOBILE = re.compile(r"Android|iPhone|iPad|iPod|Mobile", re.I)


class AppRedirect(HttpResponseRedirect):
    allowed_schemes = ["https", "whatsapp"]


# ?t=checkin / ?t=checkout → the message that opens at check-in / check-out.
AUTO = {"checkin": "on_check_in", "checkout": "on_check_out"}


def whatsapp_page(request):
    if request.method == "POST":
        whatsapp.save_hotel_info(request.POST)
        AuditLog.record("whatsapp", "Updated hotel info for WhatsApp messages")
        messages.success(request, "Saved.")
        return redirect("whatsapp")
    info = whatsapp.hotel_info()
    return render(request, "core/whatsapp.html", {
        "info": [(key, label, info[key]) for key, (label, _) in whatsapp.HOTEL_INFO.items()],
        "templates": MessageTemplate.objects.all(),
        "stay_fields": whatsapp.STAY_FIELDS.items(),
    })


def template_edit(request, pk=None):
    obj = get_object_or_404(MessageTemplate, pk=pk) if pk else None
    form = MessageTemplateForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save()
            # Only one message opens at check-in, and one at check-out.
            if saved.on_check_in:
                MessageTemplate.objects.exclude(pk=saved.pk).update(on_check_in=False)
            if saved.on_check_out:
                MessageTemplate.objects.exclude(pk=saved.pk).update(on_check_out=False)
            if saved.on_departing:
                MessageTemplate.objects.exclude(pk=saved.pk).update(on_departing=False)
        AuditLog.record("whatsapp", f"{'Updated' if obj else 'Added'} WhatsApp message: {saved}")
        messages.success(request, f"Message “{saved}” saved.")
        return redirect("whatsapp")
    return render(request, "core/simple_form.html", {
        "form": form,
        "title": f"{'Edit' if obj else 'Add'} WhatsApp message",
        "back": "whatsapp",
        "delete_url": f"/whatsapp/messages/{obj.pk}/delete/" if obj else "",
    })


@require_POST
def template_delete(request, pk):
    obj = get_object_or_404(MessageTemplate, pk=pk)
    AuditLog.record("whatsapp", f"Deleted WhatsApp message: {obj}")
    obj.delete()
    messages.success(request, "Message deleted.")
    return redirect("whatsapp")


def in_house(request):
    """One message to every guest staying now: pick a ready-made message (or type one), then send it guest by
    guest — each tap opens the next guest's chat with the text filled in for them (WhatsApp needs one Send each)."""
    stays = list(Stay.objects.filter(status=Stay.Status.CHECKED_IN).select_related("guest", "room")
                 .order_by("room__sort_order", "room__number"))
    templates = MessageTemplate.objects.all()
    t, text = request.GET.get("t", ""), request.GET.get("text", "").strip()
    template = templates.filter(pk=int(t)).first() if t.isdigit() else None
    body = template.body if template else text
    query = urlencode({"t": template.pk} if template else {"text": text}) if body else ""
    with_phone = [s for s in stays if s.guest.phone]
    return render(request, "core/whatsapp_in_house.html", {
        "templates": templates, "template": template, "text": text, "query": query,
        "with_phone": with_phone, "no_phone": [s for s in stays if not s.guest.phone],
        "preview": whatsapp.render(body, whatsapp.context(with_phone[0])) if body and with_phone else "",
    })


def send(request, pk):
    """Fill in a message for this stay's guest and open it in WhatsApp (logged in the stay's history).

    ?t=<id> a saved message · ?t=checkin / ?t=checkout the check-in (welcome) / check-out (departure)
    message · ?text=… a custom message · nothing: just open the chat.
    """
    stay = get_object_or_404(Stay.objects.select_related("guest", "room"), pk=pk)
    if not stay.guest.phone:
        messages.error(request, f"Add {stay.guest.name}’s phone number to send WhatsApp messages.")
        return redirect("stay_detail", pk=pk)
    t = request.GET.get("t", "")
    if t:
        templates = MessageTemplate.objects.all()
        if t in AUTO:
            template = templates.filter(**{AUTO[t]: True}).first()
        else:
            template = templates.filter(pk=int(t)).first() if t.isdigit() else None
        if not template:
            messages.error(request, "That message no longer exists.")
            return redirect("stay_detail", pk=pk)
        name, text = template.name, whatsapp.render(template.body, whatsapp.context(stay))
    else:
        text = whatsapp.render(request.GET.get("text", ""), whatsapp.context(stay))  # {guest}, {room}, … allowed
        name = "custom message" if text else "chat"
    AuditLog.record("whatsapp", f"WhatsApp {name} opened for {stay.guest.name}", stay)
    mobile = bool(MOBILE.search(request.headers.get("User-Agent", "")))
    return AppRedirect(whatsapp.link(stay.guest.phone, text, mobile))
