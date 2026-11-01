import base64
import io
import logging

from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.views import View
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import URLR
from .serializers import URLRCreateSerializer, URLRDetailSerializer

log = logging.getLogger(__name__)


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


def _own_hosts(request) -> set[str]:
    host = request.get_host().split(":")[0].lower()
    return {host, *getattr(settings, "SHORTENER_OWN_HOSTS", [])}


def _owner_for(request):
    if not request.user.is_authenticated:
        return None
    return getattr(request.user, "profile", None)


class LinkCreateView(APIView):
    """POST /api/links/ to create a short link. Ratelimited per IP."""

    def post(self, request):
        ip = _client_ip(request)
        if not services.check_rate_limit(ip):
            return Response(
                {
                    "error": "easy there, turbo",
                    "detail": f"limit: {settings.SHORTENER_RATE_LIMIT} links per "
                              f"{settings.SHORTENER_RATE_WINDOW}s",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        ser = URLRCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        try:
            services.validate_destination(data["origin"], _own_hosts(request))
            link = services.create_link(
                origin=data["origin"],
                alias=data.get("alias") or None,
                ttl_seconds=data.get("ttl_seconds"),
                owner=_owner_for(request),
            )
        except services.ReservedAlias as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except services.AliasConflict as e:
            return Response({"error": str(e)}, status=status.HTTP_409_CONFLICT)
        except services.UnsafeDestination as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        out = URLRDetailSerializer(link, context={"request": request})
        return Response(out.data, status=status.HTTP_201_CREATED)


class LinkDetailView(APIView):
    """GET /api/links/{code}/ stats. DELETE owner-only soft delete."""

    def get(self, request, code):
        link = URLR.all_objects.filter(pk=code).first()
        if link is None:
            return Response({"error": "dead link. it had a good run."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(URLRDetailSerializer(
            link, context={"request": request}).data)

    def delete(self, request, code):
        link = URLR.all_objects.filter(pk=code).first()
        if link is None:
            return Response({"error": "dead link. it had a good run."},
                            status=status.HTTP_404_NOT_FOUND)
        owner = _owner_for(request)
        if link.created_by is None:
            return Response(
                {"error": "this link was made anonymously. it belongs to no "
                          "one now. like a ghost."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if owner is None or (owner != link.created_by
                             and not request.user.is_staff):
            return Response({"error": "not your link. hands off."},
                            status=status.HTTP_403_FORBIDDEN)
        link.soft_delete()
        services.evict_code(link.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


def redirect_view(request: HttpRequest, code: str) -> HttpResponse:
    """The hot path. Designed to be read-mostly and cacheable."""
    res = services.resolve(code)
    if res.status == "ok":
        services.record_click(code, request.META.get("HTTP_REFERER"))
        if getattr(settings, "SHORTENER_REDIRECT_STATUS", 301) == 301:
            return HttpResponsePermanentRedirect(res.origin)
        return HttpResponseRedirect(res.origin)
    if res.status == "expired":
        return render(request, "gone.html",
                      {"code": code, "reason": "This link has crossed the "
                                               "rainbow bridge."}, status=410)
    if res.status == "deleted":
        return render(request, "gone.html",
                      {"code": code, "reason": "This link was put down by "
                                               "its owner."}, status=410)
    return render(request, "gone.html",
                  {"code": code, "reason": "No such link. It never existed, "
                                           "or it is very good at hiding."},
                  status=404)


class HomeView(View):
    """GET / the form. POST / create a link the HTML way."""

    def get(self, request):
        return render(request, "index.html", _ctx(request))

    def post(self, request):
        ctx = _ctx(request)
        ip = _client_ip(request)
        if not services.check_rate_limit(ip):
            ctx["error"] = (
                f"Easy there, turbo. Limit: {settings.SHORTENER_RATE_LIMIT} "
                f"links per {settings.SHORTENER_RATE_WINDOW}s."
            )
            return render(request, "index.html", ctx, status=429)

        origin = request.POST.get("origin", "").strip()
        alias = request.POST.get("alias", "").strip() or None
        ttl_raw = request.POST.get("ttl_seconds", "")
        try:
            ttl_seconds = int(ttl_raw) if ttl_raw else None
        except ValueError:
            ttl_seconds = None

        try:
            services.validate_destination(origin, _own_hosts(request))
            link = services.create_link(
                origin=origin, alias=alias, ttl_seconds=ttl_seconds,
                owner=_owner_for(request),
            )
        except services.UnsafeDestination as e:
            ctx["error"] = str(e)
            return render(request, "index.html", ctx, status=400)
        except (services.ReservedAlias, services.AliasConflict) as e:
            ctx["error"] = str(e)
            return render(request, "index.html", ctx,
                          status=409 if isinstance(e, services.AliasConflict)
                          else 400)

        ctx["link"] = link
        ctx["short_url"] = request.build_absolute_uri(f"/{link.id}/")
        return render(request, "index.html", ctx, status=201)


def _ctx(request) -> dict:
    return {
        "max_ttl_hours": getattr(settings, "SHORTENER_MAX_TTL_SECONDS",
                                 10 * 365 * 86400) // 3600,
        "default_ttl": getattr(settings, "SHORTENER_DEFAULT_TTL_SECONDS", 0),
    }


def stats_page(request: HttpRequest, code: str) -> HttpResponse:
    link = URLR.all_objects.filter(pk=code).first()
    if link is None:
        return render(request, "gone.html",
                      {"code": code, "reason": "No such link. It never "
                                               "existed, or it is very good "
                                               "at hiding."}, status=404)

    import qrcode

    short_url = request.build_absolute_uri(f"/{link.id}/")
    img = qrcode.make(short_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    can_delete = (
        link.created_by is not None
        and request.user.is_authenticated
        and (getattr(request.user, "profile", None) == link.created_by
             or request.user.is_staff)
    )
    return render(request, "stats.html", {
        "link": link,
        "short_url": short_url,
        "qr_b64": qr_b64,
        "daily": link.daily_clicks.order_by("-date")[:30],
        "referrers": link.referrers.order_by("-count")[:10],
        "pending": services.pending_clicks(code),
        "can_delete": can_delete,
    })


def profile_page(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return HttpResponseRedirect("/accounts/login/")
    profile = getattr(request.user, "profile", None)
    links = (profile.links.order_by("-created_at")
             if profile else URLR.objects.none())
    return render(request, "profile.html", {
        "profile": profile,
        "links": links,
    })


def healthz(request: HttpRequest) -> JsonResponse:
    db_ok = True
    try:
        URLR.all_objects.exists()
    except Exception:
        db_ok = False
    redis_ok = True
    try:
        services.get_redis().ping()
    except Exception:
        redis_ok = False
    return JsonResponse({
        "status": "alive" if db_ok else "dying",
        "mood": "barely",
        "db": db_ok,
        "redis": redis_ok,
    }, status=200 if db_ok else 503)
