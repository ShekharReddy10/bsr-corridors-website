from django.contrib.auth.views import LogoutView
from django.urls import path

from .views import auth, calendar, dashboard, data, guests, rooms, stays

urlpatterns = [
    path("login/", auth.ThrottledLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("healthz/", auth.healthz, name="healthz"),
    path("", dashboard.dashboard, name="dashboard"),
    path("calendar/", calendar.tape_chart, name="calendar"),
    path("calendar/room/<int:pk>/", calendar.room_month, name="room_month"),
    path("stays/", stays.stay_list, name="stay_list"),
    path("stays/new/", stays.stay_form, name="stay_new"),
    path("upcoming/", stays.upcoming, name="upcoming"),
    path("stays/<int:pk>/", stays.stay_detail, name="stay_detail"),
    path("stays/<int:pk>/edit/", stays.stay_form, name="stay_edit"),
    path("stays/<int:pk>/extend/", stays.extend, name="stay_extend"),
    path("stays/<int:pk>/reveal-id/", stays.reveal_id, name="stay_reveal_id"),
    path("stays/<int:pk>/<slug:action>/", stays.stay_action, name="stay_action"),
    path("guests/", guests.guest_list, name="guest_list"),
    path("guests/<int:pk>/", guests.guest_detail, name="guest_detail"),
    path("guests/lookup/", stays.guest_lookup, name="guest_lookup"),
    path("rooms/", rooms.rooms_home, name="rooms"),
    path("rooms/type/new/", rooms.edit, {"kind": "type"}, name="roomtype_new"),
    path("rooms/type/<int:pk>/", rooms.edit, {"kind": "type"}, name="roomtype_edit"),
    path("rooms/room/new/", rooms.edit, {"kind": "room"}, name="room_new"),
    path("rooms/room/<int:pk>/", rooms.edit, {"kind": "room"}, name="room_edit"),
    path("rooms/<str:kind>/<int:pk>/delete/", rooms.delete, name="room_delete"),
    path("export/", data.export, name="export"),
    path("backups/", data.backups, name="backups"),
    path("backup/run/", data.backup_hook, name="backup_hook"),
    path("settings/", data.settings_page, name="settings"),
]
