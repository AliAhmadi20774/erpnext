from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from demo.professional_demo import TAG, ensure_professional_demo
from demo.models import Order


class Command(BaseCommand):
    help = "Add the connected PX-2400 industrial presentation dataset; keep existing records."

    def handle(self, *args, **options):
        try:
            created = ensure_professional_demo()
        except ValidationError as exc:
            raise CommandError(" | ".join(exc.messages)) from exc
        order = Order.objects.get(notes=TAG)
        label = "Industrial presentation created" if created else "Existing industrial presentation preserved"
        self.stdout.write(self.style.SUCCESS(f"{label}: {order.number}; /orders/{order.pk}/detail/"))
