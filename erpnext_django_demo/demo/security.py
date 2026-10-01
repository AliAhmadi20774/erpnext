import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from .access import ROLE_FINANCE, ROLE_INVENTORY, ROLE_MANAGER, ROLE_PURCHASE, ROLE_SALES


DEMO_USERS = (
    ("manager", "مدیر سیستم", ROLE_MANAGER),
    ("sales", "کارشناس فروش", ROLE_SALES),
    ("purchase", "کارشناس خرید", ROLE_PURCHASE),
    ("warehouse", "مسئول انبار", ROLE_INVENTORY),
    ("finance", "کارشناس مالی", ROLE_FINANCE),
)


def ensure_demo_users(*, reset_passwords=False):
    password = os.environ.get("ERP_DEMO_PASSWORD", "Demo-1405!")
    user_model = get_user_model()
    result = []
    for username, display_name, role in DEMO_USERS:
        group, _ = Group.objects.get_or_create(name=role)
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={"first_name": display_name, "is_active": True},
        )
        changed = False
        if created or reset_passwords:
            user.set_password(password)
            changed = True
        if not user.first_name:
            user.first_name = display_name
            changed = True
        if changed:
            user.save()
        user.groups.add(group)
        result.append((user, created))
    return result
