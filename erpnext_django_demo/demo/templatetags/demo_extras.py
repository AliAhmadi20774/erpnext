from django import template

register = template.Library()


@register.filter
def money(value):
    return f"{value:,.0f}" if value is not None else "۰"

