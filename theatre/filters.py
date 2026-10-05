from django.utils import timezone
from django_filters import rest_framework as filters

from theatre.models import Performance, Play


class PlayFilter(filters.FilterSet):
    genre = filters.NumberFilter(field_name="genres__id", distinct=True)

    class Meta:
        model = Play
        fields = []


class PerformanceFilter(filters.FilterSet):
    play = filters.NumberFilter(field_name="play_id")
    date = filters.DateFilter(
        field_name="show_time", lookup_expr="date", input_formats=["%Y-%m-%d"],
    )
    upcoming = filters.BooleanFilter(method="filter_upcoming")

    class Meta:
        model = Performance
        fields = []

    def filter_upcoming(self, queryset, name, value):
        if value:
            return queryset.filter(show_time__gt=timezone.now())
        return queryset
