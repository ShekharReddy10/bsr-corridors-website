from django.urls import path

from core.views import users

from . import views

urlpatterns = [
    path("cluster/", views.home, name="cluster_home"),
    path("cluster/users/", users.users, name="users"),
    path("cluster/items/<slug:code>/theme/", views.theme, name="item_theme"),
]
