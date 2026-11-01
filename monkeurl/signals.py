from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import GugolUser


@receiver(post_save, sender=get_user_model())
def ensure_profile(sender, instance, created, **kwargs):
    """Every User gets a GugolUser; without it nothing is ever owned."""
    if created:
        GugolUser.objects.get_or_create(user=instance)
