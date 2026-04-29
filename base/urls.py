from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # Authentication URLs
    path('', auth_views.LoginView.as_view(template_name='base/login.html'), name='home'),
    path('login/', auth_views.LoginView.as_view(template_name='base/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
    path('password-change/', auth_views.PasswordChangeView.as_view(
        template_name='base/password_change.html',
        success_url='/password-change/done/'
    ), name='password_change'),
    path('password-change/done/', auth_views.PasswordChangeDoneView.as_view(
        template_name='base/password_change_done.html'
    ), name='password_change_done'),
    
    # Main application URLs (all require authentication)
    path('upload_jd/', views.upload_jd, name='upload_jd'),
    path('results/<int:pk>/', views.results, name='results'),
    path('match-candidates/<int:jd_pk>/', views.match_candidates, name='match_candidates'),
    path('show-matches/<int:jd_pk>/', views.show_matches, name='show_matches'),
    path('test-api/', views.test_api_connection, name='test_api_connection'),
    path('download-matches/<int:jd_pk>/', views.download_matched_file, name='download_matched_file'),
    path('token-usage/', views.token_usage_dashboard, name='token_usage_dashboard'),
]