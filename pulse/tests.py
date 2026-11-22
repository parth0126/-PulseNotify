from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from .models import NotificationLog, PriceAlert, UserProfile
from .tasks import check_prices, price_meets_threshold, send_notification


def make_user(username, role=UserProfile.Role.USER):
    user = User.objects.create_user(username=username, password="testpass123")
    if role != UserProfile.Role.USER:
        user.profile.role = role
        user.profile.save()
    return user


def make_alert(user, origin="DEL", destination="BOM", threshold="5000.00", status=PriceAlert.Status.ACTIVE):
    return PriceAlert.objects.create(
        user=user, origin=origin, destination=destination, threshold_price=Decimal(threshold), status=status
    )


class PriceThresholdTests(TestCase):
    """Group A: the comparison used by check_prices."""

    def test_price_below_threshold_triggers(self):
        self.assertTrue(price_meets_threshold(4999, Decimal("5000.00")))

    def test_price_above_threshold_does_not_trigger(self):
        self.assertFalse(price_meets_threshold(5001, Decimal("5000.00")))

    def test_price_equal_to_threshold_triggers(self):
        self.assertTrue(price_meets_threshold(5000, Decimal("5000.00")))

    @patch("pulse.tasks.send_notification.delay")
    @patch("pulse.tasks.requests.get")
    def test_check_prices_dispatches_only_matching_alerts(self, mock_get, mock_delay):
        user = make_user("alice")
        cheap = make_alert(user, threshold="4500.00")
        equal = make_alert(user, threshold="4200.00")
        make_alert(user, threshold="4000.00")  # price above threshold
        make_alert(user, threshold="9000.00", status=PriceAlert.Status.INACTIVE)
        mock_get.return_value = Mock(status_code=200, json=Mock(return_value={"route": "DEL-BOM", "price": 4200}))

        dispatched = check_prices()

        self.assertEqual(dispatched, 2)
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["params"], {"route": "DEL-BOM"})
        dispatched_ids = sorted(call.args[0] for call in mock_delay.call_args_list)
        self.assertEqual(dispatched_ids, sorted([cheap.id, equal.id]))

    @patch("pulse.tasks.send_notification.delay")
    @patch("pulse.tasks.requests.get", side_effect=requests.ConnectionError("down"))
    def test_check_prices_survives_request_failure(self, mock_get, mock_delay):
        make_alert(make_user("alice"))
        self.assertEqual(check_prices(), 0)
        mock_delay.assert_not_called()


class NotificationLogTests(TestCase):
    """Group B: notification creation via the send_notification task."""

    def setUp(self):
        self.alert = make_alert(make_user("alice"))

    def test_notification_created_for_valid_alert(self):
        NotificationLog.objects.create(alert=self.alert, triggered_price=Decimal("4100.00"), message="DEL-BOM drop")
        self.assertEqual(self.alert.notifications.count(), 1)

    def test_task_creates_notification_and_marks_alert_triggered(self):
        send_notification(self.alert.id, "4100")

        notification = NotificationLog.objects.get()
        self.assertEqual(notification.alert, self.alert)
        self.assertEqual(notification.triggered_price, Decimal("4100.00"))
        self.assertIn("DEL-BOM", notification.message)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, PriceAlert.Status.TRIGGERED)

    def test_no_duplicate_notification_for_triggered_alert(self):
        send_notification(self.alert.id, "4100")
        send_notification(self.alert.id, "4000")
        self.assertEqual(NotificationLog.objects.count(), 1)

    def test_no_notification_for_inactive_alert(self):
        self.alert.status = PriceAlert.Status.INACTIVE
        self.alert.save()
        send_notification(self.alert.id, "4100")
        self.assertFalse(NotificationLog.objects.exists())

    def test_deleted_alert_is_handled(self):
        alert_id = self.alert.id
        self.alert.delete()
        self.assertIsNone(send_notification(alert_id, "4100"))


