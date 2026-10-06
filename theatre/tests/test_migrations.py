from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from theatre.models import Actor, Genre, Performance, Play, Reservation, TheatreHall, Ticket


class ReservationPerformanceMigrationTests(TransactionTestCase):
    migrate_from = [("theatre", "0002_initial")]
    migrate_to = [("theatre", "0003_reservation_performance")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.apps = executor.loader.project_state(self.migrate_from).apps
        self.addCleanup(self.restore_current_schema)
        User = self.apps.get_model("user", "User")
        Hall = self.apps.get_model("theatre", "TheatreHall")
        Play = self.apps.get_model("theatre", "Play")
        Performance = self.apps.get_model("theatre", "Performance")
        self.Reservation = self.apps.get_model("theatre", "Reservation")
        self.Ticket = self.apps.get_model("theatre", "Ticket")
        user = User.objects.create(username="migration-owner")
        hall = Hall.objects.create(name="Hall", rows=3, seats_in_row=5)
        play = Play.objects.create(title="Play", description="Description")
        self.performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now()
        )
        self.other_performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now()
        )
        self.reservation = self.Reservation.objects.create(user=user)
        self.Ticket.objects.create(
            reservation=self.reservation, performance=self.performance, row=1, seat=1
        )
        self.Ticket.objects.create(
            reservation=self.reservation, performance=self.performance, row=1, seat=2
        )

    def restore_current_schema(self):
        # Only the isolated test database is affected. Remove ambiguous fixtures
        # before restoring the latest schema for Django's test teardown.
        self.Ticket.objects.all().delete()
        self.Reservation.objects.all().delete()
        MigrationExecutor(connection).migrate(self.migrate_to)

    def test_backfill_and_reverse_preserve_existing_data(self):
        tickets_before = list(self.Ticket.objects.order_by("pk").values())
        reservation_before = self.Reservation.objects.values().get(pk=self.reservation.pk)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        Reservation = apps.get_model("theatre", "Reservation")
        reservation = Reservation.objects.get(pk=self.reservation.pk)
        self.assertEqual(reservation.performance_id, self.performance.pk)
        self.assertFalse(Reservation._meta.get_field("performance").null)
        self.assertEqual(list(self.Ticket.objects.order_by("pk").values()), tickets_before)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reservation.objects.create(user_id=reservation.user_id, performance_id=None)
        MigrationExecutor(connection).migrate(self.migrate_from)
        self.assertEqual(
            self.Reservation.objects.values().get(pk=self.reservation.pk), reservation_before
        )
        self.assertEqual(list(self.Ticket.objects.order_by("pk").values()), tickets_before)

    def assert_migration_rejected(self, message):
        tickets_before = list(self.Ticket.objects.order_by("pk").values())
        reservations_before = list(self.Reservation.objects.order_by("pk").values())
        with self.assertRaisesMessage(RuntimeError, message):
            MigrationExecutor(connection).migrate(self.migrate_to)
        self.assertEqual(list(self.Ticket.objects.order_by("pk").values()), tickets_before)
        self.assertEqual(list(self.Reservation.objects.order_by("pk").values()), reservations_before)
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, "theatre_reservation")
        self.assertNotIn("performance_id", [column.name for column in columns])

    def test_reservation_without_tickets_blocks_migration(self):
        empty = self.Reservation.objects.create(user_id=self.reservation.user_id)
        self.assert_migration_rejected(f"Reservations without tickets: [{empty.pk}]")

    def test_reservation_with_multiple_performances_blocks_migration(self):
        self.Ticket.objects.create(
            reservation=self.reservation, performance=self.other_performance, row=1, seat=1
        )
        self.assert_migration_rejected(
            f"reservations with multiple performances: [{self.reservation.pk}]"
        )
