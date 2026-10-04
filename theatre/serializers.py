from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from theatre.exceptions import SeatUnavailable
from theatre.models import Actor, Genre, TheatreHall, Play, Performance, Reservation, Ticket


class ActorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Actor
        fields = "__all__"


class GenreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Genre
        fields = "__all__"


class TheatreHallSerializer(serializers.ModelSerializer):
    class Meta:
        model = TheatreHall
        fields = "__all__"


class PlaySummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Play
        fields = ("id", "title")
        read_only_fields = fields


class PlayListSerializer(serializers.ModelSerializer):
    genres = GenreSerializer(many=True, read_only=True)

    class Meta:
        model = Play
        fields = ("id", "title", "genres")
        read_only_fields = fields


class PlayDetailSerializer(serializers.ModelSerializer):
    genres = GenreSerializer(many=True, read_only=True)
    actors = ActorSerializer(many=True, read_only=True)

    class Meta:
        model = Play
        fields = ("id", "title", "description", "genres", "actors")
        read_only_fields = fields


class PlayWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Play
        fields = ("id", "title", "description", "actors", "genres")


class TheatreHallSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = TheatreHall
        fields = ("id", "name")
        read_only_fields = fields


class PerformanceSerializer(serializers.ModelSerializer):
    play = PlaySummarySerializer(read_only=True)
    theatre_hall = TheatreHallSummarySerializer(read_only=True)

    class Meta:
        model = Performance
        fields = ("id", "play", "theatre_hall", "show_time")
        read_only_fields = fields


class PerformanceWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Performance
        fields = ("id", "play", "theatre_hall", "show_time")


class ReservationTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = ("id", "row", "seat")
        read_only_fields = fields


class ReservationSerializer(serializers.ModelSerializer):
    performance = PerformanceSerializer(read_only=True)
    tickets = ReservationTicketSerializer(many=True, read_only=True)

    class Meta:
        model = Reservation
        fields = ("id", "created_at", "user", "performance", "tickets")
        read_only_fields = fields


class SeatInputSerializer(serializers.Serializer):
    row = serializers.IntegerField(min_value=1)
    seat = serializers.IntegerField(min_value=1)


class ReservationCreateSerializer(serializers.ModelSerializer):
    seats = SeatInputSerializer(many=True, allow_empty=False, write_only=True)

    class Meta:
        model = Reservation
        fields = ("performance", "seats")

    def validate_performance(self, performance):
        if performance.show_time <= timezone.now():
            raise serializers.ValidationError(
                "Reservations cannot be made for past performances."
            )
        return performance

    def seats_are_occupied(self, performance, seats):
        positions = Q()
        for seat in seats:
            positions |= Q(row=seat["row"], seat=seat["seat"])
        return Ticket.objects.filter(positions, performance=performance).exists()

    def validate(self, attrs):
        seats = attrs["seats"]
        positions = {(seat["row"], seat["seat"]) for seat in seats}
        if len(positions) != len(seats):
            raise serializers.ValidationError(
                {"seats": "Duplicate seat positions are not allowed."}
            )

        performance = attrs["performance"]
        hall = performance.theatre_hall
        for seat in seats:
            if seat["row"] > hall.rows or seat["seat"] > hall.seats_in_row:
                raise serializers.ValidationError(
                    {"seats": "Seat positions must be within the performance's theatre hall."}
                )

        if self.seats_are_occupied(performance, seats):
            raise SeatUnavailable()
        return attrs

    def create(self, validated_data):
        seats = validated_data.pop("seats")
        performance = validated_data["performance"]
        try:
            with transaction.atomic():
                reservation = Reservation.objects.create(**validated_data)
                for seat in seats:
                    Ticket.objects.create(
                        reservation=reservation,
                        performance=reservation.performance,
                        row=seat["row"],
                        seat=seat["seat"],
                    )
                return reservation
        except IntegrityError:
            if self.seats_are_occupied(performance, seats):
                raise SeatUnavailable()
            raise


class TicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = "__all__"
        read_only_fields = ("id", "row", "seat", "performance", "reservation")
