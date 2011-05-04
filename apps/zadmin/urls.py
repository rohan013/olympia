from django.conf.urls.defaults import patterns, url, include
from django.contrib import admin
from django.shortcuts import redirect

from amo.urlresolvers import reverse
from . import views


urlpatterns = patterns('',
    # AMO stuff.
    url('^$', lambda r: redirect('admin:index'), name='zadmin.home'),
    url('^env$', views.env, name='amo.env'),
    url('^flagged', views.flagged, name='zadmin.flagged'),
    url('^hera', views.hera, name='zadmin.hera'),
    url('^settings', views.settings, name='zadmin.settings'),
    url('^fix-disabled', views.fix_disabled_file, name='zadmin.fix-disabled'),
    url(r'^validation/application_versions\.json$',
        views.application_versions_json,
        name='zadmin.application_versions_json'),
    url(r'^validation/start$', views.start_validation,
        name='zadmin.start_validation'),
    url(r'^validation/set/(?P<job>\d+)$', views.set_max_version,
        name='zadmin.set-max-version'),
    url(r'^validation/notify/(?P<job>\d+)$', views.notify,
        name='zadmin.notify'),
    url(r'^validation/notify/syntax.json$', views.notify_syntax,
        name='zadmin.notify.syntax'),
    url(r'^validation$', views.validation, name='zadmin.validation'),

    # The Django admin.
    url('^models/', include(admin.site.urls)),
)


# Hijack the admin's login to use our pages.
def login(request):
    url = '%s?to=%s' % (reverse('users.login'), request.path)
    return redirect(url)


admin.site.login = login
