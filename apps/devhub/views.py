import codecs
import collections
import functools
import json
import os
import uuid

from django import http
from django.conf import settings
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import urlquote

import commonware.log
import jingo
import path
from tower import ugettext_lazy as _lazy, ugettext as _

import amo
import amo.utils
from amo import messages
from amo.helpers import urlparams
from amo.utils import MenuItem
from amo.urlresolvers import reverse
from amo.decorators import json_view, login_required, post_required
from access import acl
from addons import forms as addon_forms
from addons.models import Addon, AddonUser
from addons.views import BaseFilter
from cake.urlresolvers import remora_url
from devhub.models import ActivityLog, RssKey, SubmitStep
from files.models import File, FileUpload
from translations.models import delete_translation
from versions.models import License, Version

from . import forms, tasks, feeds

log = commonware.log.getLogger('z.devhub')

# Acceptable extensions.
EXTENSIONS = ('.xpi', '.jar', '.xml')

# We use a session cookie to make sure people see the dev agreement.
DEV_AGREEMENT_COOKIE = 'yes-I-read-the-dev-agreement'


def dev_required(f):
    """Requires user to be add-on owner or admin"""
    @login_required
    @functools.wraps(f)
    def wrapper(request, addon_id, *args, **kw):
        addon = get_object_or_404(Addon, id=addon_id)
        fun = lambda: f(request, addon_id=addon_id, addon=addon, *args, **kw)
        # Require an owner for POST requests.
        if request.method == 'POST':
            if acl.check_ownership(request, addon, require_owner=True):
                return fun()
        elif acl.check_ownership(request, addon, require_owner=False):
            return fun()
        return http.HttpResponseForbidden()
    return wrapper


def owner_for_post_required(f):
    @functools.wraps(f)
    def wrapper(request, addon_id, addon, *args, **kw):
        is_admin = acl.action_allowed(request, 'Admin', 'EditAnyAddon')
        qs = addon.authors.filter(addonuser__role=amo.AUTHOR_ROLE_OWNER,
                                  user=request.amo_user)
        if request.method == 'POST' and not (is_admin or qs):
            return http.HttpResponseForbidden()
        else:
            return f(request, addon_id, addon, *args, **kw)
    return wrapper


class AddonFilter(BaseFilter):
    opts = (('name', _lazy(u'Name')),
            ('updated', _lazy(u'Updated')),
            ('created', _lazy(u'Created')),
            ('popular', _lazy(u'Downloads')),
            ('rating', _lazy(u'Rating')))


def addon_listing(request, addon_type, default='name'):
    """Set up the queryset and filtering for addon listing for Dashboard."""
    qs = request.amo_user.addons.all()
    filter = AddonFilter(request, qs, 'sort', default)
    return filter.qs, filter


def index(request):
    return jingo.render(request, 'devhub/index.html')


@login_required
def dashboard(request):
    TYPE = amo.ADDON_ANY
    addons, filter = addon_listing(request, TYPE)
    addons = amo.utils.paginate(request, addons, per_page=10)
    data = dict(addons=addons, sorting=filter.field,
                items=_get_items(None, request.amo_user.addons.all())[:4],
                sort_opts=filter.opts, rss=_get_rss_feed(request))
    return jingo.render(request, 'devhub/addons/dashboard.html', data)


@dev_required
def ajax_compat_status(request, addon_id, addon):
    if not (addon.accepts_compatible_apps() and addon.current_version):
        raise http.Http404()
    return jingo.render(request, 'devhub/addons/ajax_compat_status.html',
                        dict(addon=addon))


@dev_required
def ajax_compat_update(request, addon_id, addon, version_id):
    if not addon.accepts_compatible_apps():
        raise http.Http404()
    version = get_object_or_404(Version, pk=version_id, addon=addon)
    compat_form = forms.CompatFormSet(request.POST or None,
                                      queryset=version.apps.all())
    if request.method == 'POST' and compat_form.is_valid():
        for compat in compat_form.save(commit=False):
            compat.version = version
            compat.save()
    return jingo.render(request, 'devhub/addons/ajax_compat_update.html',
                        dict(addon=addon, version=version,
                             compat_form=compat_form))


