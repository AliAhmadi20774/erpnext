from django.urls import path
from . import views

app_name = "demo"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("customers/", views.customers, name="customers"),
    path("customers/new/", views.customer_new, name="customer_new"),
    path("customers/<int:pk>/", views.customer_detail, name="customer_detail"),
    path("customers/<int:pk>/edit/", views.customer_edit, name="customer_edit"),
    path("customers/<int:pk>/toggle/", views.customer_toggle, name="customer_toggle"),
    path("suppliers/", views.suppliers, name="suppliers"),
    path("suppliers/new/", views.supplier_new, name="supplier_new"),
    path("suppliers/<int:pk>/", views.supplier_detail, name="supplier_detail"),
    path("suppliers/<int:pk>/edit/", views.supplier_edit, name="supplier_edit"),
    path("suppliers/<int:pk>/toggle/", views.supplier_toggle, name="supplier_toggle"),
    path("items/", views.items, name="items"),
    path("items/new/", views.item_new, name="item_new"),
    path("items/<int:pk>/", views.item_detail, name="item_detail"),
    path("items/<int:pk>/edit/", views.item_edit, name="item_edit"),
    path("items/<int:pk>/toggle/", views.item_toggle, name="item_toggle"),
    path("orders/<str:kind>/", views.orders, name="orders"),
    path("orders/<str:kind>/new/", views.order_new, name="order_new"),
    path("orders/<int:pk>/detail/", views.order_detail, name="order_detail"),
    path("orders/<int:pk>/confirm/", views.order_confirm, name="order_confirm"),
    path("inventory/", views.inventory, name="inventory"),
]

