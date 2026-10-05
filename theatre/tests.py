from datetime import timedelta
from queue import Queue
from threading import Event, Thread
from time import monotonic
from traceback import format_exc
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from theatre.exceptions import CancellationNotAllowed, SeatUnavailable
from theatre.models import Actor, Genre, Performance, Play, Reservation, TheatreHall, Ticket
from theatre.views import ReservationViewSet


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
class RepresentationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = get_user_model().objects.create_user(username="representation-owner")
        self.staff = get_user_model().objects.create_user(
            username="representation-staff", is_staff=True
        )
        self.actor = Actor.objects.create(first_name="Test", last_name="Actor")
        self.genre = Genre.objects.create(name="Drama")
        self.performance, self.reservation, self.ticket = self.make_bundle()

    def make_bundle(self):
        play = Play.objects.create(title="Play", description="Full description")
        play.actors.add(self.actor)
        play.genres.add(self.genre)
        hall = TheatreHall.objects.create(name="Hall", rows=3, seats_in_row=5)
        performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now() + timedelta(days=7)
        )
        reservation = Reservation.objects.create(user=self.owner, performance=performance)
        ticket = Ticket.objects.create(
            reservation=reservation, performance=performance, row=1, seat=1
        )
        return performance, reservation, ticket

    def assert_performance_representation(self, data):
        self.assertEqual(set(data), {"id", "show_time", "play", "theatre_hall"})
        self.assertEqual(data["id"], self.performance.pk)
        self.assertEqual(data["play"], {"id": self.performance.play_id, "title": "Play"})
        self.assertEqual(data["theatre_hall"], {
            "id": self.performance.theatre_hall_id, "name": "Hall",
        })
        self.assertEqual(
            timezone.datetime.fromisoformat(data["show_time"].replace("Z", "+00:00")),
            self.performance.show_time,
        )

    def test_play_list_and_detail_have_distinct_bounded_representations(self):
        summary = {
            "id": self.performance.play_id, "title": "Play",
            "genres": [{"id": self.genre.pk, "name": "Drama"}],
        }
        response = self.client.get("/api/theatre/plays/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [summary])
        response = self.client.get(f"/api/theatre/plays/{self.performance.play_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            **summary, "description": "Full description",
            "actors": [{"id": self.actor.pk, "first_name": "Test", "last_name": "Actor"}],
        })

    def test_performance_list_and_detail_share_summary_representation(self):
        response = self.client.get("/api/theatre/performances/")
        self.assertEqual(response.status_code, 200)
        self.assert_performance_representation(response.data["results"][0])
        detail = self.client.get(f"/api/theatre/performances/{self.performance.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data, response.data["results"][0])

    def test_reservation_create_list_and_detail_use_enriched_read_representation(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post("/api/theatre/reservations/", {
            "performance": self.performance.pk, "seats": [{"row": 2, "seat": 3}],
        }, format="json")
        self.assertEqual(response.status_code, 201)
        data = response.data
        self.assertEqual(set(data), {"id", "created_at", "user", "performance", "tickets"})
        self.assertEqual(data["user"], self.owner.pk)
        self.assert_performance_representation(data["performance"])
        ticket = Ticket.objects.get(reservation_id=data["id"])
        self.assertEqual(data["tickets"], [{"id": ticket.pk, "row": 2, "seat": 3}])
        detail = self.client.get(f"/api/theatre/reservations/{data['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data, data)
        listing = self.client.get("/api/theatre/reservations/")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(data, listing.data["results"])
        for item in listing.data["results"]:
            self.assert_performance_representation(item["performance"])
            self.assertTrue(item["tickets"])
            self.assertEqual(set(item["tickets"][0]), {"id", "row", "seat"})

    def test_catalog_writes_accept_ids_and_reject_nested_objects(self):
        self.client.force_authenticate(self.staff)
        payloads = {
            "plays": {
                "title": "New", "description": "Description",
                "actors": [self.actor.pk], "genres": [self.genre.pk],
            },
            "performances": {
                "play": self.performance.play_id,
                "theatre_hall": self.performance.theatre_hall_id,
                "show_time": self.performance.show_time.isoformat(),
            },
        }
        relations = {"plays": ("actors", "genres"), "performances": ("play", "theatre_hall")}
        for resource, payload in payloads.items():
            with self.subTest(resource=resource):
                base = f"/api/theatre/{resource}/"
                response = self.client.post(base, payload, format="json")
                self.assertEqual(response.status_code, 201)
                url = f"{base}{response.data['id']}/"
                for method in ("put", "patch"):
                    response = getattr(self.client, method)(url, payload, format="json")
                    self.assertEqual(response.status_code, 200)
                    for field in relations[resource]:
                        self.assertEqual(response.data[field], payload[field])
                for field in relations[resource]:
                    value = payload[field]
                    nested = [{"id": pk} for pk in value] if isinstance(value, list) else {"id": value}
                    for method, target in (("post", base), ("put", url), ("patch", url)):
                        response = getattr(self.client, method)(
                            target, {**payload, field: nested}, format="json"
                        )
                        self.assertEqual(response.status_code, 400)
                        self.assertIn(field, response.data)

    def test_list_queries_do_not_grow_with_related_objects(self):
        self.client.force_authenticate(self.owner)
        # Authentication is forced; fixture writes are outside measurement.
        # Play: base SELECT + genres prefetch. Performance: one joined SELECT.
        # Reservation: one joined SELECT + tickets prefetch.
        expected_queries = {"plays": 2, "performances": 1, "reservations": 2}
        for count in (1, 4):
            if count == 4:
                for _ in range(3):
                    self.make_bundle()
            for resource, expected in expected_queries.items():
                with self.subTest(resource=resource, count=count):
                    with CaptureQueriesContext(connection) as queries:
                        response = self.client.get(f"/api/theatre/{resource}/")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(len(response.data["results"]), count)
                    # Pagination adds exactly one COUNT; data queries stay constant.
                    count_queries = [q for q in queries if 'COUNT(' in q['sql'].upper()]
                    self.assertEqual(len(count_queries), 1)
                    self.assertEqual(response.data["count"], count)
                    self.assertEqual(len(queries) - len(count_queries), expected,
                                     [q["sql"] for q in queries])


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
                        "This resource cannot be deleted "
                        "because it has protected related data."
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
class CatalogParentDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="parent-deletion-staff", is_staff=True, is_superuser=False
        )
        cls.owner = get_user_model().objects.create_user(username="parent-deletion-owner")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.staff)

    def make_tree(self, resource, count):
        hall = TheatreHall.objects.create(name="Hall", rows=3, seats_in_row=5)
        play = Play.objects.create(title="Play", description="Description")
        performances = [
            Performance.objects.create(
                play=play, theatre_hall=hall, show_time=timezone.now() + timedelta(days=1)
            )
            for _ in range(count)
        ]
        parent = play if resource == "plays" else hall
        return parent, performances

    def test_unprotected_parent_deletion_cascades_to_performances(self):
        for resource in ("plays", "halls"):
            with self.subTest(resource=resource):
                parent, performances = self.make_tree(resource, 3)
                response = self.client.delete(f"/api/theatre/{resource}/{parent.pk}/")
                self.assertEqual(response.status_code, 204)
                self.assertFalse(type(parent).objects.filter(pk=parent.pk).exists())
                self.assertFalse(Performance.objects.filter(
                    pk__in=[performance.pk for performance in performances]
                ).exists())

    def test_protected_parent_preserves_entire_tree(self):
        for resource in ("plays", "halls"):
            for count in (1, 3):
                with self.subTest(resource=resource, performances=count):
                    parent, performances = self.make_tree(resource, count)
                    # Only the last Performance is protected. Earlier siblings
                    # must survive too, rather than being partially cascaded away.
                    reservation = Reservation.objects.create(
                        user=self.owner, performance=performances[-1]
                    )
                    Ticket.objects.create(
                        reservation=reservation, performance=performances[-1], row=1, seat=1
                    )
                    models = (Play, TheatreHall, Performance, Reservation, Ticket)
                    before = [list(model.objects.order_by("pk").values()) for model in models]
                    response = self.client.delete(f"/api/theatre/{resource}/{parent.pk}/")
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.data, {
                        "detail": (
                            "This resource cannot be deleted "
                            "because it has protected related data."
                        )
                    })
                    after = [list(model.objects.order_by("pk").values()) for model in models]
                    self.assertEqual(after, before)


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
                self.assertEqual([item["id"] for item in response.data["results"]], [own.pk])
                self.assertEqual(self.client.get(f"{base}{own.pk}/").status_code, 200)
                self.assertEqual(self.client.get(f"{base}{foreign.pk}/").status_code, 404)
                self.assertEqual(self.client.delete(f"{base}{foreign.pk}/").status_code, 404)
                self.assertTrue(type(foreign).objects.filter(pk=foreign.pk).exists())

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
        self.performance.show_time = timezone.now() + timedelta(days=1)
        self.performance.save(update_fields=["show_time"])
        Ticket.objects.create(
            reservation=self.reservation, performance=self.performance, row=2, seat=1
        )
        for resource, obj in (("tickets", self.ticket), ("reservations", self.reservation)):
            response = self.client.delete(f"/api/theatre/{resource}/{obj.pk}/")
            self.assertEqual(response.status_code, 204)
            self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())

    def test_database_constraint_rejects_duplicate_seat(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.create(
                row=1, seat=1, performance=self.performance, reservation=self.foreign_reservation
            )

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

    def test_previous_public_endpoints_remain_accessible(self):
        self.client.force_authenticate(None)
        for resource in ("actors", "genres", "halls", "plays", "performances"):
            with self.subTest(resource=resource):
                self.assertEqual(self.client.get(f"/api/theatre/{resource}/").status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ReservationCreationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="seat-owner")
        cls.other = get_user_model().objects.create_user(username="seat-other")
        hall = TheatreHall.objects.create(name="Hall", rows=3, seats_in_row=5)
        play = Play.objects.create(title="Play", description="Description")
        cls.performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now() + timedelta(days=1)
        )
        large_hall = TheatreHall.objects.create(name="Large hall", rows=10, seats_in_row=10)
        cls.other_performance = Performance.objects.create(
            play=play, theatre_hall=large_hall, show_time=timezone.now() + timedelta(days=1)
        )
        cls.existing_reservation = Reservation.objects.create(
            user=cls.other, performance=cls.performance
        )
        Ticket.objects.create(
            reservation=cls.existing_reservation, performance=cls.performance, row=1, seat=2
        )

    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.owner)}")
        self.url = "/api/theatre/reservations/"

    def payload(self, seats=None, **changes):
        data = {
            "performance": self.performance.pk,
            "seats": [{"row": 2, "seat": 1}] if seats is None else seats,
        }
        data.update(changes)
        return data

    def counts(self):
        return Reservation.objects.count(), Ticket.objects.count()

    def assert_rejected_without_writes(self, payload, expected):
        before = self.counts()
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, expected)
        self.assertEqual(self.counts(), before)
        if expected == 409:
            self.assertEqual(response.data, {"detail": SeatUnavailable.default_detail})
        return response

    def test_future_performance_creates_reservation_and_ticket_for_jwt_user(self):
        self.assertGreater(self.performance.show_time, timezone.now())
        before = self.counts()
        response = self.client.post(self.url, self.payload(), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.counts(), (before[0] + 1, before[1] + 1))
        reservation = Reservation.objects.get(pk=response.data["id"])
        self.assertEqual(reservation.user_id, self.owner.pk)
        self.assertEqual(reservation.performance_id, self.performance.pk)
        ticket = reservation.tickets.get()
        self.assertEqual(ticket.performance_id, reservation.performance_id)
        self.assertEqual((ticket.row, ticket.seat), (2, 1))
        self.assertEqual(set(response.data), {"id", "created_at", "user", "performance", "tickets"})
        self.assertEqual(response.data["tickets"], [{"id": ticket.pk, "row": 2, "seat": 1}])
        self.assertEqual(self.client.get(f"{self.url}{reservation.pk}/").data, response.data)
        self.assertEqual(self.client.get(self.url).data["results"], [response.data])

    def test_past_performance_rejects_reservation_without_writes(self):
        self.performance.show_time = timezone.now() - timedelta(days=1)
        self.performance.save(update_fields=["show_time"])
        response = self.assert_rejected_without_writes(self.payload(), 400)
        self.assertEqual(response.data, {
            "performance": ["Reservations cannot be made for past performances."]
        })

    def test_performance_at_exact_current_time_rejects_reservation_without_writes(self):
        current_time = timezone.now()
        self.performance.show_time = current_time
        self.performance.save(update_fields=["show_time"])
        with patch("theatre.serializers.timezone.now", return_value=current_time):
            response = self.assert_rejected_without_writes(self.payload(), 400)
        self.assertEqual(response.data, {
            "performance": ["Reservations cannot be made for past performances."]
        })

    def test_multiple_seats_use_server_controlled_owner_and_performance(self):
        seats = [{"row": 3, "seat": seat, "performance": self.other_performance.pk}
                 for seat in (1, 2, 3)]
        before = self.counts()
        response = self.client.post(
            self.url, self.payload(seats, user=self.other.pk,
                                  tickets=[{"performance": self.other_performance.pk}],
                                  created_at="2000-01-01T00:00:00Z"), format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.counts(), (before[0] + 1, before[1] + 3))
        reservation = Reservation.objects.get(pk=response.data["id"])
        self.assertEqual(reservation.user_id, self.owner.pk)
        self.assertNotEqual(reservation.created_at.year, 2000)
        self.assertEqual(set(reservation.tickets.values_list("performance_id", flat=True)),
                         {reservation.performance_id})
        self.assertEqual(reservation.performance_id, self.performance.pk)
        self.assertCountEqual(
            response.data["tickets"], list(reservation.tickets.values("id", "row", "seat"))
        )
        self.assertCountEqual([(t["row"], t["seat"]) for t in response.data["tickets"]],
                              [(3, 1), (3, 2), (3, 3)])

    def test_required_fields_and_invalid_seats_create_nothing(self):
        payloads = [
            {"performance": self.performance.pk},
            {"seats": [{"row": 2, "seat": 1}]},
            self.payload([]),
            self.payload([{"row": 2, "seat": 1}, {"row": 2, "seat": 1}]),
        ]
        for row, seat in ((0, 1), (1, 0), (-1, 1), (4, 1), (1, 6)):
            payloads.append(self.payload([
                {"row": 2, "seat": 1},
                {"row": row, "seat": seat, "performance": self.other_performance.pk},
            ]))
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_rejected_without_writes(payload, 400)

    def test_partly_or_fully_occupied_requests_create_nothing(self):
        for seats in (
            [{"row": 2, "seat": 1}, {"row": 1, "seat": 2}, {"row": 2, "seat": 3}],
            [{"row": 1, "seat": 2}],
        ):
            with self.subTest(seats=seats):
                self.assert_rejected_without_writes(self.payload(seats), 409)

    def test_occupancy_checks_exact_pairs_not_cartesian_product(self):
        response = self.client.post(self.url, self.payload([
            {"row": 1, "seat": 3}, {"row": 2, "seat": 2}
        ]), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["tickets"]), 2)

    def test_same_position_in_another_performance_is_allowed(self):
        response = self.client.post(self.url, self.payload(
            [{"row": 1, "seat": 2}], performance=self.other_performance.pk
        ), format="json")
        self.assertEqual(response.status_code, 201)
        ticket = Ticket.objects.get(pk=response.data["tickets"][0]["id"])
        self.assertEqual(ticket.performance_id, self.other_performance.pk)
        self.assertEqual(ticket.reservation.performance_id, self.other_performance.pk)

    def test_race_conflict_rolls_back_reservation_and_partial_tickets(self):
        before = self.counts()
        perform_create = ReservationViewSet.perform_create
        insert_ticket = Ticket.objects.create
        partial = []
        competitor = []

        def occupy_after_validation(view, serializer):
            competitor.append(insert_ticket(
                reservation=self.existing_reservation, performance=self.performance,
                row=2, seat=2,
            ).pk)
            perform_create(view, serializer)

        def track_inserts(**kwargs):
            if partial:
                self.assertTrue(Ticket.objects.filter(pk=partial[0][0]).exists())
                self.assertTrue(Reservation.objects.filter(pk=partial[0][1]).exists())
            ticket = insert_ticket(**kwargs)
            partial.append((ticket.pk, ticket.reservation_id))
            return ticket

        with patch.object(ReservationViewSet, "perform_create", occupy_after_validation), \
                patch.object(Ticket.objects, "create", side_effect=track_inserts):
            response = self.client.post(self.url, self.payload([
                {"row": 2, "seat": 1}, {"row": 2, "seat": 2}
            ]), format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"detail": SeatUnavailable.default_detail})
        self.assertEqual(len(partial), 1)
        self.assertFalse(Ticket.objects.filter(pk=partial[0][0]).exists())
        self.assertFalse(Reservation.objects.filter(pk=partial[0][1]).exists())
        self.assertTrue(Ticket.objects.filter(pk=competitor[0]).exists())
        self.assertEqual(self.counts(), (before[0], before[1] + 1))

    def test_unrelated_integrity_error_is_reraised_after_full_rollback(self):
        before = self.counts()
        insert_ticket = Ticket.objects.create
        partial = []
        failure = IntegrityError("Unrelated database failure")

        def fail_second_insert(**kwargs):
            if partial:
                raise failure
            ticket = insert_ticket(**kwargs)
            partial.append((ticket.pk, ticket.reservation_id))
            return ticket

        with patch.object(Ticket.objects, "create", side_effect=fail_second_insert):
            with self.assertRaises(IntegrityError) as caught:
                self.client.post(self.url, self.payload([
                    {"row": 2, "seat": 1}, {"row": 2, "seat": 2}
                ]), format="json")
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(partial), 1)
        self.assertFalse(Ticket.objects.filter(pk=partial[0][0]).exists())
        self.assertFalse(Reservation.objects.filter(pk=partial[0][1]).exists())
        self.assertEqual(self.counts(), before)

    def test_individual_ticket_creation_is_unavailable(self):
        before = self.counts()
        response = self.client.post("/api/theatre/tickets/", {
            "reservation": self.existing_reservation.pk,
            "performance": self.performance.pk, "row": 2, "seat": 1,
        }, format="json")
        self.assertEqual(response.status_code, 405)
        self.assertNotIn("POST", response["Allow"])
        self.assertEqual(self.counts(), before)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PerformanceAvailabilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="availability-owner")
        cls.other = get_user_model().objects.create_user(username="availability-other")
        cls.hall = TheatreHall.objects.create(name="Main hall", rows=8, seats_in_row=12)
        cls.play = Play.objects.create(title="Play", description="Description")
        cls.performance = Performance.objects.create(
            play=cls.play, theatre_hall=cls.hall, show_time=timezone.now()
        )
        cls.other_performance = Performance.objects.create(
            play=cls.play, theatre_hall=cls.hall, show_time=timezone.now()
        )

    def setUp(self):
        self.client = APIClient()
        self.url = f"/api/theatre/performances/{self.performance.pk}/availability/"

    def add_tickets(self, performance, positions, user=None):
        reservation = Reservation.objects.create(user=user or self.owner, performance=performance)
        return [Ticket.objects.create(
            reservation=reservation, performance=performance, row=row, seat=seat
        ) for row, seat in positions]

    def test_anonymous_empty_performance_has_exact_response(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            "performance": self.performance.pk,
            "theatre_hall": {
                "id": self.hall.pk, "name": "Main hall", "rows": 8, "seats_in_row": 12,
            },
            "capacity": 96,
            "occupied_count": 0,
            "available_count": 96,
            "occupied_seats": [],
        })

    def test_past_performance_remains_public_in_catalog_and_availability(self):
        self.performance.show_time = timezone.now() - timedelta(days=1)
        self.performance.save(update_fields=["show_time"])
        self.add_tickets(self.performance, [(2, 5)])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            "performance": self.performance.pk,
            "theatre_hall": {
                "id": self.hall.pk, "name": "Main hall", "rows": 8, "seats_in_row": 12,
            },
            "capacity": 96,
            "occupied_count": 1,
            "available_count": 95,
            "occupied_seats": [{"row": 2, "seat": 5}],
        })
        detail = self.client.get(f"/api/theatre/performances/{self.performance.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["id"], self.performance.pk)
        listing = self.client.get("/api/theatre/performances/")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(self.performance.pk, [item["id"] for item in listing.data["results"]])

    def test_tickets_from_multiple_reservations_are_sorted_without_changing_data(self):
        self.add_tickets(self.performance, [(4, 8), (1, 7), (2, 2)])
        self.add_tickets(self.performance, [(1, 3), (2, 1)], user=self.other)
        models = (Performance, TheatreHall, Reservation, Ticket)
        before = [list(model.objects.order_by("pk").values()) for model in models]
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["capacity"], 96)
        self.assertEqual(response.data["occupied_count"], 5)
        self.assertEqual(response.data["available_count"], 91)
        self.assertEqual(response.data["occupied_seats"], [
            {"row": 1, "seat": 3}, {"row": 1, "seat": 7},
            {"row": 2, "seat": 1}, {"row": 2, "seat": 2}, {"row": 4, "seat": 8},
        ])
        self.assertEqual(
            [list(model.objects.order_by("pk").values()) for model in models], before
        )

    def test_occupancy_is_isolated_between_performances_in_same_hall(self):
        self.add_tickets(self.other_performance, [(2, 5)])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["occupied_count"], 0)
        self.assertEqual(response.data["available_count"], 96)
        self.assertEqual(response.data["occupied_seats"], [])
        self.add_tickets(self.performance, [(1, 1)])
        response = self.client.get(self.url)
        self.assertEqual(response.data["occupied_count"], 1)
        self.assertEqual(response.data["available_count"], 95)
        self.assertEqual(response.data["occupied_seats"], [{"row": 1, "seat": 1}])

    def test_full_hall_has_no_available_seats(self):
        hall = TheatreHall.objects.create(name="Small hall", rows=2, seats_in_row=2)
        performance = Performance.objects.create(
            play=self.play, theatre_hall=hall, show_time=timezone.now()
        )
        positions = [(1, 1), (1, 2), (2, 1), (2, 2)]
        self.add_tickets(performance, positions)
        response = self.client.get(f"/api/theatre/performances/{performance.pk}/availability/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["capacity"], 4)
        self.assertEqual(response.data["occupied_count"], 4)
        self.assertEqual(response.data["available_count"], 0)
        self.assertEqual(response.data["occupied_seats"], [
            {"row": row, "seat": seat} for row, seat in positions
        ])

    def test_nonexistent_performance_returns_404(self):
        missing_id = max(self.performance.pk, self.other_performance.pk) + 1
        response = self.client.get(f"/api/theatre/performances/{missing_id}/availability/")
        self.assertEqual(response.status_code, 404)

    def test_owner_ticket_deletion_is_reflected_in_next_public_query(self):
        self.performance.show_time = timezone.now() + timedelta(days=1)
        self.performance.save(update_fields=["show_time"])
        tickets = self.add_tickets(self.performance, [(2, 4), (2, 5)])
        before = self.client.get(self.url)
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.data["occupied_count"], 2)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.owner)}")
        response = self.client.delete(f"/api/theatre/tickets/{tickets[0].pk}/")
        self.assertEqual(response.status_code, 204)
        self.client.credentials()
        after = self.client.get(self.url)
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.data["occupied_count"], before.data["occupied_count"] - 1)
        self.assertEqual(after.data["available_count"], before.data["available_count"] + 1)
        self.assertEqual(after.data["occupied_seats"], [{"row": 2, "seat": 5}])

    def test_action_does_not_allow_writes_even_for_staff(self):
        staff = get_user_model().objects.create_user(username="availability-staff", is_staff=True)
        self.client.force_authenticate(staff)
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(self.url).status_code, 405)
        self.assertTrue(Performance.objects.filter(pk=self.performance.pk).exists())


