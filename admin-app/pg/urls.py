from django.urls import path

from . import views

urlpatterns = [
    path("pg/", views.home, name="pg_home"),
    path("pg/floor/<int:pk>/", views.floor, name="pg_floor"),
    path("pg/room/<int:pk>/", views.room, name="pg_room"),
    path("pg/bed/<int:pk>/", views.bed, name="pg_bed"),
    path("pg/bed/<int:pk>/edit/", views.bed_edit, name="pg_bed_edit"),
    path("pg/bed/<int:pk>/move-in/", views.move_in, name="pg_move_in"),
    path("pg/bed/<int:pk>/<slug:action>/", views.bed_action, name="pg_bed_action"),
    path("pg/tenancy/<int:pk>/", views.tenancy, name="pg_tenancy"),
    path("pg/tenancy/<int:pk>/move-out/", views.move_out, name="pg_move_out"),
    path("pg/tenancy/<int:pk>/rent/<slug:action>/", views.rent_action, name="pg_rent_action"),
    path("pg/tenancy/<int:pk>/<slug:action>/", views.tenancy_action, name="pg_tenancy_action"),
    path("pg/payment/<int:pk>/receipt/", views.receipt, name="pg_receipt"),
    path("pg/messages/", views.message_list, name="pg_messages"),
    path("pg/messages/new/", views.message_edit, name="pg_message_new"),
    path("pg/messages/<int:pk>/", views.message_edit, name="pg_message_edit"),
    path("pg/revenue/", views.revenue, name="pg_revenue"),
    path("pay/rent/<str:token>/", views.pay_page, name="pg_pay_page"),
    path("pay/receipt/<str:token>/", views.public_receipt, name="pg_public_receipt"),
    path("pg/residents/", views.residents, name="pg_residents"),
    path("pg/residents/lookup/", views.resident_lookup, name="pg_resident_lookup"),
    path("pg/residents/<int:pk>/", views.resident, name="pg_resident"),
    path("pg/residents/<int:pk>/reveal-id/", views.reveal_id, name="pg_reveal_id"),
    path("pg/file/<slug:what>/<int:pk>/", views.file, name="pg_file"),
    path("pg/setup/", views.setup, name="pg_setup"),
    path("pg/setup/<slug:kind>/new/", views.setup_edit, name="pg_setup_new"),
    path("pg/setup/<slug:kind>/<int:pk>/", views.setup_edit, name="pg_setup_edit"),
]
