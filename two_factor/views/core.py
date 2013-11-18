from binascii import unhexlify

from django.conf import settings
from django.contrib.auth import login as login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.formtools.wizard.views import SessionWizardView
from django.contrib.sites.models import get_current_site
from django.forms import Form
from django.shortcuts import redirect
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.generic import FormView, DeleteView, TemplateView
from django_otp.decorators import otp_required
from django_otp.plugins.otp_static.models import StaticToken
from django_otp.util import random_hex

from ..compat import Django16Compat
from ..forms import (MethodForm, TOTPDeviceForm, PhoneForm,
                     DeviceValidationForm, AuthenticationTokenForm)
from ..models import PhoneDevice
from ..utils import (get_qr_url, default_device,
                     backup_phones)
from .utils import (IdempotentSessionWizardView, class_view_decorator)


@class_view_decorator(never_cache)
class LoginView(Django16Compat, IdempotentSessionWizardView):
    template_name = 'two_factor/core/login.html'
    form_list = (
        ('auth', AuthenticationForm),
        ('token', AuthenticationTokenForm),
    )
    idempotent_dict = {
        'token': False,
    }
    condition_dict = {
        'token': lambda self: default_device(self.get_user()),
    }

    def __init__(self, **kwargs):
        super(LoginView, self).__init__(**kwargs)
        self.user_cache = None
        self.device_cache = None

    def post(self, *args, **kwargs):
        """
        The user can select a particular device to challenge, being the backup
        devices added to the account.
        """
        if 'challenge_device' in self.request.POST:
            return self.render_goto_step('token')
        return super(LoginView, self).post(*args, **kwargs)

    def done(self, form_list, **kwargs):
        login(self.request, self.get_user())
        return redirect(str(settings.LOGIN_REDIRECT_URL))

    def get_form_kwargs(self, step=None):
        if step == 'token':
            return {
                'user': self.get_user(),
            }
        return {}

    def get_device(self):
        if not self.device_cache:
            challenge_device_id = self.request.POST.get('challenge_device', None)
            if challenge_device_id:
                for device in backup_phones(self.get_user()):
                    if device.persistent_id == challenge_device_id:
                        self.device_cache = device
                        break
            if not self.device_cache:
                self.device_cache = default_device(self.get_user())
        return self.device_cache

    def render(self, form=None, **kwargs):
        if self.steps.current == 'token':
            self.get_device().generate_challenge()
        return super(LoginView, self).render(form, **kwargs)

    def get_user(self):
        if not self.user_cache:
            form_obj = self.get_form(step='auth',
                                     data=self.storage.get_step_data('auth'),
                                     files=self.storage.get_step_files('auth'))
            self.user_cache = form_obj.is_valid() and form_obj.user_cache
        return self.user_cache

    def get_context_data(self, form, **kwargs):
        context = super(LoginView, self).get_context_data(form, **kwargs)
        if self.steps.current == 'token':
            device = self.get_device()
            context['device'] = device
            if isinstance(device, PhoneDevice):
                if device.method == 'call':
                    context['instructions'] = _(
                        'We are calling your phone right now, please enter '
                        'the digits you hear.')
                else:
                    context['instructions'] = _(
                        'We sent you a text message, please enter the tokens '
                        'we sent.')
            else:
                context['instructions'] = _(
                    'Please enter the tokens generated by your token '
                    'generator.')

            context['other_devices'] = [
                phone for phone in backup_phones(self.get_user())
                if phone != device]
        context['cancel_url'] = settings.LOGOUT_URL
        return context


