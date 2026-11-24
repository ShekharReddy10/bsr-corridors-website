from django.urls import path

from core.views import users

from . import views

# Shown to people as "Properties" (each hotel, PG or co-living is a property).
urlpatterns = [
    path("properties/", views.home, name="cluster_home"),
    path("properties/users/", users.users, name="users"),
    path("properties/new/", views.item_new, name="item_new"),
    path("properties/drive/callback/", views.drive_callback, name="drive_callback"),
    path("properties/<slug:code>/", views.item_edit, name="item_edit"),
    path("properties/<slug:code>/theme/", views.theme, name="item_theme"),
    path("properties/<slug:code>/photo/<slug:which>/", views.item_photo, name="item_photo"),
    path("properties/<slug:code>/drive/", views.drive_connect, name="drive_connect"),
]
