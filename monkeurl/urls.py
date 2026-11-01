from django.contrib import admin
from django.urls import include, path

from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/links/", views.LinkCreateView.as_view(), name="api-create"),
    path("api/links/<str:code>/", views.LinkDetailView.as_view(), name="api-detail"),
    path("healthz", views.healthz, name="healthz"),
    path("", views.HomeView.as_view(), name="home"),
    path("profile/", views.profile_page, name="profile"),
    path("stats/<str:code>/", views.stats_page, name="stats"),
    path("<str:code>/", views.redirect_view, name="redirect"),
]
