from django.urls import path

from . import views

urlpatterns = [
    path("auth/register/", views.RegisterView.as_view(), name="register"),
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("alerts/", views.AlertListCreateView.as_view(), name="alert-list"),
    path("alerts/<int:pk>/", views.AlertDeactivateView.as_view(), name="alert-detail"),
    path("flights/price/", views.FlightPriceView.as_view(), name="flight-price"),
    path("admin/summary/", views.AdminSummaryView.as_view(), name="admin-summary"),
]