@class_view_decorator(never_cache)
@class_view_decorator(login_required)
class SetupView(Django16Compat, SessionWizardView):
    template_name = 'two_factor/core/setup.html'
    initial_dict = {}
    form_list = (
        ('welcome', Form),
        ('method', MethodForm),
        ('generator', TOTPDeviceForm),
        #('sms', PhoneForm),
        #('sms-verify', TokenVerificationForm),
        #('call', PhoneForm),
        #('call-verify', TokenVerificationForm),
    )

    def get(self, request, *args, **kwargs):
        """
        Start the setup wizard. Redirect if already enabled.
        """
        if default_device(self.request.user):
            return redirect('two_factor:setup_complete')
        return super(SetupView, self).get(request, *args, **kwargs)

    def done(self, form_list, **kwargs):
        """
        Finish the wizard. Save all forms and redirect.
        """
        for form in form_list:
            if callable(getattr(form, 'save', None)):
                form.save()
        return redirect('two_factor:setup_complete')

    def get_form_kwargs(self, step=None):
        kwargs = {}
        if step == 'generator':
            kwargs.update({
                'key': self.get_key(step),
                'user': self.request.user,
            })
        metadata = self.get_form_metadata(step)
        if metadata:
            kwargs.update({
                'metadata': metadata,
            })
        return kwargs

    def get_key(self, step):
        self.storage.extra_data.setdefault('keys', {})
        if step in self.storage.extra_data['keys']:
            return self.storage.extra_data['keys'].get(step)
        key = random_hex(20).decode('ascii')
        self.storage.extra_data['keys'][step] = key
        return key

    def get_context_data(self, form, **kwargs):
        context = super(SetupView, self).get_context_data(form, **kwargs)
        if self.steps.current == 'generator':
            alias = '%s@%s' % (self.request.user.username,
                               get_current_site(self.request).name)
            key = unhexlify(self.get_key('generator').encode('ascii'))
            context.update({
                'QR_URL': get_qr_url(alias, key)
            })
        context['cancel_url'] = settings.LOGIN_REDIRECT_URL
        return context

    def process_step(self, form):
        if hasattr(form, 'metadata'):
            self.storage.extra_data.setdefault('forms', {})
            self.storage.extra_data['forms'][self.steps.current] = form.metadata
        return super(SetupView, self).process_step(form)

    def get_form_metadata(self, step):
        # Django 1.4 requires a little more work than simply using
        # `setdefault`; `_get_extra_data` returns a new dict instance on
        # access if it is empty, so we have to force `_set_extra_data` in this
        # case.
        if not self.storage.extra_data:
            self.storage.extra_data = {'forms': {}}
        else:
            self.storage.extra_data.setdefault('forms', {})
        return self.storage.extra_data['forms'].get(step, None)


@class_view_decorator(never_cache)
@class_view_decorator(otp_required)
class BackupTokensView(FormView):
    form_class = Form
    template_name = 'two_factor/core/backup_tokens.html'

    def get_device(self):
        return self.request.user.staticdevice_set.get_or_create(name='backup')[0]

    def get_context_data(self, **kwargs):
        context = super(BackupTokensView, self).get_context_data(**kwargs)
        context['device'] = self.get_device()
        return context

    def form_valid(self, form):
        self.get_device().token_set.all().delete()
        for n in range(10):
            self.get_device().token_set.create(token=StaticToken.random_token())

        return redirect('two_factor:backup_tokens')


@class_view_decorator(never_cache)
@class_view_decorator(otp_required)
class PhoneSetupView(Django16Compat, IdempotentSessionWizardView):
    """
    Configures and validated a `PhoneDevice` for the logged in user.
    """
    template_name = 'two_factor/core/phone_register.html'
    form_list = (
        ('setup', PhoneForm),
        ('validation', DeviceValidationForm),
    )
    key_name = 'key'

    def done(self, form_list, **kwargs):
        """
        Store the device and redirect to profile page.
        """
        self.get_device(user=self.request.user, name='backup').save()
        return redirect(str(settings.LOGIN_REDIRECT_URL))

    def render_next_step(self, form, **kwargs):
        """
        In the validation step, ask the device to generate a challenge.
        """
        next_step = self.steps.next
        if next_step == 'validation':
            self.get_device().generate_challenge()
        return super(PhoneSetupView, self).render_next_step(form, **kwargs)

    def get_form_kwargs(self, step=None):
        """
        Provide the device to the DeviceValidationForm.
        """
        if step == 'validation':
            return {'device': self.get_device()}
        return {}

    def get_device(self, **kwargs):
        """
        Uses the data from the setup step and generated key to recreate device.
        """
        kwargs = kwargs or {}
        kwargs.update(self.storage.validated_step_data.get('setup', {}))
        return PhoneDevice(key=self.get_key(), **kwargs)

    def get_key(self):
        """
        The key is preserved between steps and stored as ascii in the session.
        """
        if not self.key_name in self.storage.extra_data:
            key = random_hex(20).decode('ascii')
            self.storage.extra_data[self.key_name] = key
        return self.storage.extra_data[self.key_name]

    def get_context_data(self, form, **kwargs):
        kwargs.setdefault('cancel_url', settings.LOGIN_REDIRECT_URL)
        return super(PhoneSetupView, self).get_context_data(form, **kwargs)


@class_view_decorator(never_cache)
@class_view_decorator(otp_required)
class PhoneDeleteView(DeleteView):
    success_url = settings.LOGIN_REDIRECT_URL

    def get_queryset(self):
        return self.request.user.phonedevice_set.filter(name='backup')


@class_view_decorator(never_cache)
@class_view_decorator(login_required)
class SetupCompleteView(TemplateView):
    template_name = 'two_factor/core/setup_complete.html'
