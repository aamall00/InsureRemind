from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from policies.utils import normalize_phone

from . import access
from .models import Profile

User = get_user_model()
LEVEL_CHOICES = [(l, l) for l in access.LEVELS]


class _UserFieldsMixin:
    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "").strip()
        return normalize_phone(phone) if phone else ""

    def _apply(self, user):
        parts = self.cleaned_data["full_name"].strip().split(None, 1)
        user.first_name = parts[0]
        user.last_name = parts[1] if len(parts) > 1 else ""
        user.email = self.cleaned_data["email"]


def _check_passwords(form, user):
    data = form.cleaned_data
    p1, p2 = data.get("password1"), data.get("password2")
    if p1 and p2:
        if p1 != p2:
            form.add_error("password2", "Passwords do not match.")
        else:
            try:
                validate_password(p1, user)
            except forms.ValidationError as e:
                form.add_error("password1", e)


class UserCreateForm(_UserFieldsMixin, forms.Form):
    username = forms.CharField(max_length=150)
    full_name = forms.CharField(max_length=200)
    email = forms.EmailField()
    phone = forms.CharField(max_length=20, required=False)
    access_level = forms.ChoiceField(choices=LEVEL_CHOICES, initial=access.READ_ONLY)
    password1 = forms.CharField(widget=forms.PasswordInput, label="Password")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirm password")

    def clean_username(self):
        u = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=u).exists():
            raise forms.ValidationError("A user with that username already exists.")
        return u

    def clean(self):
        data = super().clean()
        _check_passwords(self, User(username=data.get("username", "")))
        return data

    def save(self):
        user = User(username=self.cleaned_data["username"])
        self._apply(user)
        user.set_password(self.cleaned_data["password1"])
        user.save()
        Profile.objects.update_or_create(user=user, defaults={"phone": self.cleaned_data["phone"]})
        access.set_access_level(user, self.cleaned_data["access_level"])
        return user


class UserEditForm(_UserFieldsMixin, forms.Form):
    full_name = forms.CharField(max_length=200)
    email = forms.EmailField()
    phone = forms.CharField(max_length=20, required=False)
    access_level = forms.ChoiceField(choices=LEVEL_CHOICES)

    def __init__(self, *args, user, **kwargs):
        self.user = user
        initial = kwargs.setdefault("initial", {})
        initial.update(
            full_name=user.get_full_name(),
            email=user.email,
            phone=getattr(getattr(user, "profile", None), "phone", ""),
            access_level=access.access_level(user),
        )
        super().__init__(*args, **kwargs)

    def save(self):
        self._apply(self.user)
        self.user.save()
        Profile.objects.update_or_create(user=self.user, defaults={"phone": self.cleaned_data["phone"]})
        access.set_access_level(self.user, self.cleaned_data["access_level"])
        return self.user


class PasswordResetForm(forms.Form):
    password1 = forms.CharField(widget=forms.PasswordInput, label="New password")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirm new password")

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        _check_passwords(self, self.user)
        return data

    def save(self):
        self.user.set_password(self.cleaned_data["password1"])
        self.user.save()
