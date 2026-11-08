from django import template

from accounts.access import access_level as _level

register = template.Library()


@register.filter
def access_level(user):
    return _level(user)
