from django.urls import path
from django.contrib.auth import views as auth_views
from django.views.generic import TemplateView
from . import views

urlpatterns = [
    # ── Auth (custom views) ───────────────────────────────────────────────
    path("login/",   views.login_view,  name="login"),
    path("logout/",  views.logout_view, name="logout"),

    # Password change still uses Django's built-in views (no custom logic needed)
    path(
        "password-change/",
        auth_views.PasswordChangeView.as_view(
            template_name="base/password_change.html",
            success_url="/password-change/done/",
        ),
        name="password_change",
    ),
    path(
        "password-change/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="base/password_change_done.html",
        ),
        name="password_change_done",
    ),

    # ── Core application (all require login) ─────────────────────────────
    path("upload_jd/",                    views.upload_jd,              name="upload_jd"),
    path("results/<int:pk>/",             views.results,                name="results"),
    path("match-candidates/<int:jd_pk>/", views.match_candidates,       name="match_candidates"),
    path("show-matches/<int:jd_pk>/",     views.show_matches,           name="show_matches"),
    path("download-matches/<int:jd_pk>/", views.download_matched_file,  name="download_matched_file"),

    # ── Utilities ─────────────────────────────────────────────────────────
    path("test-api/",      views.test_api_connection,      name="test_api_connection"),
    path("test-api-init/", views.test_api_connection_init, name="test_api_connection_init"),

    # ── Token usage ───────────────────────────────────────────────────────
    path("token-usage/",         views.token_usage_dashboard, name="token_usage_dashboard"),
    path("token-usage/refresh/", views.clear_token_cache,     name="clear_token_cache"),

    # ── Google site verification ──────────────────────────────────────────
    path("google63d0dde2db21043b.html", views.google_verify, name="google_verify"),
    path('about/',TemplateView.as_view(template_name='about.html'), name='about'),
    path('privacy/',TemplateView.as_view(template_name='privacy.html'),name='privacy'),
    path('terms/',TemplateView.as_view(template_name='terms.html'),name='terms'),
    ]