def _get_addons(request, addons, addon_id):
    """Create a list of ``MenuItem``s for the activity feed."""
    items = []

    a = MenuItem()
    a.selected = (not addon_id)
    (a.text, a.url) = (_('All My Add-ons'), reverse('devhub.feed_all'))
    items.append(a)

    for addon in addons:
        item = MenuItem()
        try:
            item.selected = (addon_id and addon.id == int(addon_id))
        except ValueError:
            pass  # We won't get here... EVER
        url = reverse('devhub.feed', args=(addon.id,))
        (item.text, item.url) = (addon.name, url)
        items.append(item)

    return items


def _get_activities(request, action):
    url = request.get_full_path()
    choices = (None, 'updates', 'status', 'collections', 'reviews')
    text = {None: _('All Activity'),
            'updates': _('Add-on Updates'),
            'status': _('Add-on Status'),
            'collections': _('User Collections'),
            'reviews': _('User Reviews'),
            }

    items = []
    for c in choices:
        i = MenuItem()
        i.text = text[c]
        i.url, i.selected = urlparams(url, page=None, action=c), (action == c)
        items.append(i)

    return items


def _get_items(action, addons):
    filters = dict(updates=(amo.LOG.ADD_VERSION, amo.LOG.ADD_FILE_TO_VERSION),
                   status=(amo.LOG.SET_INACTIVE, amo.LOG.UNSET_INACTIVE,
                           amo.LOG.CHANGE_STATUS, amo.LOG.APPROVE_VERSION,),
                   collections=(amo.LOG.ADD_TO_COLLECTION,
                            amo.LOG.REMOVE_FROM_COLLECTION,),
                   reviews=(amo.LOG.ADD_REVIEW,))

    filter = filters.get(action)
    items = ActivityLog.objects.for_addons(addons)
    if filter:
        items = items.filter(action__in=[i.id for i in filter])

    return items


def _get_rss_feed(request):
    key, __ = RssKey.objects.get_or_create(user=request.amo_user)
    return urlparams(reverse('devhub.feed_all'), privaterss=key.key)


def feed(request, addon_id=None):
    if request.GET.get('privaterss'):
        return feeds.ActivityFeedRSS()(request)

    if not request.user.is_authenticated():
        url = reverse('users.login')
        p = urlquote(request.get_full_path())
        return http.HttpResponseRedirect('%s?to=%s' % (url, p))
    else:
        addons_all = request.amo_user.addons.all()

        if addon_id:
            try:
                key = RssKey.objects.get(addon=addon_id)
            except RssKey.DoesNotExist:
                key = RssKey.objects.create(addon_id=addon_id)

            rssurl = urlparams(reverse('devhub.feed', args=(addon_id,)),
                               privaterss=key.key)
            addons = get_object_or_404(Addon, pk=addon_id)

            if not acl.check_ownership(request, addons):
                return http.HttpResponseForbidden()
        else:
            rssurl = _get_rss_feed(request)
            addons = addons_all

    action = request.GET.get('action')

    items = _get_items(action, addons)

    activities = _get_activities(request, action)
    addon_items = _get_addons(request, addons_all, addon_id)

    pager = amo.utils.paginate(request, items, 20)
    data = dict(addons=addon_items, pager=pager, activities=activities,
                rss=rssurl)
    return jingo.render(request, 'devhub/addons/activity.html', data)


@dev_required
def edit(request, addon_id, addon):

    data = {
       'page': 'edit',
       'addon': addon,
       'tags': addon.tags.not_blacklisted().values_list('tag_text', flat=True),
       'previews': addon.previews.all(),
       }

    return jingo.render(request, 'devhub/addons/edit.html', data)


@dev_required
@owner_for_post_required
def delete(request, addon_id, addon):
    form = forms.DeleteForm(request)
    if form.is_valid():
        addon.delete('Removed via devhub')
        messages.success(request, _('Add-on deleted.'))
        return redirect(reverse('devhub.addons'))
    else:
        messages.error(request,
                       _('Password was incorrect.  Add-on was not deleted.'))
        return redirect(reverse('devhub.versions', args=(addon_id,)))


