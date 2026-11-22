import logging
from decimal import Decimal, InvalidOperation

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction

from .models import NotificationLog, PriceAlert

logger = logging.getLogger(__name__)


def price_meets_threshold(current_price, threshold_price):
    """An alert fires when the current price is at or below its threshold."""
    return Decimal(str(current_price)) <= Decimal(str(threshold_price))


def fetch_price(route):
    """Return the current price for a route from the mock endpoint, or None on failure."""
    url = f"{settings.INTERNAL_API_BASE_URL.rstrip('/')}/api/flights/price/"
    try:
        response = requests.get(url, params={"route": route}, timeout=settings.PRICE_API_TIMEOUT_SECONDS)
        response.raise_for_status()
        return Decimal(str(response.json()["price"]))
    except (requests.RequestException, ValueError, KeyError, InvalidOperation) as exc:
        logger.warning("Could not fetch price for %s: %s", route, exc)
        return None


@shared_task
def check_prices():
    active_alerts = PriceAlert.objects.filter(status=PriceAlert.Status.ACTIVE)
    routes = active_alerts.values_list("origin", "destination").distinct()

    dispatched = 0
    for origin, destination in routes:
        current_price = fetch_price(f"{origin}-{destination}")
        if current_price is None:
            continue
        for alert in active_alerts.filter(origin=origin, destination=destination):
            if price_meets_threshold(current_price, alert.threshold_price):
                # Sent as a string so the JSON serializer keeps the exact decimal value.
                send_notification.delay(alert.id, str(current_price))
                dispatched += 1
    return dispatched


@shared_task
def send_notification(alert_id, triggered_price):
    price = Decimal(str(triggered_price))
    with transaction.atomic():
        # Lock the row so concurrent tasks cannot notify the same alert twice.
        alert = PriceAlert.objects.select_for_update().filter(pk=alert_id).first()
        if alert is None:
            logger.info("Alert %s no longer exists; skipping notification.", alert_id)
            return None
        if alert.status != PriceAlert.Status.ACTIVE:
            logger.info("Alert %s is %s; skipping notification.", alert_id, alert.status)
            return None

        notification = NotificationLog.objects.create(
            alert=alert,
            triggered_price=price,
            message=(
                f"Price drop on {alert.route}: current price {price} is at or below "
                f"your threshold of {alert.threshold_price}."
            ),
        )
        alert.status = PriceAlert.Status.TRIGGERED
        alert.save(update_fields=["status"])
    return notification.id
