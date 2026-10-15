"""WhatsApp messages: ready-made texts, hotel info (Wi-Fi, rules, timings) and click-to-send links."""

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import whatsapp
from ..forms import MessageTemplateForm
from ..models import AuditLog, MessageTemplate, Stay


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
            if saved.on_check_in:  # only one message opens at check-in
                MessageTemplate.objects.exclude(pk=saved.pk).update(on_check_in=False)
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


def send(request, pk):
    """Fill in a message for this stay's guest and open it in WhatsApp (logged in the stay's history).

    ?t=<id> a saved message · ?t=checkin the check-in (welcome) message · ?text=… a custom message.
    """
    stay = get_object_or_404(Stay.objects.select_related("guest", "room"), pk=pk)
    if not stay.guest.phone:
        messages.error(request, f"Add {stay.guest.name}’s phone number to send WhatsApp messages.")
        return redirect("stay_detail", pk=pk)
    t = request.GET.get("t", "")
    if t:
        templates = MessageTemplate.objects.all()
        template = templates.filter(on_check_in=True).first() if t == "checkin" else (
            templates.filter(pk=int(t)).first() if t.isdigit() else None)
        if not template:
            messages.error(request, "That message no longer exists.")
            return redirect("stay_detail", pk=pk)
        name, text = template.name, whatsapp.render(template.body, whatsapp.context(stay))
    else:
        name, text = "custom message", request.GET.get("text", "").strip()
    AuditLog.record("whatsapp", f"WhatsApp {name} opened for {stay.guest.name}", stay)
    return redirect(whatsapp.link(stay.guest.phone, text))
