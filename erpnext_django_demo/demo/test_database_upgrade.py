from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone
from django.urls import reverse

from .tests import AuthenticatedTestCase


LEGACY_NAMES = ('0005_auditevent', '0006_alter_auditevent_action', '0007_backfill_audit')


class LegacyDatabaseUpgradeTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        self.latest = executor.loader.graph.leaf_nodes()
        self.old_target = [('demo', '0004_stockmovement_balance_after_and_more')]
        executor.migrate(self.old_target)
        self.old_apps = executor.loader.project_state(self.old_target).apps
        for name in LEGACY_NAMES:
            MigrationRecorder(connection).record_applied('demo', name)

    def tearDown(self):
        # Restore the test database's normal schema even after a refusal assertion.
        applied = MigrationRecorder(connection).applied_migrations()
        if ('demo', '0005_payment_idempotency_key_auditevent') not in applied:
            with connection.cursor() as cursor:
                cursor.execute('DROP TABLE IF EXISTS demo_auditevent')
                cursor.execute('DROP TABLE IF EXISTS demo_legacy_auditevent')
        MigrationExecutor(connection).migrate(self.latest)
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS demo_legacy_auditevent')
        for name in LEGACY_NAMES:
            MigrationRecorder(connection).record_unapplied('demo', name)
        super().tearDown()

    def create_legacy_table(self):
        with connection.cursor() as cursor:
            cursor.execute('''CREATE TABLE demo_auditevent (
                id integer PRIMARY KEY AUTOINCREMENT, action varchar(32) NOT NULL,
                reference varchar(80) NOT NULL, description varchar(255) NOT NULL,
                created_at datetime NOT NULL,
                actor_id integer NULL REFERENCES auth_user(id) DEFERRABLE INITIALLY DEFERRED)''')
            # This exact index name collides with the current model unless the
            # legacy schema is preserved in a separate, index-independent archive.
            cursor.execute('CREATE INDEX demo_auditevent_actor_id_21a5f883 '
                           'ON demo_auditevent(actor_id)')

    def test_known_legacy_chain_preserves_documents_money_inventory_and_audit(self):
        self.create_legacy_table()
        actor = get_user_model().objects.create_user('legacy-actor')
        stamp = timezone.now().replace(microsecond=123456)
        with connection.cursor() as cursor:
            cursor.execute('INSERT INTO demo_auditevent VALUES (%s,%s,%s,%s,%s,%s)',
                           [7, 'order_fulfill', 'SO-LEGACY', 'Original description', stamp, actor.pk])
        Customer = self.old_apps.get_model('demo', 'Customer')
        Item = self.old_apps.get_model('demo', 'Item')
        Order = self.old_apps.get_model('demo', 'Order')
        Line = self.old_apps.get_model('demo', 'OrderLine')
        Invoice = self.old_apps.get_model('demo', 'Invoice')
        Payment = self.old_apps.get_model('demo', 'Payment')
        Marker = self.old_apps.get_model('demo', 'Fulfillment')
        Movement = self.old_apps.get_model('demo', 'StockMovement')
        customer = Customer.objects.create(name='Preserved customer', code='LEG-C')
        item = Item.objects.create(name='Preserved item', sku='LEG-I', stock=8,
                                   sale_price=150, purchase_price=100)
        Movement.objects.create(item=item, change=10, balance_before=0, balance_after=10,
                                source='opening', created_at=stamp)
        payment_ids = []
        for balance in (9, 8):
            order = Order.objects.create(kind='sales', status='confirmed', customer=customer,
                                          confirmed_at=stamp)
            Line.objects.create(order=order, item=item, quantity=1, unit_price=150)
            Marker.objects.create(order=order, completed_at=stamp)
            Movement.objects.create(item=item, order=order, source='sales', change=-1,
                                    balance_before=balance + 1, balance_after=balance, created_at=stamp)
            invoice = Invoice.objects.create(order=order, amount=150, issued_at=stamp)
            payment_ids.append(Payment.objects.create(invoice=invoice, amount=50,
                                                      reference='Preserved payment', paid_at=stamp).pk)
        executor = MigrationExecutor(connection)
        executor.migrate(self.latest)
        from .models import AuditEvent, FulfillmentBatch, InvoiceCharge, Item as CurrentItem
        from .models import Invoice as CurrentInvoice, JournalEntry, Payment as CurrentPayment
        audit = AuditEvent.objects.get(pk=7)
        self.assertEqual((audit.action, audit.object_type, audit.object_label, audit.actor_id,
                          audit.created_at), ('order_fulfill', 'legacy_audit', 'SO-LEGACY', actor.pk, stamp))
        self.assertEqual(audit.details['legacy_description'], 'Original description')
        self.assertEqual(CurrentItem.objects.get(pk=item.pk).stock, 8)
        self.assertEqual([(row.amount, row.paid, row.balance) for row in CurrentInvoice.objects.all()],
                         [(150, 50, 100), (150, 50, 100)])
        self.assertEqual(list(CurrentPayment.objects.values_list('pk', flat=True)), payment_ids)
        self.assertEqual(len(set(CurrentPayment.objects.values_list('idempotency_key', flat=True))), 2)
        self.assertEqual(FulfillmentBatch.objects.count(), 2)
        self.assertEqual(InvoiceCharge.objects.count(), 2)
        self.assertEqual(JournalEntry.objects.count(), 0)  # upgrade never replays stock or accounting
        with connection.cursor() as cursor:
            cursor.execute('SELECT id,description,actor_id FROM demo_legacy_auditevent')
            self.assertEqual(cursor.fetchall(), [(7, 'Original description', actor.pk)])
        output = StringIO()
        call_command('prepare_demo_database', stdout=output)
        self.assertIn('no migration or backup needed', output.getvalue())
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_unknown_audit_schema_refuses_without_replacing_rows(self):
        with connection.cursor() as cursor:
            cursor.execute('CREATE TABLE demo_auditevent (id integer PRIMARY KEY, payload text)')
            cursor.execute("INSERT INTO demo_auditevent VALUES (1,'Keep this record')")
        with self.assertRaisesMessage(RuntimeError, 'Unrecognized existing audit schema'):
            MigrationExecutor(connection).migrate(self.latest)
        with connection.cursor() as cursor:
            cursor.execute('SELECT payload FROM demo_auditevent')
            self.assertEqual(cursor.fetchall(), [('Keep this record',)])
            self.assertNotIn('demo_legacy_auditevent', connection.introspection.table_names(cursor))


