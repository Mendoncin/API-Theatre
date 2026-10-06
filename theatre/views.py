from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view,
)
from rest_framework import filters, mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from theatre.exceptions import CancellationNotAllowed
from theatre.filters import PerformanceFilter, PlayFilter
from theatre.permissions import IsAdminOrReadOnly
from theatre.schema import DetailErrorSerializer, PerformanceAvailabilitySerializer

from theatre.models import (
    Actor,
    Genre,
    TheatreHall,
    Play,
    Performance,
    Reservation,
    Ticket,
)
from theatre.serializers import (
    ActorSerializer,
    GenreSerializer,
    TheatreHallSerializer,
    PlayListSerializer,
    PlayDetailSerializer,
    PlayWriteSerializer,
    PerformanceSerializer,
    PerformanceWriteSerializer,
    ReservationCreateSerializer,
    ReservationSerializer,
    TicketSerializer,
)


class ProtectedDeleteMixin:
    protected_delete_message = (
        "This resource cannot be deleted because it has protected related data."
    )

    @extend_schema(responses={
        204: None,
        409: OpenApiResponse(
            DetailErrorSerializer,
            description="This resource has protected related data.",
        ),
    })
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        try:
            self.perform_destroy(instance)
        except ProtectedError:
            return Response(
                {"detail": self.protected_delete_message},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(auth=[], methods=["GET"])
class ActorViewSet(viewsets.ModelViewSet):
    queryset = Actor.objects.order_by("pk")
    serializer_class = ActorSerializer
    permission_classes = [IsAdminOrReadOnly]


@extend_schema(auth=[], methods=["GET"])
class GenreViewSet(viewsets.ModelViewSet):
    queryset = Genre.objects.order_by("pk")
    serializer_class = GenreSerializer
    permission_classes = [IsAdminOrReadOnly]


@extend_schema(auth=[], methods=["GET"])
class TheatreHallViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    queryset = TheatreHall.objects.order_by("pk")
    serializer_class = TheatreHallSerializer
    permission_classes = [IsAdminOrReadOnly]


@extend_schema(auth=[], methods=["GET"])
class PlayViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    queryset = Play.objects.all()
    serializer_class = PlayWriteSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = PlayFilter
    search_fields = ["title"]
    ordering_fields = ["title"]
    ordering = ["title"]

    def filter_queryset(self, queryset):
        # Collection parameters must not affect detail or write actions.
        if self.action == "list":
            return super().filter_queryset(queryset)
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return PlayListSerializer
        if self.action == "retrieve":
            return PlayDetailSerializer
        return PlayWriteSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == "list":
            return queryset.prefetch_related("genres")
        if self.action == "retrieve":
            return queryset.prefetch_related("genres", "actors")
        return queryset


@extend_schema(auth=[], methods=["GET"])
@extend_schema_view(list=extend_schema(parameters=[
    OpenApiParameter(
        "upcoming", bool,
        description=(
            "Use true for future performances, false for started/past performances; "
            "omit for all. Only lowercase true and false are accepted."
        ),
    ),
]))
class PerformanceViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    queryset = Performance.objects.all()
    serializer_class = PerformanceSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = PerformanceFilter
    ordering_fields = ["show_time"]
    ordering = ["show_time"]

    def filter_queryset(self, queryset):
        # In particular, availability must ignore collection parameters.
        if self.action == "list":
            return super().filter_queryset(queryset)
        return queryset

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return PerformanceWriteSerializer
        return PerformanceSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in ("list", "retrieve"):
            return queryset.select_related("play", "theatre_hall")
        return queryset

    @extend_schema(
        responses=PerformanceAvailabilitySerializer,
        description=(
            "Returns a snapshot of the current seat occupancy for this performance. "
            "Availability does not guarantee that a seat will remain available "
            "until reservation creation."
        ),
    )
    @action(detail=True, methods=["get"], url_path="availability")
    def availability(self, request, pk=None):
        performance = self.get_object()
        hall = performance.theatre_hall
        occupied_seats = list(
            Ticket.objects.filter(performance=performance)
            .order_by("row", "seat")
            .values("row", "seat")
        )
        capacity = hall.rows * hall.seats_in_row
        occupied_count = len(occupied_seats)
        return Response({
            "performance": performance.pk,
            "theatre_hall": {
                "id": hall.pk,
                "name": hall.name,
                "rows": hall.rows,
                "seats_in_row": hall.seats_in_row,
            },
            "capacity": capacity,
            "occupied_count": occupied_count,
            "available_count": capacity - occupied_count,
            "occupied_seats": occupied_seats,
        })


@extend_schema_view(
    retrieve=extend_schema(parameters=[OpenApiParameter("id", int, OpenApiParameter.PATH)]),
    destroy=extend_schema(
        parameters=[OpenApiParameter("id", int, OpenApiParameter.PATH)],
        responses={
            204: None,
            409: OpenApiResponse(
                DetailErrorSerializer,
                description="Cancellation is allowed only before the performance starts.",
            ),
        },
    ),
)
class ReservationViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ReservationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == "create":
            return ReservationCreateSerializer
        return ReservationSerializer

    def get_queryset(self):
        return Reservation.objects.filter(user=self.request.user).select_related(
            "performance__play", "performance__theatre_hall",
        ).prefetch_related("tickets").order_by("pk")

    @extend_schema(
        request=ReservationCreateSerializer,
        responses={
            201: ReservationSerializer,
            409: OpenApiResponse(
                DetailErrorSerializer,
                description="One or more seats are unavailable, including concurrency conflicts.",
            ),
        },
        description=(
            "Creates a reservation and all requested tickets atomically. "
            "The user comes from authentication."
        ),
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        data = ReservationSerializer(
            serializer.instance, context=self.get_serializer_context()
        ).data
        return Response(data, status=status.HTTP_201_CREATED, headers=self.get_success_headers(data))

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def perform_destroy(self, instance):
        with transaction.atomic():
            reservation = get_object_or_404(
                Reservation.objects.select_for_update(),
                pk=instance.pk, user=self.request.user,
            )
            if reservation.performance.show_time <= timezone.now():
                raise CancellationNotAllowed()
            reservation.delete()


@extend_schema_view(
    retrieve=extend_schema(parameters=[OpenApiParameter("id", int, OpenApiParameter.PATH)]),
    destroy=extend_schema(
        parameters=[OpenApiParameter("id", int, OpenApiParameter.PATH)],
        responses={
            204: None,
            409: OpenApiResponse(
                DetailErrorSerializer,
                description="Cancellation is allowed only before the performance starts.",
            ),
        },
    ),
)
class TicketViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = TicketSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Ticket.objects.filter(reservation__user=self.request.user).order_by("pk")

    def perform_destroy(self, instance):
        with transaction.atomic():
            reservation = get_object_or_404(
                Reservation.objects.select_for_update(),
                pk=instance.reservation_id, user=self.request.user,
            )
            if reservation.performance.show_time <= timezone.now():
                raise CancellationNotAllowed()
            instance.delete()
            if not reservation.tickets.exists():
                reservation.delete()
