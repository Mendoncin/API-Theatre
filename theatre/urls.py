from rest_framework.routers import DefaultRouter

from theatre.views import (
    ActorViewSet,
    GenreViewSet,
    TheatreHallViewSet,
    PlayViewSet,
    PerformanceViewSet,
)


router = DefaultRouter()

router.register("actors", ActorViewSet)
router.register("genres", GenreViewSet)
router.register("halls", TheatreHallViewSet)
router.register("plays", PlayViewSet)
router.register("performances", PerformanceViewSet)

urlpatterns = router.urls
