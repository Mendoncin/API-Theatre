from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from theatre.models import Actor, Genre, Performance, Play, Reservation, TheatreHall, Ticket


@override_settings(ALLOWED_HOSTS=["testserver"], TIME_ZONE="UTC")
class PlayNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.drama = Genre.objects.create(name="Drama")
        cls.comedy = Genre.objects.create(name="Comedy")
        cls.hamlet = Play.objects.create(title="Hamlet", description="Description")
        cls.tragedy = Play.objects.create(title="The Tragedy of Hamlet", description="Description")
        cls.romeo = Play.objects.create(title="Romeo and Juliet", description="hamlet")
        cls.hamlet.genres.add(cls.drama, cls.comedy)
        cls.tragedy.genres.add(cls.comedy)
        cls.romeo.genres.add(cls.drama)
        actor = Actor.objects.create(first_name="Hamlet", last_name="Actor")
        cls.romeo.actors.add(actor)

    def setUp(self):
        self.client = APIClient()
        self.url = "/api/theatre/plays/"

    def ids(self, params=None):
        response = self.client.get(self.url, params or {})
        self.assertEqual(response.status_code, 200)
        return [item["id"] for item in response.data["results"]]

    def test_partial_search_is_case_insensitive_and_title_only(self):
        for search in ("hamlet", "HAMLET"):
            with self.subTest(search=search):
                self.assertEqual(self.ids({"search": search}), [self.hamlet.pk, self.tragedy.pk])
        self.assertEqual(self.ids({"search": "Drama"}), [])
        self.assertEqual(self.ids({"search": str(self.hamlet.pk)}), [])

    def test_search_without_matches_returns_empty_page(self):
        response = self.client.get(self.url, {"search": "nonexistent"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"count": 0, "next": None, "previous": None, "results": []})

    def test_genre_is_exact_and_multiple_genres_do_not_duplicate_play(self):
        ids = self.ids({"genre": self.drama.pk})
        self.assertEqual(ids, [self.hamlet.pk, self.romeo.pk])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.ids({"genre": self.comedy.pk}), [self.hamlet.pk, self.tragedy.pk])
        self.assertEqual(self.ids({"genre": 999999}), [])
        self.assertEqual(len(self.ids({"genres": self.drama.pk})), 3)

    def test_search_and_genre_are_combined_with_and(self):
        self.assertEqual(self.ids({"search": "hamlet", "genre": self.drama.pk}), [self.hamlet.pk])

    def test_ordering_is_restricted_to_title_and_defaults_to_ascending(self):
        ascending = [self.hamlet.pk, self.romeo.pk, self.tragedy.pk]
        self.assertEqual(self.ids(), ascending)
        self.assertEqual(self.ids({"ordering": "title"}), ascending)
        self.assertEqual(self.ids({"ordering": "-title"}), ascending[::-1])
        for field in ("-id", "description", "genres__name", "actors"):
            with self.subTest(field=field):
                self.assertEqual(self.ids({"ordering": field}), ascending)

    def test_detail_ignores_collection_parameters(self):
        url = f"{self.hamlet.pk}/"
        baseline = self.client.get(self.url + url)
        response = self.client.get(self.url + url, {
            "search": "no match", "genre": "invalid", "ordering": "-title", "page": 999,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, baseline.data)

    def test_filtered_search_keeps_query_count_constant(self):
        for size in (1, 4):
            if size == 4:
                for index in range(3):
                    play = Play.objects.create(title=f"Hamlet {index}", description="Description")
                    play.genres.add(self.drama, self.comedy)
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.url, {"search": "hamlet", "genre": self.drama.pk})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["count"], size)
            self.assertEqual(len(response.data["results"]), size)
            self.assertEqual(len(queries), 3, [q["sql"] for q in queries])
            self.assertEqual(sum("COUNT(" in q["sql"].upper() for q in queries), 1)