def _license_form(request, addon, save=False):
    qs = addon.versions.order_by('-version')[:1]
    version = qs[0] if qs else None
    if version:
        instance, initial = version.license, None
        # Clear out initial data if it's a builtin license.
        if getattr(instance, 'builtin', None):
            instance, initial = None, {'builtin': instance.builtin}
        license_form = forms.LicenseForm(request.POST or None, initial=initial,
                                         instance=instance)

    if save and version and license_form.is_valid():
        license = license_form.save()
        version.update(license=license)
        amo.log(amo.LOG.CHANGE_LICENSE, license, addon)

    license_urls = dict(License.objects.builtins()
                        .values_list('builtin', 'url'))
    return dict(license_urls=license_urls, version=version,
                license_form=version and license_form,
                license_other_val=License.OTHER)


def _policy_form(request, addon, save=False):
    policy_form = forms.PolicyForm(
        request.POST or None, instance=addon,
        initial=dict(has_priv=bool(addon.privacy_policy),
                     has_eula=bool(addon.eula)))
    if save and policy_form.is_valid():
        policy_form.save(addon=addon)
        amo.log(amo.LOG.CHANGE_POLICY, addon, policy_form.instance)
    return policy_form


@dev_required
@owner_for_post_required
def ownership(request, addon_id, addon):
    fs, ctx = [], {}
    # Authors.
    qs = AddonUser.objects.filter(addon=addon).order_by('position')
    user_form = forms.AuthorFormSet(request.POST or None, queryset=qs)
    fs.append(user_form)
    # Versions.
    ctx.update(_license_form(request, addon))
    if ctx['license_form']:
        fs.append(ctx['license_form'])
    # Policy.
    policy_form = _policy_form(request, addon)
    fs.append(policy_form)

    if request.method == 'POST' and all([form.is_valid() for form in fs]):
        # Authors.
        authors = user_form.save(commit=False)
        for author in authors:
            action = (amo.LOG.CHANGE_USER_WITH_ROLE if author.id
                      else amo.LOG.REMOVE_USER_WITH_ROLE)
            author.addon = addon
            author.save()
            amo.log(action, author.user, author.get_role_display(), addon)
        _license_form(request, addon, save=True)
        _policy_form(request, addon, save=True)
        return redirect('devhub.addons.owner', addon_id)

    ctx.update(addon=addon, user_form=user_form, policy_form=policy_form)
    return jingo.render(request, 'devhub/addons/owner.html', ctx)


@dev_required
def payments(request, addon_id, addon):
    charity = None if addon.charity_id == amo.FOUNDATION_ORG else addon.charity
    charity_form = forms.CharityForm(request.POST or None, instance=charity,
                                     prefix='charity')
    contrib_form = forms.ContribForm(request.POST or None, instance=addon,
                                     initial=forms.ContribForm.initial(addon))
    profile_form = forms.ProfileForm(request.POST or None, instance=addon,
                                     required=True)
    if request.method == 'POST':
        if contrib_form.is_valid():
            addon = contrib_form.save(commit=False)
            addon.wants_contributions = True
            valid = _save_charity(addon, contrib_form, charity_form)
            if not addon.has_full_profile():
                valid &= profile_form.is_valid()
                if valid:
                    profile_form.save()
            if valid:
                addon.save()
                messages.success(request, _('Changes successfully saved.'))
                amo.log(amo.LOG.EDIT_CONTRIBUTIONS, addon)

                return redirect('devhub.addons.payments', addon_id)
    errors = charity_form.errors or contrib_form.errors or profile_form.errors
    if errors:
        messages.error(request, _('There were errors in your submission.'))
    return jingo.render(request, 'devhub/addons/payments.html',
        dict(addon=addon, charity_form=charity_form, errors=errors,
             contrib_form=contrib_form, profile_form=profile_form))


def _save_charity(addon, contrib_form, charity_form):
    recipient = contrib_form.cleaned_data['recipient']
    if recipient == 'dev':
        addon.charity = None
    elif recipient == 'moz':
        addon.charity_id = amo.FOUNDATION_ORG
    elif recipient == 'org':
        if charity_form.is_valid():
            addon.charity = charity_form.save()
        else:
            return False
    return True


