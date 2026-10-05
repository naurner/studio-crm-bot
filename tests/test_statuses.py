from crmlumi.statuses import (
    FUNNEL_ORDER,
    STATUS_DELIVERED,
    STATUS_LEAD,
    STATUS_SEEKING_PHOTOGRAPHER,
    STATUS_SHOOT_ASSIGNED,
)


def test_funnel_order_has_ten_statuses():
    assert len(FUNNEL_ORDER) == 10


def test_funnel_order_starts_with_lead_and_ends_with_delivered():
    assert FUNNEL_ORDER[0] == STATUS_LEAD
    assert FUNNEL_ORDER[-1] == STATUS_DELIVERED


def test_seeking_photographer_precedes_shoot_assigned():
    assert FUNNEL_ORDER.index(STATUS_SEEKING_PHOTOGRAPHER) < FUNNEL_ORDER.index(
        STATUS_SHOOT_ASSIGNED
    )
