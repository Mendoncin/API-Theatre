from datetime import timedelta
from queue import Queue
from threading import Event, Thread
from time import monotonic
from traceback import format_exc
from unittest import skipUnless
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from theatre.exceptions import CancellationNotAllowed, SeatUnavailable
from theatre.models import Actor, Genre, Performance, Play, Reservation, TheatreHall, Ticket


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
