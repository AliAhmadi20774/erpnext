from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from demo.models import Customer, Fulfillment, Invoice, Item, Order, OrderLine, Payment, StockMovement, Supplier
from demo.services import confirm_order, fulfill_order, issue_invoice, record_opening_stock, record_payment


class Command(BaseCommand):
    help = "Create sample customers, suppliers, items and order history for the management demo."

    def handle(self, *args, **options):
        if Customer.objects.exists() or Item.objects.exists() or Order.objects.exists():
            self.stdout.write(self.style.WARNING("Database already has data; nothing was changed."))
            return

        customers = [Customer.objects.create(**data) for data in [
            {"name": "فناوری آریا", "code": "CUS-001", "contact": "خانم رستمی", "phone": "021-88741020", "city": "تهران"},
            {"name": "گروه صنعتی پارس", "code": "CUS-002", "contact": "آقای حیدری", "phone": "026-34011230", "city": "کرج"},
            {"name": "تجارت سپهر", "code": "CUS-003", "contact": "خانم نادری", "phone": "031-36621580", "city": "اصفهان"},
            {"name": "راهکار نوین", "code": "CUS-004", "contact": "آقای شریفی", "phone": "051-38741200", "city": "مشهد"},
            {"name": "شرکت داده‌پردازان", "code": "CUS-005", "contact": "آقای کریمی", "phone": "021-44273110", "city": "تهران"},
            {"name": "فروشگاه مهر", "code": "CUS-006", "contact": "خانم اسدی", "phone": "071-36382900", "city": "شیراز"},
        ]]
        suppliers = [Supplier.objects.create(**data) for data in [
            {"name": "تامین کالای البرز", "code": "SUP-001", "contact": "آقای رضایی", "phone": "021-66721000", "city": "تهران"},
            {"name": "پخش آرمان", "code": "SUP-002", "contact": "خانم زمانی", "phone": "026-32242000", "city": "کرج"},
            {"name": "صنایع نوآور", "code": "SUP-003", "contact": "آقای عباسی", "phone": "031-33391110", "city": "اصفهان"},
        ]]
        products = [
            ("لپ‌تاپ سازمانی A14", "IT-101", "تجهیزات رایانه", 42000000, 35500000, 30, 8),
            ("مانیتور ۲۴ اینچ", "IT-102", "تجهیزات رایانه", 9800000, 8100000, 45, 10),
            ("کیبورد بی‌سیم", "IT-103", "لوازم جانبی", 1850000, 1320000, 18, 10),
            ("ماوس ارگونومیک", "IT-104", "لوازم جانبی", 1250000, 890000, 14, 10),
            ("پرینتر لیزری", "IT-105", "تجهیزات اداری", 17600000, 14900000, 13, 5),
            ("اسکنر رومیزی", "IT-106", "تجهیزات اداری", 11900000, 9700000, 9, 5),
            ("هدست اداری", "IT-107", "لوازم جانبی", 2950000, 2200000, 8, 10),
            ("هارد اکسترنال ۱ ترابایت", "IT-108", "ذخیره‌سازی", 5400000, 4500000, 15, 6),
            ("روتر شبکه", "IT-109", "تجهیزات شبکه", 8900000, 7300000, 7, 8),
            ("سوئیچ شبکه ۸ پورت", "IT-110", "تجهیزات شبکه", 7100000, 5800000, 16, 5),
        ]
        items = []
        for name, sku, category, sale, purchase, stock, level in products:
            item = Item.objects.create(name=name, sku=sku, category=category,
                                       sale_price=sale, purchase_price=purchase,
                                       stock=0, reorder_level=level)
            record_opening_stock(item.pk, stock)
            item.refresh_from_db()
            items.append(item)

        now = timezone.localtime()

        def historical_time(months_ago, day):
            year = now.year
            month = now.month - months_ago
            while month <= 0:
                year -= 1
                month += 12
            return timezone.make_aware(datetime(year, month, min(day, 25), 10, 30))

        StockMovement.objects.filter(source=StockMovement.OPENING).update(created_at=historical_time(6, 1))

        def add_order(kind, party, rows, months_ago, day, confirm=True):
            order = Order.objects.create(kind=kind, customer=party if kind == Order.SALES else None,
                                         supplier=party if kind == Order.PURCHASE else None)
            for index, quantity in rows:
                item = items[index]
                OrderLine.objects.create(order=order, item=item, quantity=quantity,
                                         unit_price=item.sale_price if kind == Order.SALES else item.purchase_price)
            if confirm:
                confirm_order(order.pk)
                fulfill_order(order.pk)
            stamp = historical_time(months_ago, day)
            Order.objects.filter(pk=order.pk).update(created_at=stamp,
                                                      confirmed_at=stamp if confirm else None)
            if confirm:
                Fulfillment.objects.filter(order=order).update(completed_at=stamp)
                StockMovement.objects.filter(order=order).update(created_at=stamp)

        sales_orders = [
            (5, 8, 0, [(0, 2), (1, 3)]),
            (4, 11, 1, [(0, 3), (2, 5)]),
            (4, 21, 2, [(4, 2), (3, 4)]),
            (3, 13, 3, [(0, 4), (1, 4)]),
            (2, 9, 4, [(4, 3), (7, 3)]),
            (2, 23, 1, [(0, 2), (8, 2)]),
            (1, 10, 5, [(0, 3), (6, 2)]),
            (1, 20, 0, [(1, 5), (2, 4)]),
            (0, 5, 2, [(0, 3), (7, 2)]),
            (0, 14, 3, [(8, 2), (9, 4)]),
        ]

        purchase_orders = [
            (4, 6, 0, [(1, 8), (3, 8)]),
            (2, 4, 1, [(2, 6), (7, 7)]),
            (1, 7, 2, [(4, 4), (9, 5)]),
            (0, 12, 0, [(0, 5), (5, 3)]),
        ]
        events = ([(months_ago, day, Order.SALES, customers[index], rows)
                   for months_ago, day, index, rows in sales_orders]
                  + [(months_ago, day, Order.PURCHASE, suppliers[index], rows)
                     for months_ago, day, index, rows in purchase_orders])
        for months_ago, day, kind, party, rows in sorted(events, key=lambda event: historical_time(event[0], event[1])):
            add_order(kind, party, rows, months_ago, day)
        add_order(Order.SALES, customers[4], [(1, 2), (6, 1)], 0, 18, confirm=False)
        for index, order in enumerate(Order.objects.filter(kind=Order.SALES, status=Order.CONFIRMED).order_by("pk")[:7]):
            invoice = issue_invoice(order.pk)
            Invoice.objects.filter(pk=invoice.pk).update(issued_at=order.confirmed_at)
            if index < 5:
                payment = record_payment(invoice.pk, invoice.amount, f"DEMO-{index + 1:03d}")
            elif index == 5:
                payment = record_payment(invoice.pk, invoice.amount / 2, "DEMO-006")
            else:
                continue
            Payment.objects.filter(pk=payment.pk).update(paid_at=order.confirmed_at + timedelta(days=1))
        for index, order in enumerate(Order.objects.filter(kind=Order.PURCHASE, status=Order.CONFIRMED).order_by("pk")[:3]):
            invoice = issue_invoice(order.pk)
            Invoice.objects.filter(pk=invoice.pk).update(issued_at=order.confirmed_at)
            amount = invoice.amount if index < 2 else invoice.amount / 2
            payment = record_payment(invoice.pk, amount, f"BUY-{index + 1:03d}")
            Payment.objects.filter(pk=payment.pk).update(paid_at=order.confirmed_at + timedelta(days=1))
        self.stdout.write(self.style.SUCCESS("Demo data created: 6 customers, 3 suppliers, 10 items, 15 orders."))