@override_settings(ALLOWED_HOSTS=["testserver"])
class CancellationTests(TransactionTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="cancellation-owner")
        self.other = get_user_model().objects.create_user(username="cancellation-other")
        hall = TheatreHall.objects.create(name="Hall", rows=2, seats_in_row=3)
        play = Play.objects.create(title="Play", description="Description")
        self.performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now() + timedelta(days=1)
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.owner)}")
        self.availability_url = f"/api/theatre/performances/{self.performance.pk}/availability/"

    def make_reservation(self, count=3):
        reservation = Reservation.objects.create(user=self.owner, performance=self.performance)
        tickets = [Ticket.objects.create(
            reservation=reservation, performance=self.performance, row=1, seat=seat
        ) for seat in range(1, count + 1)]
        return reservation, tickets

    def test_partial_cancellation_preserves_other_seats_and_updates_availability(self):
        reservation, tickets = self.make_reservation()
        before = self.client.get(self.availability_url).data
        response = self.client.delete(f"/api/theatre/tickets/{tickets[1].pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(Reservation.objects.filter(pk=reservation.pk).exists())
        self.assertCountEqual(reservation.tickets.values_list("pk", flat=True),
                              [tickets[0].pk, tickets[2].pk])
        after = self.client.get(self.availability_url).data
        self.assertEqual(after["occupied_count"], before["occupied_count"] - 1)
        self.assertEqual(after["available_count"], before["available_count"] + 1)
        self.assertEqual(after["occupied_seats"], [{"row": 1, "seat": 1}, {"row": 1, "seat": 3}])

    def test_last_ticket_removes_reservation(self):
        reservation, tickets = self.make_reservation(1)
        response = self.client.delete(f"/api/theatre/tickets/{tickets[0].pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Ticket.objects.filter(pk=tickets[0].pk).exists())
        self.assertFalse(Reservation.objects.filter(pk=reservation.pk).exists())

    def test_integral_cancellation_cascades_and_releases_all_seats(self):
        reservation, tickets = self.make_reservation()
        before = self.client.get(self.availability_url).data
        response = self.client.delete(f"/api/theatre/reservations/{reservation.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Reservation.objects.filter(pk=reservation.pk).exists())
        self.assertFalse(Ticket.objects.filter(pk__in=[t.pk for t in tickets]).exists())
        after = self.client.get(self.availability_url).data
        self.assertEqual(after["occupied_count"], before["occupied_count"] - 3)
        self.assertEqual(after["available_count"], before["available_count"] + 3)
        self.assertEqual(after["occupied_seats"], [])

    def test_anonymous_and_other_user_cannot_cancel(self):
        reservation, tickets = self.make_reservation()
        for user, expected in ((None, 401), (self.other, 404)):
            self.client.credentials()
            if user:
                self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
            for resource, pk in (("tickets", tickets[0].pk), ("reservations", reservation.pk)):
                with self.subTest(user=user, resource=resource):
                    response = self.client.delete(f"/api/theatre/{resource}/{pk}/")
                    self.assertEqual(response.status_code, expected)
                    self.assertTrue(Reservation.objects.filter(pk=reservation.pk).exists())
                    self.assertEqual(reservation.tickets.count(), 3)

    def test_past_and_exact_start_block_both_deletes_but_allow_history(self):
        reservation, tickets = self.make_reservation()
        current_time = timezone.now()
        for show_time in (current_time - timedelta(days=1), current_time):
            self.performance.show_time = show_time
            self.performance.save(update_fields=["show_time"])
            with patch("theatre.views.timezone.now", return_value=current_time):
                for resource, pk in (("tickets", tickets[0].pk), ("reservations", reservation.pk)):
                    with self.subTest(show_time=show_time, resource=resource):
                        url = f"/api/theatre/{resource}/{pk}/"
                        response = self.client.delete(url)
                        self.assertEqual(response.status_code, 409)
                        self.assertEqual(response.data, {"detail": CancellationNotAllowed.default_detail})
                        self.assertTrue(Reservation.objects.filter(pk=reservation.pk).exists())
                        self.assertEqual(reservation.tickets.count(), 3)
                        self.assertEqual(self.client.get(url).status_code, 200)
                        listing = self.client.get(f"/api/theatre/{resource}/")
                        self.assertEqual(listing.status_code, 200)
                        self.assertIn(pk, [item["id"] for item in listing.data["results"]])

    def test_last_ticket_deletion_rolls_back_if_reservation_deletion_fails(self):
        reservation, tickets = self.make_reservation(1)

        def fail_delete(instance, *args, **kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertFalse(Ticket.objects.filter(reservation=instance).exists())
            raise RuntimeError("Deletion failed")

        with patch.object(Reservation, "delete", fail_delete):
            with self.assertRaisesMessage(RuntimeError, "Deletion failed"):
                self.client.delete(f"/api/theatre/tickets/{tickets[0].pk}/")
        self.assertTrue(Reservation.objects.filter(pk=reservation.pk).exists())
        self.assertTrue(Ticket.objects.filter(pk=tickets[0].pk).exists())

    def test_reservation_lock_covers_both_last_ticket_decisions_and_integral_delete(self):
        reservation, tickets = self.make_reservation(2)
        locked_queryset = Reservation.objects.select_for_update()
        get_locked = locked_queryset.get
        delete_ticket = Ticket.delete
        delete_reservation = Reservation.delete
        exists = QuerySet.exists
        events = []

        def request_lock():
            self.assertTrue(connection.in_atomic_block)
            self.assertTrue(locked_queryset.query.select_for_update)
            return locked_queryset

        def acquire_lock(*args, **kwargs):
            self.assertTrue(connection.in_atomic_block)
            result = get_locked(*args, **kwargs)
            self.assertEqual(result.pk, reservation.pk)
            events.append("lock")
            return result

        def track_ticket_delete(instance, *args, **kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(events, ["lock"])
            events.append("ticket")
            return delete_ticket(instance, *args, **kwargs)

        def track_exists(queryset):
            if queryset.model is Ticket:
                self.assertTrue(connection.in_atomic_block)
                self.assertEqual(events, ["lock", "ticket"])
                events.append("remaining")
            return exists(queryset)

        def track_reservation_delete(instance, *args, **kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(events[0], "lock")
            events.append("reservation")
            return delete_reservation(instance, *args, **kwargs)

        # TransactionTestCase leaves autocommit enabled outside each API call.
        self.assertFalse(connection.in_atomic_block)
        with (
            patch.object(Reservation.objects, "select_for_update", side_effect=request_lock),
            patch.object(locked_queryset, "get", side_effect=acquire_lock),
            patch.object(Ticket, "delete", track_ticket_delete),
            patch.object(Reservation, "delete", track_reservation_delete),
            patch.object(QuerySet, "exists", track_exists),
        ):
            for index, ticket in enumerate(tickets):
                events.clear()
                response = self.client.delete(f"/api/theatre/tickets/{ticket.pk}/")
                self.assertEqual(response.status_code, 204)
                expected = ["lock", "ticket", "remaining"]
                if index == 1:
                    expected.append("reservation")
                self.assertEqual(events, expected)
                self.assertFalse(connection.in_atomic_block)
                self.assertEqual(Reservation.objects.filter(pk=reservation.pk).count(), 1 - index)
                self.assertEqual(Ticket.objects.filter(reservation_id=reservation.pk).count(), 1 - index)
            reservation, _ = self.make_reservation(2)
            events.clear()
            response = self.client.delete(f"/api/theatre/reservations/{reservation.pk}/")
            self.assertEqual(response.status_code, 204)
            self.assertEqual(events, ["lock", "reservation"])
        self.assertFalse(connection.in_atomic_block)


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locking")
@override_settings(ALLOWED_HOSTS=["testserver"])
class CancellationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="concurrent-owner")
        hall = TheatreHall.objects.create(name="Hall", rows=2, seats_in_row=3)
        play = Play.objects.create(title="Play", description="Description")
        self.performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now() + timedelta(days=7)
        )

    def make_reservation(self, count):
        reservation = Reservation.objects.create(user=self.owner, performance=self.performance)
        tickets = [
            Ticket.objects.create(
                reservation=reservation, performance=self.performance, row=1, seat=seat
            )
            for seat in range(1, count + 1)
        ]
        return reservation, tickets

    def run_concurrent_deletes(self, first_url, second_url, expected_statuses):
        locked = Event()
        release = Event()
        attempting = Event()
        identities = Queue()
        results = Queue()
        errors = Queue()
        owner_id = self.owner.pk

        def worker(index, url):
            try:
                close_old_connections()
                self.assertEqual(connection.vendor, "postgresql")
                # These limits also bound database waits if the test itself fails.
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '15s'")
                    cursor.execute("SET lock_timeout = '10s'")
                client = APIClient()
                client.force_authenticate(get_user_model().objects.get(pk=owner_id))

                def synchronize(execute, sql, params, many, context):
                    if '"theatre_reservation"' not in sql or "FOR UPDATE" not in sql:
                        return execute(sql, params, many, context)
                    self.assertTrue(connection.in_atomic_block)
                    # Record the identity inside the production transaction, not
                    # in a separate test transaction. Each worker owns its connection.
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid(), txid_current()")
                        identities.put((index, cursor.fetchone()))
                    if index == 1:
                        attempting.set()
                    result = execute(sql, params, many, context)
                    if index == 0:
                        # Real SELECT FOR UPDATE has completed and holds the row.
                        # Pause only until PostgreSQL confirms the other worker waits.
                        locked.set()
                        if not release.wait(10):
                            raise TimeoutError("Timed out waiting to release Reservation lock")
                    return result

                # Instrumentation delegates every statement to PostgreSQL unchanged:
                # no mock of select_for_update, atomic, or ORM deletion is used.
                with connection.execute_wrapper(synchronize):
                    response = client.delete(url)
                results.put((index, response.status_code))
            except Exception:
                errors.put((index, format_exc()))
            finally:
                connection.close()
                close_old_connections()

        workers = [
            Thread(target=worker, args=(index, url), daemon=True)
            for index, url in enumerate((first_url, second_url))
        ]
        started = []
        try:
            workers[0].start()
            started.append(workers[0])
            self.assertTrue(locked.wait(10), "First DELETE did not acquire the row lock")
            workers[1].start()
            started.append(workers[1])
            self.assertTrue(attempting.wait(10), "Second DELETE did not attempt the row lock")
            identities_by_worker = dict(identities.get(timeout=1) for _ in range(2))
            first_pid, first_txid = identities_by_worker[0]
            second_pid, second_txid = identities_by_worker[1]
            self.assertNotEqual(first_pid, second_pid)
            self.assertNotEqual(first_txid, second_txid)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                self.assertNotIn(cursor.fetchone()[0], (first_pid, second_pid))

            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_blocking_pids(%s)", [second_pid])
                    blocked = first_pid in cursor.fetchone()[0]
                if blocked:
                    break
                release.wait(0.01)
            self.assertTrue(blocked, "PostgreSQL did not observe the second DELETE waiting")
        finally:
            release.set()
            for thread in started:
                thread.join(timeout=20)
            self.assertFalse(any(thread.is_alive() for thread in started), "Worker timed out")
            failures = [errors.get_nowait() for _ in range(errors.qsize())]
            self.assertEqual(failures, [], failures)

        self.assertEqual(dict(results.get(timeout=1) for _ in range(2)), expected_statuses)

    def assert_reservation_removed(self, reservation_id):
        self.assertFalse(Reservation.objects.filter(pk=reservation_id).exists())
        self.assertFalse(Ticket.objects.filter(reservation_id=reservation_id).exists())
        self.assertFalse(Reservation.objects.filter(tickets__isnull=True).exists())
        self.assertFalse(Ticket.objects.exclude(reservation_id__in=Reservation.objects.values("pk")).exists())

    def test_concurrent_ticket_cancellations_keep_reservation_consistent(self):
        reservation, tickets = self.make_reservation(2)
        # Different tickets: the first cancellation leaves one; the second removes
        # the last ticket and its Reservation after acquiring the same row lock.
        self.run_concurrent_deletes(
            f"/api/theatre/tickets/{tickets[0].pk}/",
            f"/api/theatre/tickets/{tickets[1].pk}/",
            {0: 204, 1: 204},
        )
        self.assert_reservation_removed(reservation.pk)

    def test_concurrent_reservation_and_ticket_cancellation_keep_state_consistent(self):
        for whole_first in (True, False):
            with self.subTest(whole_first=whole_first):
                reservation, tickets = self.make_reservation(3)
                whole_url = f"/api/theatre/reservations/{reservation.pk}/"
                partial_url = f"/api/theatre/tickets/{tickets[1].pk}/"
                # Whole first: the waiting partial DELETE finds its parent gone (404).
                # Partial first: other tickets remain, so whole DELETE also succeeds.
                self.run_concurrent_deletes(
                    whole_url if whole_first else partial_url,
                    partial_url if whole_first else whole_url,
                    {0: 204, 1: 404 if whole_first else 204},
                )
                self.assert_reservation_removed(reservation.pk)


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