@dev_required
@post_required
def disable_payments(request, addon_id, addon):
    addon.update(wants_contributions=False)
    return redirect('devhub.addons.payments', addon_id)


@dev_required
@post_required
def remove_profile(request, addon_id, addon):
    delete_translation(addon, 'the_reason')
    delete_translation(addon, 'the_future')
    if addon.wants_contributions:
        addon.update(wants_contributions=False)
    return redirect('devhub.addons.profile', addon_id)


@dev_required
def profile(request, addon_id, addon):
    profile_form = forms.ProfileForm(request.POST or None, instance=addon)

    if request.method == 'POST' and profile_form.is_valid():
        profile_form.save()
        amo.log(amo.LOG.EDIT_PROPERTIES, addon)

        return redirect('devhub.addons.profile', addon_id)

    return jingo.render(request, 'devhub/addons/profile.html',
                        dict(addon=addon, profile_form=profile_form))


def upload(request):
    if request.method == 'POST':
        #TODO(gkoberger): Bug 610800 - Don't load uploads into memory.
        upload = request.raw_post_data
        upload_name = request.META['HTTP_X_FILE_NAME']
        upload_size = request.META['HTTP_X_FILE_SIZE']
        loc = path.path(settings.ADDONS_PATH) / 'temp' / uuid.uuid4().hex
        if not loc.dirname().exists():
            loc.dirname().makedirs()
        ext = path.path(upload_name).ext
        if ext in EXTENSIONS:
            loc += ext
        log.info('UPLOAD: %r (%s bytes) to %r' %
                 (upload_name, upload_size, loc))
        with open(loc, 'wb') as fd:
            for chunk in upload:
                fd.write(chunk)
        user = getattr(request, 'amo_user', None)
        fu = FileUpload.objects.create(path=loc, name=upload_name, user=user)
        tasks.validator.delay(fu.pk)
        return redirect('devhub.upload_detail', fu.pk, 'json')

    return jingo.render(request, 'devhub/upload.html')


@json_view
def json_upload_detail(upload):
    validation = json.loads(upload.validation) if upload.validation else ""
    url = reverse('devhub.upload_detail', args=[upload.uuid, 'json'])
    r = dict(upload=upload.uuid, validation=validation,
             error=upload.task_error, url=url)
    return r


def upload_detail(request, uuid, format='html'):
    upload = get_object_or_404(FileUpload.uncached, uuid=uuid)

    if format == 'json' or request.is_ajax():
        return json_upload_detail(upload)

    return jingo.render(request, 'devhub/validation.html',
                        dict(upload=upload))


@dev_required
def addons_section(request, addon_id, addon, section, editable=False):
    models = {'basic': addon_forms.AddonFormBasic,
              'details': addon_forms.AddonFormDetails,
              'support': addon_forms.AddonFormSupport,
              'technical': addon_forms.AddonFormTechnical}

    if section not in models:
        return http.HttpResponseNotFound()

    if editable:
        if request.method == 'POST':
            form = models[section](request.POST, request.FILES,
                                  instance=addon, request=request)
            if form.is_valid():
                addon = form.save(addon)
                editable = False
                amo.log(amo.LOG.EDIT_PROPERTIES, addon)
        else:
            form = models[section](instance=addon, request=request)
    else:
        form = False

    tags = addon.tags.not_blacklisted().values_list('tag_text', flat=True)

    data = {'addon': addon,
            'form': form,
            'editable': editable,
            'tags': tags}

    return jingo.render(request,
                        'devhub/includes/addon_edit_%s.html' % section, data)