@override_settings(ALLOWED_HOSTS=["testserver"], TIME_ZONE="UTC")
class PerformanceNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = datetime(2026, 12, 20, 12, tzinfo=datetime_timezone.utc)
        cls.play = Play.objects.create(title="Hamlet", description="Description")
        cls.other_play = Play.objects.create(title="Romeo", description="Description")
        cls.hall = TheatreHall.objects.create(name="Hall", rows=3, seats_in_row=5)
        cls.future_b = cls.make_performance(cls.other_play, cls.now + timedelta(hours=4))
        cls.past = cls.make_performance(cls.play, cls.now - timedelta(days=1))
        cls.future_a = cls.make_performance(cls.play, cls.now + timedelta(hours=2))
        cls.exact_start = cls.make_performance(cls.play, cls.now)
        cls.tomorrow = cls.make_performance(cls.play, cls.now + timedelta(days=1))

    @classmethod
    def make_performance(cls, play, show_time):
        return Performance.objects.create(play=play, theatre_hall=cls.hall, show_time=show_time)

    def setUp(self):
        self.client = APIClient()
        self.url = "/api/theatre/performances/"
        self.clock = patch("theatre.filters.timezone.now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def ids(self, params=None):
        response = self.client.get(self.url, params or {})
        self.assertEqual(response.status_code, 200)
        return [item["id"] for item in response.data["results"]]

    def test_filter_by_play_id(self):
        self.assertEqual(self.ids({"play": self.play.pk}), [
            self.past.pk, self.exact_start.pk, self.future_a.pk, self.tomorrow.pk,
        ])
        self.assertEqual(self.ids({"play": self.other_play.pk}), [self.future_b.pk])

    def test_filter_by_iso_date(self):
        self.assertEqual(self.ids({"date": "2026-12-20"}), [
            self.exact_start.pk, self.future_a.pk, self.future_b.pk,
        ])
        self.assertEqual(self.ids({"date": "2026-12-19"}), [self.past.pk])

    def test_date_rejects_visual_formats_and_invalid_dates(self):
        for value in ("20/12/2026", "12/20/2026", "Dec 20, 2026", "2026-02-30"):
            with self.subTest(value=value):
                response = self.client.get(self.url, {"date": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("date", response.data)

    def test_date_uses_django_active_timezone(self):
        boundary = self.make_performance(
            self.play, datetime(2026, 12, 20, 1, tzinfo=datetime_timezone.utc),
        )
        with timezone.override(ZoneInfo("America/Bahia")):
            self.assertEqual(self.ids({"date": "2026-12-19"}), [self.past.pk, boundary.pk])

    def test_upcoming_is_strictly_future_and_false_does_not_filter(self):
        all_ids = [self.past.pk, self.exact_start.pk, self.future_a.pk, self.future_b.pk, self.tomorrow.pk]
        self.assertEqual(self.ids(), all_ids)
        self.assertEqual(self.ids({"upcoming": "false"}), all_ids)
        self.assertEqual(self.ids({"upcoming": "true"}), [
            self.future_a.pk, self.future_b.pk, self.tomorrow.pk,
        ])

    def test_filters_are_cumulative(self):
        self.assertEqual(self.ids({"play": self.play.pk, "date": "2026-12-20"}), [
            self.exact_start.pk, self.future_a.pk,
        ])
        self.assertEqual(self.ids({"play": self.play.pk, "upcoming": "true"}), [
            self.future_a.pk, self.tomorrow.pk,
        ])
        self.assertEqual(self.ids({
            "play": self.play.pk, "date": "2026-12-20", "upcoming": "true",
        }), [self.future_a.pk])

    def test_ordering_is_restricted_to_show_time_and_defaults_to_ascending(self):
        ascending = [self.past.pk, self.exact_start.pk, self.future_a.pk, self.future_b.pk, self.tomorrow.pk]
        self.assertEqual(self.ids(), ascending)
        self.assertEqual(self.ids({"ordering": "show_time"}), ascending)
        self.assertEqual(self.ids({"ordering": "-show_time"}), ascending[::-1])
        for field in ("-id", "play", "theatre_hall", "play__title"):
            with self.subTest(field=field):
                self.assertEqual(self.ids({"ordering": field}), ascending)

    def test_search_and_unrequested_filters_are_ignored(self):
        self.assertEqual(self.ids({
            "search": "nonexistent", "theatre_hall": 999999,
            "from": "2099-01-01", "to": "1900-01-01",
        }), self.ids())

    def test_detail_and_availability_ignore_filters_ordering_and_pagination(self):
        for suffix in ("", "availability/"):
            with self.subTest(suffix=suffix):
                url = f"{self.url}{self.past.pk}/{suffix}"
                baseline = self.client.get(url)
                response = self.client.get(url, {
                    "play": self.other_play.pk, "date": "invalid", "upcoming": "true",
                    "search": "nonexistent", "ordering": "-show_time", "page": 999,
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, baseline.data)
                self.assertNotIn("results", response.data)
                if suffix:
                    self.assertEqual(set(response.data), {
                        "performance", "theatre_hall", "capacity", "occupied_count",
                        "available_count", "occupied_seats",
                    })

    def test_filtered_lists_keep_query_count_constant(self):
        for size in (1, 4):
            if size == 4:
                for hours in range(5, 8):
                    self.make_performance(self.other_play, self.now + timedelta(hours=hours))
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.url, {
                    "play": self.other_play.pk, "date": "2026-12-20", "upcoming": "true",
                })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["count"], size)
            self.assertEqual(len(response.data["results"]), size)
            self.assertEqual(len(queries), 2, [q["sql"] for q in queries])
            self.assertEqual(sum("COUNT(" in q["sql"].upper() for q in queries), 1)


@override_settings(ALLOWED_HOSTS=["testserver"], TIME_ZONE="UTC")
class CollectionPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="pagination-owner")
        cls.other = get_user_model().objects.create_user(username="pagination-other")
        cls.genre = Genre.objects.create(name="Selected")
        cls.play_ids = []
        for index in range(12):
            Actor.objects.create(first_name=f"Actor {index:02}", last_name="Test")
            if index:
                Genre.objects.create(name=f"Genre {index:02}")
            hall = TheatreHall.objects.create(name=f"Hall {index:02}", rows=3, seats_in_row=5)
            play = Play.objects.create(title=f"Play {index:02}", description="Description")
            play.genres.add(cls.genre)
            cls.play_ids.append(play.pk)
            performance = Performance.objects.create(
                play=play, theatre_hall=hall,
                show_time=datetime(2026, 12, 20, 12, tzinfo=datetime_timezone.utc) + timedelta(days=index),
            )
            reservation = Reservation.objects.create(user=cls.owner, performance=performance)
            Ticket.objects.create(reservation=reservation, performance=performance, row=1, seat=1)
        foreign = Reservation.objects.create(user=cls.other, performance=performance)
        Ticket.objects.create(reservation=foreign, performance=performance, row=1, seat=2)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def assert_pages(self, url, params=None, expected_ids=None):
        params = params or {}
        first = self.client.get(url, params)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(set(first.data), {"count", "next", "previous", "results"})
        self.assertEqual(first.data["count"], 12)
        self.assertEqual(len(first.data["results"]), 10)
        self.assertIsNotNone(first.data["next"])
        self.assertIsNone(first.data["previous"])
        second = self.client.get(first.data["next"])
        explicit_second = self.client.get(url, {**params, "page": 2})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data, explicit_second.data)
        self.assertEqual(second.data["count"], 12)
        self.assertEqual(len(second.data["results"]), 2)
        self.assertIsNone(second.data["next"])
        self.assertIsNotNone(second.data["previous"])
        first_ids = [item["id"] for item in first.data["results"]]
        second_ids = [item["id"] for item in second.data["results"]]
        self.assertEqual(len(set(first_ids + second_ids)), 12)
        if expected_ids is not None:
            self.assertEqual(first_ids + second_ids, expected_ids)

    def test_all_seven_collections_use_global_pagination_with_disjoint_pages(self):
        for resource in ("actors", "genres", "halls", "plays", "performances", "reservations", "tickets"):
            with self.subTest(resource=resource):
                self.assert_pages(f"/api/theatre/{resource}/")

    def test_client_cannot_change_page_size(self):
        for value in (1, 100, 0):
            with self.subTest(value=value):
                self.assert_pages("/api/theatre/plays/", {"page_size": value})

    def test_play_search_and_genre_filter_before_pagination(self):
        Play.objects.create(title="Play excluded", description="Description")
        other = Play.objects.create(title="Different title", description="Description")
        other.genres.add(self.genre)
        self.assert_pages("/api/theatre/plays/", {
            "search": "Play", "genre": self.genre.pk, "ordering": "title",
        }, self.play_ids)

    def test_performance_filter_paginates_only_matching_collection(self):
        selected = Play.objects.create(title="Selected", description="Description")
        excluded = Play.objects.create(title="Excluded", description="Description")
        # Reuse 12 performances and add 8: the unfiltered collection has 20.
        Performance.objects.update(play=selected)
        expected_ids = list(Performance.objects.order_by("show_time").values_list("pk", flat=True))
        hall = TheatreHall.objects.first()
        for index in range(8):
            Performance.objects.create(
                play=excluded, theatre_hall=hall,
                show_time=datetime(2027, 1, 1, index, tzinfo=datetime_timezone.utc),
            )
        self.assertEqual(self.client.get("/api/theatre/performances/").data["count"], 20)
        self.assert_pages("/api/theatre/performances/", {"play": selected.pk}, expected_ids)

    def test_other_collections_ignore_unrequested_filters_and_ordering(self):
        params = {
            "search": "nonexistent", "ordering": "-id", "performance": 999999,
            "upcoming": "true", "past": "true", "play": 999999,
            "genre": 999999, "date": "invalid",
        }
        for resource in ("actors", "genres", "halls", "reservations", "tickets"):
            with self.subTest(resource=resource):
                url = f"/api/theatre/{resource}/"
                baseline = self.client.get(url)
                response = self.client.get(url, params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["count"], baseline.data["count"])
                self.assertEqual(response.data["results"], baseline.data["results"])
