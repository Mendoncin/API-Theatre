from rest_framework import status
from rest_framework.exceptions import APIException


class SeatUnavailable(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "One or more seats are no longer available for this performance."
    default_code = "seat_unavailable"


class CancellationNotAllowed(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Reservations for performances that have already started cannot be cancelled."
    default_code = "cancellation_not_allowed"
