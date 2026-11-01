from rest_framework import serializers

from . import services
from .models import URLR


class URLRCreateSerializer(serializers.ModelSerializer):
    alias = serializers.CharField(
        required=False, allow_blank=True, max_length=64, write_only=True
    )
    ttl_seconds = serializers.IntegerField(
        required=False, min_value=0, write_only=True
    )

    class Meta:
        model = URLR
        fields = ["origin", "alias", "ttl_seconds"]

    def validate_origin(self, value):
        return value.strip()


class URLRDetailSerializer(serializers.ModelSerializer):
    short_url = serializers.SerializerMethodField()
    owner = serializers.SerializerMethodField()
    clicks = serializers.SerializerMethodField()
    clicks_pending = serializers.SerializerMethodField()
    daily = serializers.SerializerMethodField()
    top_referrers = serializers.SerializerMethodField()
    expired = serializers.SerializerMethodField()

    class Meta:
        model = URLR
        fields = [
            "id", "origin", "short_url", "owner", "clicks", "clicks_pending",
            "created_at", "expires_at", "expired", "deleted", "deleted_at",
            "daily", "top_referrers",
        ]
        read_only_fields = fields

    def get_short_url(self, obj):
        request = self.context.get("request")
        path = f"/{obj.id}/"
        return request.build_absolute_uri(path) if request else path

    def get_clicks_pending(self, obj):
        return services.pending_clicks(obj.id)

    def get_clicks(self, obj):
        return obj.clicks + services.pending_clicks(obj.id)

    def get_owner(self, obj):
        return obj.created_by.user.username if obj.created_by else None

    def get_expired(self, obj):
        return obj.is_expired()

    def get_daily(self, obj):
        return {
            str(row["date"]): row["count"]
            for row in obj.daily_clicks.order_by("-date").values("date", "count")[:30]
        }

    def get_top_referrers(self, obj):
        return {
            row["name"]: row["count"]
            for row in obj.referrers.order_by("-count").values("name", "count")[:10]
        }
