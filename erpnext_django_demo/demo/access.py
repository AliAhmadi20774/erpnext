from functools import wraps

from django.core.exceptions import PermissionDenied
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import get_object_or_404

from .models import Order


ROLE_MANAGER = "erp_manager"
ROLE_SALES = "erp_sales"
ROLE_PURCHASE = "erp_purchase"
ROLE_INVENTORY = "erp_inventory"

ROLE_LABELS = {
    ROLE_MANAGER: "مدیر",
    ROLE_SALES: "فروش",
    ROLE_PURCHASE: "خرید",
    ROLE_INVENTORY: "انبار",
}


def user_roles(user):
    if not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set(ROLE_LABELS)
    return set(user.groups.filter(name__in=ROLE_LABELS).values_list("name", flat=True))


def has_role(user, *roles):
    return bool(user_roles(user).intersection(roles))


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not has_role(request.user, *roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def order_kind_roles(kind, include_inventory=False):
    roles = [ROLE_MANAGER, ROLE_SALES if kind == Order.SALES else ROLE_PURCHASE]
    if include_inventory:
        roles.append(ROLE_INVENTORY)
    return roles


def require_order_access(user, order, *, include_inventory=False):
    if not has_role(user, *order_kind_roles(order.kind, include_inventory=include_inventory)):
        raise PermissionDenied


def require_order_kind_access(user, kind, *, include_inventory=False):
    if not has_role(user, *order_kind_roles(kind, include_inventory=include_inventory)):
        raise PermissionDenied


def order_access_required(*, include_inventory=False):
    def decorator(view):
        @wraps(view)
        def wrapped(request, pk, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            order = get_object_or_404(Order, pk=pk)
            require_order_access(request.user, order, include_inventory=include_inventory)
            return view(request, pk, *args, **kwargs)

        return wrapped

    return decorator


def role_context(request):
    roles = user_roles(request.user)
    manager = ROLE_MANAGER in roles
    return {
        "access": {
            "manager": manager,
            "sales": manager or ROLE_SALES in roles,
            "purchase": manager or ROLE_PURCHASE in roles,
            "inventory": manager or ROLE_INVENTORY in roles,
            "reports": manager,
            "manage_items": manager or ROLE_INVENTORY in roles,
            "role_label": next((label for role, label in ROLE_LABELS.items() if role in roles), "کاربر"),
        }
    }
