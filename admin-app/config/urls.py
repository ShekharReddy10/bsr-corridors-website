from django.urls import include, path

urlpatterns = [
    path("", include("cluster.urls")),
    path("", include("core.urls")),
    path("", include("pg.urls")),
]
