"""Representation-only contracts for responses introspection cannot infer."""

from rest_framework import serializers


class DetailErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()


class OccupiedSeatSerializer(serializers.Serializer):
    row = serializers.IntegerField()
    seat = serializers.IntegerField()


class TheatreHallAvailabilitySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    rows = serializers.IntegerField()
    seats_in_row = serializers.IntegerField()


class PerformanceAvailabilitySerializer(serializers.Serializer):
    performance = serializers.IntegerField()
    theatre_hall = TheatreHallAvailabilitySerializer()
    capacity = serializers.IntegerField()
    occupied_count = serializers.IntegerField()
    available_count = serializers.IntegerField()
    occupied_seats = OccupiedSeatSerializer(many=True)
