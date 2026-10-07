from django.contrib import messages
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import RoomForm, RoomTypeForm
from ..models import AuditLog, Room, RoomType

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