@dev_required
def version_edit(request, addon_id, addon, version_id):
    version = get_object_or_404(Version, pk=version_id, addon=addon)
    version_form = forms.VersionForm(request.POST or None, instance=version)

    new_file_form = forms.NewFileForm(request.POST or None)

    file_form = forms.FileFormSet(request.POST or None, prefix='files',
                                  queryset=version.files.all())
    data = {'version_form': version_form, 'file_form': file_form}

    # https://bugzilla.mozilla.org/show_bug.cgi?id=605941
    # remove compatability from the version edit page for search engines
    if addon.type != amo.ADDON_SEARCH:
        compat_form = forms.CompatFormSet(request.POST or None,
                                      queryset=version.apps.all())
        data['compat_form'] = compat_form

    if (request.method == 'POST' and
        all([form.is_valid() for form in data.values()])):
        data['version_form'].save()
        data['file_form'].save()

        for deleted in data['file_form'].deleted_forms:
            file = deleted.cleaned_data['id']
            amo.log(amo.LOG.DELETE_FILE_FROM_VERSION,
                    file.filename, file.version, addon)

        if 'compat_form' in data:
            for compat in data['compat_form'].save(commit=False):
                compat.version = version
                compat.save()
        return redirect('devhub.versions.edit', addon_id, version_id)

    data.update(addon=addon, version=version, new_file_form=new_file_form)
    return jingo.render(request, 'devhub/versions/edit.html', data)


@dev_required
@post_required
def version_delete(request, addon_id, addon):
    version_id = request.POST.get('version_id')
    version = get_object_or_404(Version, pk=version_id, addon=addon)
    version.delete()
    return redirect('devhub.versions', addon_id)


@dev_required
def version_add(request, addon_id, addon):
    if request.method == 'POST':
        upload = get_object_or_404(FileUpload, pk=request.POST['upload'])
        v = Version.from_upload(upload, addon)
        return redirect('devhub.versions.edit', addon.id, v.id)


@json_view
@dev_required
@post_required
def version_add_file(request, addon_id, addon, version_id):
    version = get_object_or_404(Version, pk=version_id, addon=addon)
    form = forms.NewFileForm(request.POST)
    if not form.is_valid():
        return json_view.error(json.dumps(form.errors))
    upload = get_object_or_404(FileUpload, pk=form.cleaned_data['upload'])
    File.from_upload(upload, version, form.cleaned_data['platform'])
    file_form = forms.FileFormSet(prefix='files', queryset=version.files.all())
    # TODO (jbalogh): get the right form
    return jingo.render(request, 'devhub/includes/version_file.html',
                        {'form': file_form.forms[-1]})


@dev_required
def version_list(request, addon_id, addon):
    qs = addon.versions.order_by('-created').transform(Version.transformer)
    versions = amo.utils.paginate(request, qs)

    data = {'addon': addon,
            'versions': versions,
            'addon_status': amo.STATUS_CHOICES[addon.status],
           }

    return jingo.render(request, 'devhub/addons/versions.html', data)


@dev_required
def version_bounce(request, addon_id, addon, version):
    # Use filter since there could be dupes.
    vs = (Version.objects.filter(version=version, addon=addon)
          .order_by('-created'))
    if vs:
        return redirect('devhub.versions.edit', addon_id, vs[0].id)
    else:
        raise http.Http404()


Step = collections.namedtuple('Step', 'current max')


