from django.apps import AppConfig


class MonkeurlConfig(AppConfig):
    name = "monkeurl"

    def ready(self):
        from . import signals  # noqa: F401
