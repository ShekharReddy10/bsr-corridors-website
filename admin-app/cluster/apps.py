from django.apps import AppConfig


class ClusterConfig(AppConfig):
    name = "cluster"
    verbose_name = "Cluster"

    def ready(self):
        from . import current  # noqa: F401 — sets the schema on new item connections
