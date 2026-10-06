from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from theatre.models import Actor, Genre, Performance, Play, Reservation, TheatreHall, Ticket


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
