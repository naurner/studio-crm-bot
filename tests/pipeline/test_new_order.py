from crmlumi.pipeline.new_order import build_new_order_row, next_order_id
from crmlumi.statuses import STATUS_LEAD


def test_next_order_id_starts_at_one_when_empty():
    assert next_order_id([]) == "1"


def test_next_order_id_increments_max_existing():
    rows = [{"ID": "1"}, {"ID": "3"}, {"ID": "2"}]
    assert next_order_id(rows) == "4"


def test_next_order_id_ignores_non_numeric_ids():
    rows = [{"ID": "abc"}, {"ID": "2"}]
    assert next_order_id(rows) == "3"


def test_next_order_id_ignores_blank_ids():
    rows = [{"ID": ""}, {"ID": "5"}]
    assert next_order_id(rows) == "6"


def test_build_new_order_row_has_lead_status():
    row = build_new_order_row(
        "5",
        school="11А",
        contact_name="Иванова",
        contact_phone="+7999",
        people_count="24",
    )
    assert row["Статус"] == STATUS_LEAD


def test_build_new_order_row_includes_all_fields():
    row = build_new_order_row(
        "5",
        school="11А",
        contact_name="Иванова",
        contact_phone="+7999",
        people_count="24",
    )
    assert row == {
        "ID": "5",
        "Школа/класс": "11А",
        "Контактное лицо": "Иванова",
        "Телефон": "+7999",
        "Кол-во человек": "24",
        "Статус": STATUS_LEAD,
    }
