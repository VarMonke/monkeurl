from django.conf import settings
from django.core.validators import URLValidator
from django.db import models
from django.db.models import F
from django.utils import timezone


class GugolUser(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )


class LiveManager(models.Manager):
    """Just returns a queryset for non soft deleted links"""

    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class URLR(models.Model):
    """The alias object itself"""

    id = models.CharField(
        primary_key=True,
        max_length=64
    )
    origin = models.TextField(
        validators=[URLValidator()]
    )
    created_by = models.ForeignKey(
        GugolUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="links",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted = models.BooleanField(default=False)
    clicks = models.PositiveBigIntegerField(default=0)

    objects = LiveManager()
    all_objects = models.Manager()

    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    def soft_delete(self) -> None:
        self.deleted = True
        self.deleted_at = timezone.now()
        self.save(
            update_fields=["deleted", "deleted_at"]
        )

    def add_clicks(self, n: int) -> None:
        """Atowomic bump used by redis to push click count"""
        URLR.all_objects.filter(pk=self.pk).update(clicks=F("clicks") + n)

    def inc(self) -> None:
        self.add_clicks(1)


class DailyClick(models.Model):
    link = models.ForeignKey(
        URLR,
        on_delete=models.CASCADE,
        related_name="daily_clicks"
    )
    date = models.DateField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["link", "date"], name="uniq_daily_click",
            )
        ]
        indexes = [models.Index(fields=["link", "date"])]

    def add(self, n: int) -> None:
        DailyClick.objects.filter(pk=self.pk).update(count=F("count") + n)


class Referrer(models.Model):
    link = models.ForeignKey(
        URLR,
        on_delete=models.CASCADE,
        related_name="referrers"
    )
    name = models.CharField(max_length=256)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["link", "name"], name="uniq_link_referrer")
        ]

    def add(self, n: int) -> None:
        Referrer.objects.filter(pk=self.pk).update(count=F("count") + n)
