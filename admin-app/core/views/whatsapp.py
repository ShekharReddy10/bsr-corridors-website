"""WhatsApp messages: ready-made texts, hotel info (Wi-Fi, rules, timings) and click-to-send links."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib import messages

from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cluster.current import local

from ..db import atomic
from .. import invoice, whatsapp
from ..forms import MessageTemplateForm
from ..models import AuditLog, MessageTemplate, Stay

MOBILE = re.compile(r"Android|iPhone|iPad|iPod|Mobile", re.I)


class AppRedirect(HttpResponseRedirect):
    allowed_schemes = ["https", "whatsapp"]


def open_chat(request, phone: str, text: str):
    """Open the chat with the message filled in: the WhatsApp app on a phone, WhatsApp Web on a computer.
    From a computer's page script (X-Requested-With: fetch) it answers {url, text} instead, so the page can also copy
    the message — when WhatsApp Web is already open (or installed as an app) it sometimes doesn't fill it in."""
    mobile = bool(MOBILE.search(request.headers.get("User-Agent", "")))
    url = whatsapp.link(phone, text, mobile)
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"url": url, "text": text})
    return AppRedirect(url)


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
        with atomic():
            saved = form.save()
            # Only one message opens at check-in, and one at check-out.
            if saved.on_check_in:
                MessageTemplate.objects.exclude(pk=saved.pk).update(on_check_in=False)
            if saved.on_check_out:
                MessageTemplate.objects.exclude(pk=saved.pk).update(on_check_out=False)
            if saved.shortcut:  # one message per Today-page list
                MessageTemplate.objects.exclude(pk=saved.pk).filter(shortcut=saved.shortcut).update(shortcut="")
        AuditLog.record("whatsapp", f"{'Updated' if obj else 'Added'} WhatsApp message: {saved}")
        messages.success(request, f"Message “{saved}” saved.")
        return redirect("whatsapp")
    return render(request, "core/simple_form.html", {
        "form": form,
        "title": f"{'Edit' if obj else 'Add'} WhatsApp message",
        "back": "whatsapp",
        "delete_url": local(f"/whatsapp/messages/{obj.pk}/delete/") if obj else "",
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
        "preview": whatsapp.with_footer(whatsapp.render(body, whatsapp.context(with_phone[0], base_url=request.build_absolute_uri(local("/"))))) if body and with_phone else "",
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
    try:
        amount = Decimal(request.GET.get("amount") or "x")
    except InvalidOperation:
        amount = None
    if t:
        templates = MessageTemplate.objects.all()
        if t in AUTO:
            template = templates.filter(**{AUTO[t]: True}).first()
        else:
            template = templates.filter(pk=int(t)).first() if t.isdigit() else None
        if not template:
            messages.error(request, "That message no longer exists.")
            return redirect("stay_detail", pk=pk)
        if "{payment_link}" in template.body:  # needs an amount and a new link: sent from the Payment section
            messages.info(request, "Use “Send payment link” in the Payment section to send a payment link.")
            return redirect("stay_detail", pk=pk)
        if any(f in template.body for f in whatsapp.EXT_FIELDS) and not whatsapp.extension(stay):
            messages.info(request, "This stay hasn’t been extended yet — the “Stay extended” message opens after "
                                   "you extend it.")
            return redirect("stay_detail", pk=pk)
        if "{invoice_link}" in template.body or "{invoice_no}" in template.body:
            invoice.ensure(stay)  # departure / check-out details: make the invoice first, so the link goes with it
        name, text = template.name, whatsapp.render(template.body, whatsapp.context(stay, amount, request.build_absolute_uri(local("/"))))
    else:
        text = whatsapp.render(request.GET.get("text", ""), whatsapp.context(stay, amount, request.build_absolute_uri(local("/"))))  # {guest}, {room}, … allowed
        name = "custom message" if text else "chat"
    text = whatsapp.with_footer(text)
    AuditLog.record("whatsapp", f"WhatsApp {name} opened for {stay.guest.name}", stay)
    return open_chat(request, stay.guest.phone, text)
