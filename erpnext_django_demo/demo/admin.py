from django.contrib import admin
from .models import Customer, Item, Order, OrderLine, StockMovement, Supplier

admin.site.register([Customer, Supplier, Item, Order, OrderLine, StockMovement])
