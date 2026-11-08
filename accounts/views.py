from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from . import access
from .forms import PasswordResetForm, UserCreateForm, UserEditForm

User = get_user_model()
FORM_TPL = "accounts/user_form.html"


class AdminRequiredMixin(UserPassesTestMixin):
    """Admins only. Authenticated non-admins get 403 (login enforced by middleware)."""
    raise_exception = True

    def test_func(self):
        u = self.request.user
        return u.is_authenticated and u.is_active and u.is_superuser


class UserListView(AdminRequiredMixin, View):
    def get(self, request):
        users = User.objects.order_by("username").prefetch_related("groups")
        rows = [(u, access.access_level(u)) for u in users]
        return render(request, "accounts/user_list.html", {"rows": rows})


class UserCreateView(AdminRequiredMixin, View):
    def get(self, request):
        return render(request, FORM_TPL, {"form": UserCreateForm(), "title": "New user"})

    def post(self, request):
        form = UserCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"User {user.username} created.")
            return redirect("user_list")
        return render(request, FORM_TPL, {"form": form, "title": "New user"})


class UserEditView(AdminRequiredMixin, View):
    def _render(self, request, form, user):
        return render(request, FORM_TPL, {"form": form, "title": f"Edit {user.username}", "target": user})

    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        return self._render(request, UserEditForm(user=user), user)

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        form = UserEditForm(request.POST, user=user)
        if form.is_valid():
            if user == request.user and form.cleaned_data["access_level"] != access.ADMIN:
                form.add_error("access_level", "You cannot remove your own admin access.")
            else:
                form.save()
                messages.success(request, f"User {user.username} updated.")
                return redirect("user_list")
        return self._render(request, form, user)


class UserPasswordView(AdminRequiredMixin, View):
    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        return render(request, FORM_TPL, {"form": PasswordResetForm(user=user), "title": f"Reset password: {user.username}"})

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        form = PasswordResetForm(request.POST, user=user)
        if form.is_valid():
            form.save()
            messages.success(request, f"Password reset for {user.username}.")
            return redirect("user_list")
        return render(request, FORM_TPL, {"form": form, "title": f"Reset password: {user.username}"})


class UserToggleActiveView(AdminRequiredMixin, View):
    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        if user == request.user:
            messages.error(request, "You cannot deactivate your own account.")
        else:
            user.is_active = not user.is_active
            user.save(update_fields=["is_active"])
            messages.success(request, f"{user.username} is now {'active' if user.is_active else 'deactivated'}.")
        return redirect("user_list")
