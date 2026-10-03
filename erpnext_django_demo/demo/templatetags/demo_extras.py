from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import jdatetime
from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def money(value):
    try:
        return fa_number(f"{Decimal(str(value)):,.0f}" if value is not None else "0")
    except (InvalidOperation, ValueError):
        return "—"


@register.filter
def fa_number(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


@register.filter
def jalali_date(value):
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return "—"
    if not isinstance(value, (date, datetime)):
        return "—"
    if isinstance(value, datetime):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    result = jdatetime.date.fromgregorian(date=value)
    return fa_number(f"{result.year:04d}/{result.month:02d}/{result.day:02d}")


@register.filter
def jalali_datetime(value):
    if not isinstance(value, datetime):
        return "—"
    local_value = timezone.localtime(value) if timezone.is_aware(value) else value
    return f"{jalali_date(local_value.date())} - {fa_number(local_value.strftime('%H:%M'))}"


@register.simple_tag
def current_jalali():
    return jalali_date(timezone.localdate())

