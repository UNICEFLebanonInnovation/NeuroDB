from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views import defaults as default_views
from django.views.generic import TemplateView

from pivoting.views import HomeView
from django.views.static import serve 
from .sso_views import sso_callback, sso_login, sso_logout

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path('', view=HomeView.as_view(), name='home'),
    path('etools/', include(('etools.urls', 'etools'), namespace='etools')),
    path('v2/', include(('pivoting.urls', 'pivoting'), namespace='pivoting')),
    path('locations/', include(('locations.urls', 'locations'), namespace='locations')),
    path('about/', TemplateView.as_view(template_name='pages/about.html'), name='about'),
    path('users/', include(('users.urls', 'users'), namespace='users')),
    path('accounts/', include('allauth.urls')),
    # path('sso/callback/', include('allauth.socialaccount.urls')),  # your callback view is handled here
    path('sso/login/', sso_login, name='sso_login'),
    path('sso/callback/', sso_callback, name='sso_callback'),
    path('sso/logout/', sso_logout, name='sso_logout'),
    path('media/<str:path>', serve, {'document_root': settings.MEDIA_ROOT}),
    path('static/<str:path>', serve, {'document_root': settings.STATIC_ROOT}),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT) + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)


if settings.DEBUG:
    # This allows the error pages to be debugged during development, just visit
    # these url in browser to see how these error pages look like.
    urlpatterns += [
        path(
            "400/",
            default_views.bad_request,
            kwargs={"exception": Exception("Bad Request!")},
        ),
        path(
            "403/",
            default_views.permission_denied,
            kwargs={"exception": Exception("Permission Denied")},
        ),
        path(
            "404/",
            default_views.page_not_found,
            kwargs={"exception": Exception("Page not Found")},
        ),
        path("500/", default_views.server_error),
    ]
