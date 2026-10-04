from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APIClient

from theatre.models import Actor, Genre, Performance, Play, Reservation, TheatreHall, Ticket
from theatre.serializers import TicketSerializer


@override_settings(ALLOWED_HOSTS=["testserver"])
class CatalogPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.regular = get_user_model().objects.create_user(username="catalog-reader")
        cls.staff = get_user_model().objects.create_user(
            username="catalog-staff", is_staff=True, is_superuser=False
        )
        cls.actor = Actor.objects.create(first_name="Test", last_name="Actor")
        genre = Genre.objects.create(name="Drama")
        hall = TheatreHall.objects.create(name="Hall", rows=3, seats_in_row=5)
        play = Play.objects.create(title="Play", description="Description")
        performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now()
        )
        cls.resources = (
            ("actors", cls.actor, {"first_name": "New", "last_name": "Actor"}),
            ("genres", genre, {"name": "Comedy"}),
            ("halls", hall, {"name": "New hall", "rows": 2, "seats_in_row": 4}),
            ("plays", play, {"title": "New play", "description": "Description",
                             "actors": [cls.actor.pk], "genres": [genre.pk]}),
            ("performances", performance, {"play": play.pk, "theatre_hall": hall.pk,
                                           "show_time": timezone.now().isoformat()}),
        )

    def setUp(self):
        self.client = APIClient()

    def test_safe_methods_are_public_on_all_catalog_resources(self):
        for resource, obj, _ in self.resources:
            for url in (f"/api/theatre/{resource}/", f"/api/theatre/{resource}/{obj.pk}/"):
                for method in ("get", "head", "options"):
                    with self.subTest(url=url, method=method):
                        self.assertEqual(getattr(self.client, method)(url).status_code, 200)

    def test_all_catalog_resources_reject_nonstaff_creation(self):
        for user, expected in ((None, 401), (self.regular, 403)):
            self.client.force_authenticate(user)
            for resource, _, payload in self.resources:
                with self.subTest(user=user, resource=resource):
                    response = self.client.post(f"/api/theatre/{resource}/", payload, format="json")
                    self.assertEqual(response.status_code, expected)

    def test_actor_nonstaff_read_and_write_permissions(self):
        base = "/api/theatre/actors/"
        detail = f"{base}{self.actor.pk}/"
        payload = {"first_name": "Changed", "last_name": "Actor"}
        for user, expected in ((None, 401), (self.regular, 403)):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(detail).status_code, 200)
            for method in ("put", "patch", "delete"):
                with self.subTest(user=user, method=method):
                    self.assertEqual(
                        getattr(self.client, method)(detail, payload, format="json").status_code,
                        expected,
                    )
        self.actor.refresh_from_db()
        self.assertEqual(self.actor.first_name, "Test")

    def test_staff_without_superuser_can_manage_catalog(self):
        self.client.force_authenticate(self.staff)
        for resource, _, payload in self.resources:
            base = f"/api/theatre/{resource}/"
            with self.subTest(resource=resource):
                self.assertEqual(self.client.get(base).status_code, 200)
                response = self.client.post(base, payload, format="json")
                self.assertEqual(response.status_code, 201)
                detail = f"{base}{response.data['id']}/"
                self.assertEqual(self.client.put(detail, payload, format="json").status_code, 200)
                self.assertEqual(self.client.patch(detail, payload, format="json").status_code, 200)
                self.assertEqual(self.client.delete(detail).status_code, 204)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PerformanceDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="performance-staff", is_staff=True, is_superuser=False
        )
        cls.regular = get_user_model().objects.create_user(username="performance-reader")
        hall = TheatreHall.objects.create(name="Hall", rows=3, seats_in_row=5)
        play = Play.objects.create(title="Play", description="Description")
        cls.performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now()
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.url = f"/api/theatre/performances/{self.performance.pk}/"

    def test_staff_can_delete_performance_without_reservations(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Performance.objects.filter(pk=self.performance.pk).exists())

    def test_reserved_performance_returns_conflict_and_preserves_data(self):
        reservation_ids = []
        for count in (1, 2):
            reservation_ids.append(Reservation.objects.create(
                user=self.regular, performance=self.performance
            ).pk)
            with self.subTest(reservations=count):
                response = self.client.delete(self.url)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data, {
                    "detail": (
                        "This performance cannot be deleted "
                        "because it has existing reservations."
                    )
                })
                self.assertTrue(Performance.objects.filter(pk=self.performance.pk).exists())
                self.assertEqual(
                    Reservation.objects.filter(
                        pk__in=reservation_ids, performance=self.performance
                    ).count(), count,
                )

    def test_nonstaff_cannot_delete_even_when_performance_is_protected(self):
        reservation = Reservation.objects.create(
            user=self.regular, performance=self.performance
        )
        for user, expected in ((None, 401), (self.regular, 403)):
            with self.subTest(user=user):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(self.url).status_code, 200)
                self.assertEqual(self.client.delete(self.url).status_code, expected)
                self.assertTrue(Performance.objects.filter(pk=self.performance.pk).exists())
                self.assertTrue(Reservation.objects.filter(pk=reservation.pk).exists())


