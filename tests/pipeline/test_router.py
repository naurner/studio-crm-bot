from datetime import datetime, timezone

from crmlumi.models import Order, OrderEvent
from crmlumi.pipeline.router import role_for_event, task_role_for_status
from crmlumi.roles import (
    ROLE_DATES_LOCATIONS,
    ROLE_LAYOUT,
    ROLE_NEW_LEADS,
    ROLE_PHOTO_PROCESSING,
    ROLE_PHOTOGRAPHER,
    ROLE_PRINTING,
)
from crmlumi.statuses import (
    STATUS_AGREED,
    STATUS_CLIENT_APPROVAL,
    STATUS_DATE_LOCATION_SET,
    STATUS_LAYOUT,
    STATUS_LEAD,
    STATUS_PHOTO_PROCESSING,
    STATUS_PRINTING,
    STATUS_SEEKING_PHOTOGRAPHER,
)


def make_order(**overrides) -> Order:
    base = dict(
        order_id="1",
        school="11А",
        contact_name="Иванова",
        contact_phone="+7999",
        people_count="24",
        shoot_date="",
        locations="",
        photographer="",
        status="Лид",
        contract_amount="",
        payment_status="",
        notes="",
    )
    base.update(overrides)
    return Order(**base)


def _event(kind: str, **overrides) -> OrderEvent:
    order = make_order(**overrides)
    return OrderEvent(order.order_id, kind, order, None, datetime.now(timezone.utc))


def test_created_event_routes_to_new_leads_role():
    assert role_for_event(_event("created")) == ROLE_NEW_LEADS


def test_status_changed_event_has_no_role_yet():
    # Роутинг статусов на claim-механику и Microsoft To Do — Фазы 3-4.
    assert role_for_event(_event("status_changed")) is None


def test_date_location_set_event_has_no_role_yet():
    assert role_for_event(_event("date_location_set")) is None


def test_updated_event_has_no_role():
    assert role_for_event(_event("updated")) is None


def test_status_changed_to_seeking_photographer_routes_to_photographer_role():
    assert (
        role_for_event(_event("status_changed", status=STATUS_SEEKING_PHOTOGRAPHER))
        == ROLE_PHOTOGRAPHER
    )


def test_status_date_location_set_still_has_no_role():
    # Промежуточный статус — задача создаётся раньше, при "Договорились".
    assert role_for_event(_event("status_changed", status=STATUS_DATE_LOCATION_SET)) is None


def test_status_agreed_routes_to_dates_locations_role():
    assert (
        role_for_event(_event("status_changed", status=STATUS_AGREED))
        == ROLE_DATES_LOCATIONS
    )


def test_status_photo_processing_routes_to_photo_processing_role():
    assert (
        role_for_event(_event("status_changed", status=STATUS_PHOTO_PROCESSING))
        == ROLE_PHOTO_PROCESSING
    )


def test_status_layout_routes_to_layout_role():
    assert role_for_event(_event("status_changed", status=STATUS_LAYOUT)) == ROLE_LAYOUT


def test_status_client_approval_routes_to_layout_role_too():
    assert (
        role_for_event(_event("status_changed", status=STATUS_CLIENT_APPROVAL)) == ROLE_LAYOUT
    )


def test_task_role_for_lead_is_new_leads():
    assert task_role_for_status(STATUS_LEAD) == ROLE_NEW_LEADS


def test_task_role_follows_status_map_except_photographer():
    assert task_role_for_status(STATUS_AGREED) == ROLE_DATES_LOCATIONS
    assert task_role_for_status(STATUS_CLIENT_APPROVAL) == ROLE_LAYOUT
    assert task_role_for_status(STATUS_SEEKING_PHOTOGRAPHER) is None
    assert task_role_for_status(STATUS_DATE_LOCATION_SET) is None
    assert task_role_for_status("что-то непонятное") is None


def test_status_printing_routes_to_printing_role():
    assert (
        role_for_event(_event("status_changed", status=STATUS_PRINTING)) == ROLE_PRINTING
    )