class AlertScopingTests(TestCase):
    """Group C: users only see and modify their own alerts."""

    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.alice_alert = make_alert(self.alice, "DEL", "BOM")
        self.bob_alert = make_alert(self.bob, "BLR", "HYD")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def test_list_returns_only_own_alerts(self):
        response = self.client.get("/api/alerts/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([alert["id"] for alert in response.json()], [self.alice_alert.id])

    def test_list_empty_for_user_without_alerts(self):
        self.client.force_authenticate(make_user("carol"))
        response = self.client.get("/api/alerts/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_create_sets_owner_from_request(self):
        response = self.client.post(
            "/api/alerts/",
            {"origin": "del", "destination": "blr", "threshold_price": "6000.00", "user": self.bob.id},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["route"], "DEL-BLR")
        self.assertEqual(response.json()["status"], "active")
        self.assertEqual(PriceAlert.objects.get(id=response.json()["id"]).user, self.alice)

    def test_create_rejects_invalid_input(self):
        response = self.client.post("/api/alerts/", {"origin": "DEL"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_delete_own_alert_soft_deletes(self):
        response = self.client.delete(f"/api/alerts/{self.alice_alert.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "inactive"})
        self.alice_alert.refresh_from_db()
        self.assertEqual(self.alice_alert.status, PriceAlert.Status.INACTIVE)

    def test_delete_other_users_alert_returns_404_and_leaves_it_unchanged(self):
        response = self.client.delete(f"/api/alerts/{self.bob_alert.id}/")
        self.assertEqual(response.status_code, 404)
        self.bob_alert.refresh_from_db()
        self.assertEqual(self.bob_alert.status, PriceAlert.Status.ACTIVE)

    def test_alerts_require_authentication(self):
        self.assertEqual(APIClient().get("/api/alerts/").status_code, 401)


class AuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_register_creates_user_with_default_role(self):
        response = self.client.post(
            "/api/auth/register/",
            {"username": "alice", "password": "securepass", "email": "alice@example.com", "role": "admin"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], "user")
        self.assertIn("access", response.json())
        user = User.objects.get(username="alice")
        self.assertTrue(user.check_password("securepass"))
        self.assertEqual(user.profile.role, UserProfile.Role.USER)

    def test_register_duplicate_username_returns_400(self):
        make_user("alice")
        response = self.client.post("/api/auth/register/", {"username": "alice", "password": "securepass"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_login_returns_token_usable_on_protected_endpoint(self):
        make_user("alice")
        response = self.client.post("/api/auth/login/", {"username": "alice", "password": "testpass123"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
        self.assertEqual(self.client.get("/api/alerts/").status_code, 200)

    def test_login_invalid_credentials_returns_401(self):
        make_user("alice")
        response = self.client.post("/api/auth/login/", {"username": "alice", "password": "wrong"}, format="json")
        self.assertEqual(response.status_code, 401)


class FlightPriceTests(TestCase):
    def test_known_route_returns_price_in_range(self):
        response = APIClient().get("/api/flights/price/", {"route": "DEL-BOM"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route"], "DEL-BOM")
        self.assertTrue(3000 <= response.json()["price"] <= 7000)

    def test_unknown_route_returns_404(self):
        response = APIClient().get("/api/flights/price/", {"route": "XXX-YYY"})
        self.assertEqual(response.status_code, 404)


class AdminSummaryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_user("alice")
        self.admin = make_user("admin", role=UserProfile.Role.ADMIN)

    def test_regular_user_gets_403(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/admin/summary/").status_code, 403)

    def test_unauthenticated_gets_401(self):
        self.assertEqual(self.client.get("/api/admin/summary/").status_code, 401)

    def test_user_without_profile_gets_403(self):
        UserProfile.objects.filter(user=self.user).delete()
        self.user.refresh_from_db()
        self.client.force_authenticate(User.objects.get(pk=self.user.pk))
        self.assertEqual(self.client.get("/api/admin/summary/").status_code, 403)

    def test_admin_gets_aggregated_summary(self):
        triggered = make_alert(self.user, "DEL", "BOM", status=PriceAlert.Status.TRIGGERED)
        make_alert(self.user, "DEL", "BOM")
        make_alert(self.admin, "BLR", "HYD")
        make_alert(self.admin, "BOM", "GOA", status=PriceAlert.Status.INACTIVE)
        NotificationLog.objects.create(alert=triggered, triggered_price=Decimal("3500"), message="DEL-BOM drop")

        self.client.force_authenticate(self.admin)
        response = self.client.get("/api/admin/summary/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_alerts"], 4)
        self.assertEqual(data["active_alerts"], 2)
        self.assertEqual(data["triggered_alerts"], 1)
        self.assertEqual(data["total_notifications"], 1)
        self.assertEqual(data["top_routes"][0], {"route": "DEL-BOM", "alert_count": 2})
        self.assertEqual(len(data["top_routes"]), 3)
