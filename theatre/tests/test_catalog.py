from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from theatre.models import Actor, Genre, Performance, Play, Reservation, TheatreHall, Ticket


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

    def test_catalog_nonstaff_read_and_write_permissions(self):
        for user, expected in ((None, 401), (self.regular, 403)):
            self.client.force_authenticate(user)
            for resource, obj, payload in self.resources:
                detail = f"/api/theatre/{resource}/{obj.pk}/"
                before = type(obj).objects.values().get(pk=obj.pk)
                self.assertEqual(self.client.get(detail).status_code, 200)
                for method in ("put", "patch", "delete"):
                    with self.subTest(user=user, resource=resource, method=method):
                        response = getattr(self.client, method)(detail, payload, format="json")
                        self.assertEqual(response.status_code, expected)
                        self.assertEqual(set(response.data), {"detail"})
                        self.assertEqual(type(obj).objects.values().get(pk=obj.pk), before)

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
                response = self.client.delete(detail)
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.content, b"")

    def test_invalid_catalog_payloads_return_native_field_errors(self):
        self.client.force_authenticate(self.staff)
        for resource, _, _ in self.resources:
            with self.subTest(resource=resource):
                response = self.client.post(f"/api/theatre/{resource}/", {}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.data)
                self.assertNotIn("detail", response.data)
                self.assertTrue(all(isinstance(errors, list) for errors in response.data.values()))

    def test_missing_catalog_details_return_native_404(self):
        for resource, obj, _ in self.resources:
            with self.subTest(resource=resource):
                response = self.client.get(f"/api/theatre/{resource}/{obj.pk + 1}/")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(set(response.data), {"detail"})


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
