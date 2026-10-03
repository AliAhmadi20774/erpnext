from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DecisionAmountUpgradeTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        self.latest = executor.loader.graph.leaf_nodes()
        self.previous = [('demo', '0021_preserve_completed_operations')]
        executor.migrate(self.previous)
        self.old_apps = executor.loader.project_state(self.previous).apps
        self.bad_pk = None

    def tearDown(self):
        # Only test fixtures are corrected here so a refusal cannot leave the
        # shared test database behind the current schema for later tests.
        if self.bad_pk is not None:
            with connection.cursor() as cursor:
                cursor.execute('UPDATE demo_fitgapitem SET cost_low = 0 WHERE id = %s', [self.bad_pk])
        MigrationExecutor(connection).migrate(self.latest)
        super().tearDown()

    def make_old_need(self, decision, value):
        return self.old_apps.get_model('demo', 'FitGapItem').objects.create(
            decision=decision, area='sales', title='Historical cost', requirement='Migration QA',
            cost_low=value, cost_high=value)

    def test_upgrade_preserves_exact_legacy_integers_nulls_and_audit(self):
        Decision = self.old_apps.get_model('demo', 'ManagementDecision')
        Audit = self.old_apps.get_model('demo', 'AuditEvent')
        amount = 999999999999999999
        decision = Decision.objects.create(budget_ceiling=amount)
        ids = [self.make_old_need(decision, value).pk for value in (None, 0, amount)]
        event = Audit.objects.create(action='fit_gap_saved', object_type='fitgapitem',
                                     object_id=str(ids[-1]), details={'cost_low': str(amount)})
        MigrationExecutor(connection).migrate(self.latest)
        from .models import AuditEvent, FitGapItem, ManagementDecision
        self.assertEqual(ManagementDecision.objects.get(pk=decision.pk).budget_ceiling, amount)
        self.assertEqual(list(FitGapItem.objects.filter(pk__in=ids).order_by('pk')
                              .values_list('cost_low', 'cost_high')), [(None, None), (0, 0), (amount, amount)])
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).details, {'cost_low': str(amount)})
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_unsafe_legacy_amount_is_refused_before_schema_or_records_change(self):
        decision = self.old_apps.get_model('demo', 'ManagementDecision').objects.create()
        self.bad_pk = self.make_old_need(decision, 10).pk
        with connection.cursor() as cursor:
            cursor.execute('UPDATE demo_fitgapitem SET cost_low = 1.5 WHERE id = %s', [self.bad_pk])
        with self.assertRaisesMessage(RuntimeError, 'Unsafe existing amount'):
            MigrationExecutor(connection).migrate(self.latest)
        with connection.cursor() as cursor:
            cursor.execute('SELECT cost_low, cost_high FROM demo_fitgapitem WHERE id = %s', [self.bad_pk])
            self.assertEqual(cursor.fetchone(), (1.5, 10))
            cursor.execute('PRAGMA table_info(demo_fitgapitem)')
            self.assertEqual({row[1]: row[2].lower() for row in cursor.fetchall()}['cost_low'], 'decimal')
