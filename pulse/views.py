import random

from django.contrib.auth import authenticate
from django.db.models import Count, Q
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from .models import NotificationLog, PriceAlert
from .permissions import IsAdminUser
from .serializers import LoginSerializer, PriceAlertSerializer, RegisterSerializer

MOCK_PRICES = {
    "DEL-BOM": (3000, 7000),
    "BLR-HYD": (1500, 4000),
    "DEL-BLR": (4000, 9000),
    "BOM-GOA": (2000, 5000),
}


class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {
                "username": user.username,
                "access": str(AccessToken.for_user(user)),
                "role": user.profile.role,
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(request, **serializer.validated_data)
        if user is None:
            return Response({"detail": "Invalid username or password."}, status=status.HTTP_401_UNAUTHORIZED)
        return Response({"username": user.username, "access": str(AccessToken.for_user(user))})


class AlertListCreateView(generics.ListCreateAPIView):
    serializer_class = PriceAlertSerializer

    def get_queryset(self):
        return PriceAlert.objects.filter(user=self.request.user).order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class AlertDeactivateView(generics.GenericAPIView):
    serializer_class = PriceAlertSerializer

    def get_queryset(self):
        # Scoping to the current user makes other users' alerts return 404.
        return PriceAlert.objects.filter(user=self.request.user)

    def delete(self, request, pk):
        alert = self.get_object()
        alert.status = PriceAlert.Status.INACTIVE
        alert.save(update_fields=["status"])
        return Response({"status": alert.status})


class FlightPriceView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        route = request.query_params.get("route", "").strip().upper()
        if route not in MOCK_PRICES:
            return Response({"detail": "Unknown route."}, status=status.HTTP_404_NOT_FOUND)
        low, high = MOCK_PRICES[route]
        return Response({"route": route, "price": random.randint(low, high)})


class AdminSummaryView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        alert_counts = PriceAlert.objects.aggregate(
            total_alerts=Count("id"),
            active_alerts=Count("id", filter=Q(status=PriceAlert.Status.ACTIVE)),
            triggered_alerts=Count("id", filter=Q(status=PriceAlert.Status.TRIGGERED)),
        )
        total_notifications = NotificationLog.objects.aggregate(total=Count("id"))["total"]
        top_routes = (
            PriceAlert.objects.values("origin", "destination")
            .annotate(alert_count=Count("id"))
            .order_by("-alert_count", "origin", "destination")[:5]
        )
        return Response(
            {
                **alert_counts,
                "total_notifications": total_notifications,
                "top_routes": [
                    {"route": f"{row['origin']}-{row['destination']}", "alert_count": row["alert_count"]}
                    for row in top_routes
                ],
            }
        )
