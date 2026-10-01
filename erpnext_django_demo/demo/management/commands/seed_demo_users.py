from django.core.management.base import BaseCommand

from demo.security import ensure_demo_users


class Command(BaseCommand):
    help = "Create the five local demo users and their least-privilege roles."

    def add_arguments(self, parser):
        parser.add_argument("--reset-passwords", action="store_true")

    def handle(self, *args, **options):
        rows = ensure_demo_users(reset_passwords=options["reset_passwords"])
        created = sum(1 for _, is_created in rows if is_created)
        self.stdout.write(self.style.SUCCESS(f"Demo roles ready; {created} user(s) created."))
