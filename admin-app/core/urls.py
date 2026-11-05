from django.contrib.auth.views import LogoutView
from django.urls import path, register_converter

from .views import auth, calendar, dashboard, data, expenses, guests, photos, revenue, rooms, stays, users, whatsapp


class TwoDigits:
    """Month in /revenue/2026-01/."""
    regex = "[0-9]{2}"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return f"{int(value):02d}"


register_converter(TwoDigits, "mm")

urlpatterns = [
    path("login/", auth.ThrottledLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("healthz/", auth.healthz, name="healthz"),
    path("", dashboard.dashboard, name="dashboard"),
    path("checks/ok/", dashboard.confirm_check, name="confirm_check"),
    path("calendar/", calendar.tape_chart, name="calendar"),
    path("calendar/room/<int:pk>/", calendar.room_month, name="room_month"),
    path("stays/", stays.stay_list, name="stay_list"),
    path("stays/new/", stays.stay_form, name="stay_new"),
    path("upcoming/", stays.upcoming, name="upcoming"),
    path("stays/<int:pk>/", stays.stay_detail, name="stay_detail"),
    path("stays/<int:pk>/edit/", stays.stay_form, name="stay_edit"),
    path("stays/<int:pk>/extend/", stays.extend, name="stay_extend"),
    path("stays/<int:pk>/reveal-id/", stays.reveal_id, name="stay_reveal_id"),
    path("stays/<int:pk>/whatsapp/", whatsapp.send, name="stay_whatsapp"),
    path("stays/<int:pk>/payments/<int:payment_pk>/delete/", stays.payment_delete, name="payment_delete"),
    path("stays/<int:pk>/<slug:action>/", stays.stay_action, name="stay_action"),
    path("guests/", guests.guest_list, name="guest_list"),
    path("guests/<int:pk>/", guests.guest_detail, name="guest_detail"),
    path("guests/lookup/", stays.guest_lookup, name="guest_lookup"),
    path("rooms/", rooms.rooms_home, name="rooms"),
    path("rooms/type/new/", rooms.edit, {"kind": "type"}, name="roomtype_new"),
    path("rooms/type/<int:pk>/", rooms.edit, {"kind": "type"}, name="roomtype_edit"),
    path("rooms/room/new/", rooms.edit, {"kind": "room"}, name="room_new"),
    path("rooms/room/<int:pk>/", rooms.edit, {"kind": "room"}, name="room_edit"),
    path("rooms/room/<int:pk>/log/", rooms.room_log, name="room_log"),
    path("rooms/log/<int:pk>/done/", rooms.log_done, name="maintenance_done"),
    path("rooms/log/<int:pk>/delete/", rooms.log_delete, name="maintenance_delete"),
    path("rooms/<str:kind>/<int:pk>/delete/", rooms.delete, name="room_delete"),
    path("revenue/", revenue.revenue_page, name="revenue"),
    path("revenue/<int:year>-<mm:month_no>/", revenue.revenue_page, name="revenue_month"),
    path("revenue/overall/", revenue.revenue_overall, name="revenue_overall"),
    path("revenue/<int:year>-<mm:month_no>/share/", revenue.month_share, name="revenue_month_share"),
    path("revenue/<int:year>-<mm:month_no>/report/", revenue.month_report, name="revenue_month_report"),
    path("revenue/lock/", revenue.revenue_lock, name="revenue_lock"),
    path("revenue/settle/", revenue.settle, name="settle"),
    path("revenue/settle/<int:pk>/delete/", revenue.settlement_delete, name="settlement_delete"),
    path("revenue/password/", revenue.revenue_change_password, name="revenue_password"),
    path("expenses/", expenses.expense_list, name="expense_list"),
    path("expenses/new/", expenses.expense_form, name="expense_new"),
    path("expenses/<int:pk>/", expenses.expense_form, name="expense_edit"),
    path("expenses/<int:pk>/receipt/", photos.receipt, name="expense_receipt"),
    path("expenses/<int:pk>/receipt/delete/", photos.receipt_delete, name="expense_receipt_delete"),
    path("guests/<int:pk>/id-photo/", photos.id_photo, name="guest_id_photo"),
    path("guests/<int:pk>/id-photo/upload/", photos.id_photo_upload, name="guest_id_photo_upload"),
    path("guests/<int:pk>/id-photo/delete/", photos.id_photo_delete, name="guest_id_photo_delete"),
    path("expenses/monthly/new/", expenses.recurring_form, name="recurring_new"),
    path("expenses/monthly/<int:pk>/", expenses.recurring_form, name="recurring_edit"),
    path("expenses/monthly/<int:pk>/delete/", expenses.recurring_delete, name="recurring_delete"),
    path("expenses/<int:pk>/delete/", expenses.expense_delete, name="expense_delete"),
    path("whatsapp/", whatsapp.whatsapp_page, name="whatsapp"),
    path("whatsapp/in-house/", whatsapp.in_house, name="wa_in_house"),
    path("whatsapp/messages/new/", whatsapp.template_edit, name="wa_template_new"),
    path("whatsapp/messages/<int:pk>/", whatsapp.template_edit, name="wa_template_edit"),
    path("whatsapp/messages/<int:pk>/delete/", whatsapp.template_delete, name="wa_template_delete"),
    path("export/", data.export, name="export"),
    path("backups/", data.backups, name="backups"),
    path("backup/run/", data.backup_hook, name="backup_hook"),
    path("settings/", data.settings_page, name="settings"),
    path("settings/users/", users.users, name="users"),
    path("settings/password/", users.my_password, name="my_password"),
]
