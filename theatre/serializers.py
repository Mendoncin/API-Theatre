from django.db import IntegrityError, transaction
from rest_framework import serializers

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


class PlaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Play
        fields = "__all__"


class PerformanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Performance
        fields = "__all__"


class ReservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reservation
        fields = "__all__"
        read_only_fields = ("user", "created_at")


class TicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = "__all__"
        read_only_fields = ("performance",)
        validators = []

    def validate(self, attrs):
        request = self.context.get("request")
        reservation = attrs.get(
            "reservation", getattr(self.instance, "reservation", None)
        )
        if (
            request
            and request.user.is_authenticated
            and reservation
            and reservation.user_id != request.user.id
        ):
            raise serializers.ValidationError(
                {"reservation": "You cannot add tickets to another user's reservation."}
            )

        performance = reservation.performance
        row = attrs.get("row", getattr(self.instance, "row", None))
        seat = attrs.get("seat", getattr(self.instance, "seat", None))

        hall = performance.theatre_hall
        errors = {}
        if row > hall.rows:
            errors["row"] = "Row exceeds the number of rows in the theatre hall."
        if seat > hall.seats_in_row:
            errors["seat"] = (
                "Seat exceeds the number of seats per row in the theatre hall."
            )
        if errors:
            raise serializers.ValidationError(errors)

        tickets = Ticket.objects.filter(
            performance=performance,
            row=row,
            seat=seat,
        )
        if self.instance:
            tickets = tickets.exclude(pk=self.instance.pk)
        if tickets.exists():
            raise serializers.ValidationError(
                "This seat is already reserved for this performance."
            )
        return attrs

    def create(self, validated_data):
        validated_data["performance"] = validated_data["reservation"].performance
        try:
            with transaction.atomic():
                return super().create(validated_data)
        except IntegrityError:
            if Ticket.objects.filter(
                performance=validated_data["performance"],
                row=validated_data["row"],
                seat=validated_data["seat"],
            ).exists():
                raise serializers.ValidationError(
                    "This seat is already reserved for this performance."
                )
            raise
