from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from theatre.exceptions import CancellationNotAllowed, SeatUnavailable
from theatre.models import Actor, Genre, Performance, Play, Reservation, TheatreHall, Ticket
from theatre.views import ReservationViewSet


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
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(set(response.data), {"detail"})
                    self.assertTrue(response["WWW-Authenticate"].startswith("Bearer"))

    def test_invalid_jwt_returns_401_without_echoing_credentials(self):
        self.client.force_authenticate(None)
        credential = "invalid-private-credential"
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {credential}")
        for resource in ("reservations", "tickets"):
            with self.subTest(resource=resource):
                response = self.client.get(f"/api/theatre/{resource}/")
                self.assertEqual(response.status_code, 401)
                self.assertIn("detail", response.data)
                self.assertNotIn(credential, response.content.decode())

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
                for method in ("get", "delete"):
                    foreign_response = getattr(self.client, method)(f"{base}{foreign.pk}/")
                    missing_response = getattr(self.client, method)(f"{base}{max(own.pk, foreign.pk) + 1}/")
                    self.assertEqual(foreign_response.status_code, 404)
                    self.assertEqual(missing_response.status_code, 404)
                    self.assertEqual(foreign_response.data, missing_response.data)
                    self.assertEqual(set(foreign_response.data), {"detail"})
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
                    self.assertEqual(set(response.data), {"detail"})
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
            self.assertEqual(response.content, b"")
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
        self.assertEqual(set(response.data), {"detail"})
        self.assertNotIn("POST", response["Allow"])
        self.assertEqual(self.counts(), before)