def submit_step(step):
    """Wraps the function with a decorator that bounces to the right step."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(request, *args, **kw):
            max_step = 7
            # We only bounce on pages with an addon id.
            if 'addon_id' in kw:
                addon_id = kw['addon_id']
                on_step = SubmitStep.objects.filter(addon=addon_id)
                if on_step:
                    max_step = on_step[0].step
                    if max_step < step:
                        # The step was too high, so bounce to the saved step.
                        return redirect('devhub.submit.%s' % max_step,
                                        addon_id)
                elif step != max_step:
                    # We couldn't find a step, so we must be done.
                    return redirect('devhub.submit.7', addon_id)
            kw['step'] = Step(step, max_step)
            return f(request, *args, **kw)
        return wrapper
    return decorator


@login_required
@submit_step(1)
def submit(request, step):
    if request.method == 'POST':
        response = redirect('devhub.submit.2')
        response.set_cookie(DEV_AGREEMENT_COOKIE)
        return response

    base = os.path.join(os.path.dirname(amo.__file__), '..', '..', 'locale')
    # Note that the agreement is not localized (for legal reasons)
    # but the official version is stored in en_US.
    agrmt = os.path.join(base,
                'en_US', 'pages', 'docs', 'policies', 'agreement.thtml')
    f = codecs.open(agrmt, encoding='utf8')
    # The %1$s is a placeholder in the template shared by Remora.
    # There is currently only one of them.
    agreement_text = f.read().replace(u'%1$s',
                                      remora_url('/pages/developer_faq'), 1)
    f.close()

    return jingo.render(request, 'devhub/addons/submit/start.html',
                        {'agreement_text': agreement_text, 'step': step})


@login_required
@submit_step(2)
def submit_addon(request, step):
    if DEV_AGREEMENT_COOKIE not in request.COOKIES:
        return redirect('devhub.submit.1')
    if request.method == 'POST':
        upload = get_object_or_404(FileUpload, pk=request.POST['upload'])
        addon = Addon.from_upload(upload)
        AddonUser(addon=addon, user=request.amo_user).save()
        SubmitStep.objects.create(addon=addon, step=2)
        return redirect('devhub.submit.3', addon.id)
    return jingo.render(request, 'devhub/addons/submit/start.html',
                        {'agreement_text': 'xx', 'step': step})


@dev_required
@submit_step(3)
def submit_describe(request, addon_id, addon, step):
    form = addon_forms.AddonFormBasic(request.POST or None, instance=addon,
                                      request=request)
    if request.method == 'POST' and form.is_valid():
        addon = form.save(addon)
        amo.log(amo.LOG.EDIT_DESCRIPTIONS, addon)
        SubmitStep.objects.filter(addon=addon).update(step=4)
        return redirect('devhub.submit.4', addon.id)

    return jingo.render(request, 'devhub/addons/submit/describe.html',
                        {'form': form, 'addon': addon, 'step': step})


@dev_required
@submit_step(5)
def submit_license(request, addon_id, addon, step):
    fs, ctx = [], {}
    # Versions.
    ctx.update(_license_form(request, addon))
    fs.append(ctx['license_form'])
    # Policy.
    policy_form = _policy_form(request, addon)
    fs.append(policy_form)
    if request.method == 'POST' and all([form.is_valid() for form in fs]):
        _license_form(request, addon, save=True)
        _policy_form(request, addon, save=True)
        SubmitStep.objects.filter(addon=addon).update(step=6)
        return redirect('devhub.submit.6', addon.id)
    ctx.update(addon=addon, policy_form=policy_form, step=step)
    return jingo.render(request, 'devhub/addons/submit/license.html', ctx)


@dev_required
@submit_step(6)
def submit_select_review(request, addon_id, addon, step):
    review_type_form = forms.ReviewTypeForm(request.POST or None)
    if request.method == 'POST' and review_type_form.is_valid():
        addon.status = review_type_form.cleaned_data['review_type']
        addon.save()
        SubmitStep.objects.filter(addon=addon).delete()
        return redirect('devhub.submit.7', addon_id)
    return jingo.render(request, 'devhub/addons/submit/select-review.html',
                        {'addon': addon, 'review_type_form': review_type_form,
                         'step': step})


@dev_required
@submit_step(7)
def submit_done(request, addon_id, addon, step):
    sp = addon.current_version.supported_platforms
    is_platform_specific = sp != [amo.PLATFORM_ALL]

    return jingo.render(request, 'devhub/addons/submit/done.html',
                        {'addon': addon, 'step': step,
                         'is_platform_specific': is_platform_specific})


@login_required
def submit_bump(request, addon_id):
    if not acl.action_allowed(request, 'Admin', 'EditSubmitStep'):
        return http.HttpResponseForbidden()
    addon = get_object_or_404(Addon, pk=addon_id)
    step = SubmitStep.objects.filter(addon=addon)
    step = step[0] if step else None
    if request.method == 'POST' and request.POST.get('step'):
        new_step = request.POST['step']
        if step:
            step.step = new_step
        else:
            step = SubmitStep(addon=addon, step=new_step)
        step.save()
        return redirect('devhub.submit.bump', addon.id)
    return jingo.render(request, 'devhub/addons/submit/bump.html',
                        dict(addon=addon, step=step))
