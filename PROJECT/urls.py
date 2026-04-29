from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from base.views import google_verify


urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('base.urls')),
    path('google.html', google_verify),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)