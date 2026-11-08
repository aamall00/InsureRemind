from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/new/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", views.UserEditView.as_view(), name="user_edit"),
    path("users/<int:pk>/password/", views.UserPasswordView.as_view(), name="user_password"),
    path("users/<int:pk>/toggle-active/", views.UserToggleActiveView.as_view(), name="user_toggle_active"),
]
