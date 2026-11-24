from django.urls import path

from core.views import users

from . import views

urlpatterns = [
    path("cluster/", views.home, name="cluster_home"),
    path("cluster/users/", users.users, name="users"),
    path("cluster/items/<slug:code>/theme/", views.theme, name="item_theme"),
    path("cluster/items/new/", views.item_new, name="item_new"),
    path("cluster/items/<slug:code>/", views.item_edit, name="item_edit"),
    path("cluster/items/<slug:code>/photo/<slug:which>/", views.item_photo, name="item_photo"),
    path("cluster/items/<slug:code>/drive/", views.drive_connect, name="drive_connect"),
    path("cluster/drive/callback/", views.drive_callback, name="drive_callback"),
]