@override_settings(ALLOWED_HOSTS=["testserver"])
class OwnershipEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="owner")
        cls.other = get_user_model().objects.create_user(username="other")
        hall = TheatreHall.objects.create(name="Hall", rows=3, seats_in_row=5)
        play = Play.objects.create(title="Play", description="Description")
        cls.performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now()
        )
        cls.reservation = Reservation.objects.create(
            user=cls.owner, performance=cls.performance
        )
        cls.foreign_reservation = Reservation.objects.create(
            user=cls.other, performance=cls.performance
        )
        cls.ticket = Ticket.objects.create(
            row=1, seat=1, performance=cls.performance, reservation=cls.reservation
        )
        cls.foreign_ticket = Ticket.objects.create(
            row=1, seat=2, performance=cls.performance,
            reservation=cls.foreign_reservation,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def payload(self, **changes):
        data = {
            "row": 2, "seat": 3,
            "reservation": self.reservation.pk,
        }
        data.update(changes)
        return data

    def test_authentication_required(self):
        self.client.force_authenticate(None)
        for resource, obj in (("reservations", self.reservation), ("tickets", self.ticket)):
            base = f"/api/theatre/{resource}/"
            for method, url in (
                ("get", base), ("post", base), ("get", f"{base}{obj.pk}/"),
                ("delete", f"{base}{obj.pk}/"),
            ):
                with self.subTest(resource=resource, method=method, url=url):
                    response = getattr(self.client, method)(url)
                    self.assertIn(response.status_code, (401, 403))

    def test_lists_details_and_deletes_are_scoped_to_owner(self):
        for resource, own, foreign in (
            ("reservations", self.reservation, self.foreign_reservation),
            ("tickets", self.ticket, self.foreign_ticket),
        ):
            base = f"/api/theatre/{resource}/"
            with self.subTest(resource=resource):
                response = self.client.get(base)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item["id"] for item in response.data], [own.pk])
                self.assertEqual(self.client.get(f"{base}{own.pk}/").status_code, 200)
                self.assertEqual(self.client.get(f"{base}{foreign.pk}/").status_code, 404)
                self.assertEqual(self.client.delete(f"{base}{foreign.pk}/").status_code, 404)
                self.assertTrue(type(foreign).objects.filter(pk=foreign.pk).exists())

    def test_reservation_creation_ignores_supplied_user(self):
        response = self.client.post(
            "/api/theatre/reservations/",
            {"user": self.other.pk, "performance": self.performance.pk}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        reservation = Reservation.objects.get(pk=response.data["id"])
        self.assertEqual(reservation.user_id, self.owner.pk)
        self.assertEqual(reservation.performance_id, self.performance.pk)

    def test_reservation_requires_performance_and_assigns_owner(self):
        response = self.client.post("/api/theatre/reservations/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("performance", response.data)
        response = self.client.post(
            "/api/theatre/reservations/", {"performance": self.performance.pk}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        reservation = Reservation.objects.get(pk=response.data["id"])
        self.assertEqual(reservation.user_id, self.owner.pk)
        self.assertEqual(reservation.performance_id, self.performance.pk)

    def test_ticket_creation_rejects_foreign_reservation(self):
        response = self.client.post(
            "/api/theatre/tickets/",
            self.payload(reservation=self.foreign_reservation.pk), format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("reservation", response.data)
        self.assertEqual(Ticket.objects.count(), 2)

    def test_ticket_creation_accepts_own_reservation(self):
        response = self.client.post("/api/theatre/tickets/", self.payload(), format="json")
        self.assertEqual(response.status_code, 201)
        ticket = Ticket.objects.get(pk=response.data["id"])
        self.assertEqual(ticket.reservation_id, self.reservation.pk)
        self.assertEqual(ticket.performance_id, self.reservation.performance_id)
        self.assertEqual(response.data["performance"], self.performance.pk)

    def test_update_methods_are_unavailable_and_leave_objects_unchanged(self):
        for resource, obj, data in (
            ("reservations", self.reservation, {"user": self.other.pk}),
            ("tickets", self.ticket, self.payload(reservation=self.foreign_reservation.pk)),
        ):
            before = type(obj).objects.values().get(pk=obj.pk)
            for method in ("put", "patch"):
                with self.subTest(resource=resource, method=method):
                    response = getattr(self.client, method)(
                        f"/api/theatre/{resource}/{obj.pk}/", data, format="json"
                    )
                    self.assertEqual(response.status_code, 405)
                    self.assertNotIn("PUT", response["Allow"])
                    self.assertNotIn("PATCH", response["Allow"])
                    self.assertEqual(type(obj).objects.values().get(pk=obj.pk), before)

    def test_owner_can_delete_ticket_and_reservation(self):
        for resource, obj in (("tickets", self.ticket), ("reservations", self.reservation)):
            response = self.client.delete(f"/api/theatre/{resource}/{obj.pk}/")
            self.assertEqual(response.status_code, 204)
            self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())

    def test_physical_seat_limits(self):
        for field, invalid in (("row", 0), ("row", 4), ("seat", 0), ("seat", 6)):
            with self.subTest(field=field, invalid=invalid):
                response = self.client.post(
                    "/api/theatre/tickets/", self.payload(**{field: invalid}), format="json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)

    def test_duplicate_seat_has_friendly_error(self):
        response = self.client.post(
            "/api/theatre/tickets/", self.payload(row=1, seat=2), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("This seat is already reserved", str(response.data))

    def test_database_constraint_rejects_duplicate_seat(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.create(
                row=1, seat=1, performance=self.performance, reservation=self.foreign_reservation
            )

    def test_multiple_seats_in_same_reservation(self):
        for seat in (1, 2, 3):
            with self.subTest(seat=seat):
                response = self.client.post(
                    "/api/theatre/tickets/", self.payload(row=3, seat=seat), format="json"
                )
                self.assertEqual(response.status_code, 201)
                ticket = Ticket.objects.get(pk=response.data["id"])
                self.assertEqual(ticket.performance_id, self.performance.pk)
                self.assertEqual(ticket.reservation_id, self.reservation.pk)

    def test_same_seat_in_another_performance_is_allowed(self):
        performance = Performance.objects.create(
            play=self.performance.play, theatre_hall=self.performance.theatre_hall,
            show_time=timezone.now(),
        )
        reservation = Reservation.objects.create(user=self.owner, performance=performance)
        response = self.client.post(
            "/api/theatre/tickets/",
            self.payload(reservation=reservation.pk, row=1, seat=1), format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Ticket.objects.get(pk=response.data["id"]).performance_id, performance.pk)

    def test_client_performance_cannot_override_reservation_or_its_hall(self):
        large_hall = TheatreHall.objects.create(name="Large", rows=20, seats_in_row=20)
        other_performance = Performance.objects.create(
            play=self.performance.play, theatre_hall=large_hall, show_time=timezone.now()
        )
        response = self.client.post(
            "/api/theatre/tickets/", self.payload(performance=other_performance.pk), format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Ticket.objects.get(pk=response.data["id"]).performance_id, self.performance.pk)
        for changes, field in (({"row": 4}, "row"), ({"seat": 6}, "seat")):
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/theatre/tickets/",
                    self.payload(performance=other_performance.pk, **changes), format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
        response = self.client.post(
            "/api/theatre/tickets/",
            self.payload(performance=other_performance.pk, row=1, seat=2), format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("This seat is already reserved", str(response.data))

    def test_performance_with_reservation_is_protected_even_without_tickets(self):
        performance = Performance.objects.create(
            play=self.performance.play, theatre_hall=self.performance.theatre_hall,
            show_time=timezone.now(),
        )
        reservation = Reservation.objects.create(user=self.owner, performance=performance)
        self.assertEqual(list(performance.reservations.all()), [reservation])
        with self.assertRaises(ProtectedError):
            performance.delete()
        self.assertTrue(Performance.objects.filter(pk=performance.pk).exists())

    def test_duplicate_insert_after_validation_has_friendly_error(self):
        serializer = TicketSerializer(
            data=self.payload(), context={"request": SimpleNamespace(user=self.owner)}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        Ticket.objects.create(
            row=2, seat=3, performance=self.performance, reservation=self.reservation
        )
        with self.assertRaisesMessage(serializers.ValidationError, "This seat is already reserved"):
            serializer.save()

    def test_ownership_validation_uses_existing_reservation(self):
        serializer = TicketSerializer(
            self.foreign_ticket, data={"seat": 4}, partial=True,
            context={"request": SimpleNamespace(user=self.owner)},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("reservation", serializer.errors)

    def test_previous_public_endpoints_remain_accessible(self):
        self.client.force_authenticate(None)
        for resource in ("actors", "genres", "halls", "plays", "performances"):
            with self.subTest(resource=resource):
                self.assertEqual(self.client.get(f"/api/theatre/{resource}/").status_code, 200)


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
