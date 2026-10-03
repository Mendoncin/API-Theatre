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
        read_only_fields = ("created_at",)


class TicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = "__all__"