class UpgradeBackupTests(SimpleTestCase):
    def test_failed_backup_prevents_migration(self):
        with TemporaryDirectory() as root:
            database = Path(root) / 'existing.sqlite3'
            database.write_bytes(b'existing database')
            with patch('demo.management.commands.prepare_demo_database.settings') as config:
                config.DATABASES = {'default': {
                    'ENGINE': 'django.db.backends.sqlite3', 'NAME': database}}
                config.BASE_DIR = Path(root)
                with patch('demo.management.commands.prepare_demo_database.MigrationExecutor') as executor, \
                     patch('demo.management.commands.prepare_demo_database.create_sqlite_backup',
                           side_effect=ValueError('Backup destination not writable')), \
                     patch('demo.management.commands.prepare_demo_database.call_command') as migrate:
                    executor.return_value.migration_plan.return_value = [object()]
                    with self.assertRaisesMessage(CommandError, 'migrations were not applied'):
                        call_command('prepare_demo_database', stdout=StringIO())
                    migrate.assert_not_called()
                    self.assertEqual(database.read_bytes(), b'existing database')


class UpgradedAuditPageTests(AuthenticatedTestCase):
    def test_system_events_and_users_without_display_name_render(self):
        from .models import AuditEvent
        AuditEvent.objects.create(action='journal_posted', object_type='journalentry',
                                  object_id='1', object_label='System journal')
        AuditEvent.objects.create(actor=self.manager, action='order_fulfill',
                                  object_type='legacy_audit', object_id='7',
                                  object_label='Legacy audit', details={'legacy_description': 'Original audit'})
        page = self.client.get(reverse('demo:audit_events'))
        self.assertContains(page, 'سیستم')
        self.assertContains(page, self.manager.username)
        self.assertContains(page, 'Original audit')
