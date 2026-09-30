from django.urls import path
from . import views

app_name = "demo"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("customers/", views.customers, name="customers"),
    path("customers/new/", views.customer_new, name="customer_new"),
    path("suppliers/", views.suppliers, name="suppliers"),
    path("suppliers/new/", views.supplier_new, name="supplier_new"),
    path("items/", views.items, name="items"),
    path("items/new/", views.item_new, name="item_new"),
    path("orders/<str:kind>/", views.orders, name="orders"),
    path("orders/<str:kind>/new/", views.order_new, name="order_new"),
    path("orders/<int:pk>/detail/", views.order_detail, name="order_detail"),
    path("orders/<int:pk>/confirm/", views.order_confirm, name="order_confirm"),
    path("inventory/", views.inventory, name="inventory"),
]

