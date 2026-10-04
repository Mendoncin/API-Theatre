from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
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
class OwnershipEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="owner")
        cls.other = get_user_model().objects.create_user(username="other")
        cls.reservation = Reservation.objects.create(user=cls.owner)
        cls.foreign_reservation = Reservation.objects.create(user=cls.other)
        hall = TheatreHall.objects.create(name="Hall", rows=3, seats_in_row=5)
        play = Play.objects.create(title="Play", description="Description")
        cls.performance = Performance.objects.create(
            play=play, theatre_hall=hall, show_time=timezone.now()
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
            "row": 2, "seat": 3, "performance": self.performance.pk,
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
            "/api/theatre/reservations/", {"user": self.other.pk}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Reservation.objects.get(pk=response.data["id"]).user_id, self.owner.pk)

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
        self.assertEqual(Ticket.objects.get(pk=response.data["id"]).reservation_id, self.reservation.pk)

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
                row=1, seat=1, performance=self.performance, reservation=self.reservation
            )

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
