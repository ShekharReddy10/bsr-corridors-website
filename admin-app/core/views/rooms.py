from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from ..forms import MaintenanceLogForm, RoomForm, RoomTypeForm
from ..models import AuditLog, Expense, MaintenanceLog, Room, RoomType
from ..services import today

KINDS = {
    "type": (RoomType, RoomTypeForm, "room type"),
    "room": (Room, RoomForm, "room"),
}


def rooms_home(request):
    return render(request, "core/rooms.html", {
        "types": RoomType.objects.annotate(room_count=Count("rooms")),
        "rooms": Room.objects.select_related("room_type").annotate(stay_count=Count("stays")),
    })


def edit(request, kind, pk=None):
    model, form_class, label = KINDS[kind]
    obj = get_object_or_404(model, pk=pk) if pk else None
    if kind == "room" and not obj and not RoomType.objects.filter(is_active=True).exists():
        messages.info(request, "Add a room type first.")
        return redirect("roomtype_new")
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        AuditLog.record("rooms", f"{'Updated' if obj else 'Added'} {label}: {saved}")
        messages.success(request, f"{label.capitalize()} “{saved}” saved.")
        return redirect("rooms")
    return render(request, "core/simple_form.html", {
        "form": form,
        "title": f"{'Edit' if obj else 'Add'} {label}",
        "back": "rooms",
        "delete_url": f"/rooms/{kind}/{obj.pk}/delete/" if obj else "",
    })


@require_POST
def delete(request, kind, pk):
    if kind not in KINDS:
        raise Http404
    model, _, label = KINDS[kind]
    obj = get_object_or_404(model, pk=pk)
    in_use = obj.stays.exists() if kind == "room" else obj.rooms.exists()
    if in_use:
        obj.is_active = False
        obj.save(update_fields=["is_active"])
        AuditLog.record("rooms", f"Deactivated {label}: {obj}")
        messages.info(request, f"“{obj}” has history, so it was deactivated instead of deleted.")
    else:
        AuditLog.record("rooms", f"Deleted {label}: {obj}")
        obj.delete()
        messages.success(request, f"{label.capitalize()} deleted.")
    return redirect("rooms")


def room_log(request, pk):
    """A room's upkeep: services (with the next one due), repairs and problems."""
    room = get_object_or_404(Room, pk=pk)
    form = MaintenanceLogForm(request.POST or None, initial={"date": today()})
    if request.method == "POST" and form.is_valid():
        entry = form.save(commit=False)
        entry.room = room
        entry.done = entry.kind == MaintenanceLog.Kind.REPAIR
        with transaction.atomic():
            entry.save()
            if entry.kind == MaintenanceLog.Kind.SERVICE:  # a new service replaces the earlier "next due" reminders
                room.maintenance.filter(kind=MaintenanceLog.Kind.SERVICE, done=False).exclude(pk=entry.pk).update(done=True)
            if entry.cost and form.cleaned_data["add_expense"]:
                Expense.objects.create(date=entry.date, category=Expense.Category.REPAIRS, amount=entry.cost,
                                       reason=f"{entry.what} — room {room.number}", room=room,
                                       notes="Added from the room’s maintenance log")
            AuditLog.record("maintenance", f"{room}: {entry.get_kind_display()} — {entry.what}")
        messages.success(request, "Saved to the room’s log.")
        return redirect("room_log", pk=pk)
    return render(request, "core/room_log.html", {"room": room, "form": form, "entries": room.maintenance.all()})


@require_POST
def log_done(request, pk):
    entry = get_object_or_404(MaintenanceLog, pk=pk)
    entry.done = True
    entry.save(update_fields=["done"])
    AuditLog.record("maintenance", f"{entry.room}: marked done — {entry.what}")
    messages.success(request, f"Marked done: {entry.what} ({entry.room}).")
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect("room_log", pk=entry.room_id)


@require_POST
def log_delete(request, pk):
    entry = get_object_or_404(MaintenanceLog, pk=pk)
    AuditLog.record("maintenance", f"{entry.room}: deleted log entry — {entry.what}")
    entry.delete()
    return redirect("room_log", pk=entry.room_id)
