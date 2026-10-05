from datetime import datetime, timezone

from crmlumi.models import Order, OrderEvent


def make_row(**overrides):
    row = {
        "ID": "1",
        "Школа/класс": "11А, школа 5",
        "Контактное лицо": "Иванова Мария",
        "Телефон": "+79991234567",
        "Кол-во человек": "24",
        "Дата съёмки": "",
        "Локации": "",
        "Ответственный фотограф": "",
        "Статус": "Лид",
        "Сумма контракта": "",
        "Оплата": "",
        "Примечания": "",
    }
    row.update(overrides)
    return row


def test_from_row_maps_all_fields():
    order = Order.from_row(make_row())
    assert order.order_id == "1"
    assert order.school == "11А, школа 5"
    assert order.contact_name == "Иванова Мария"
    assert order.contact_phone == "+79991234567"
    assert order.people_count == "24"
    assert order.status == "Лид"


def test_from_row_strips_whitespace():
    order = Order.from_row(make_row(**{"Школа/класс": "  11А  "}))
    assert order.school == "11А"


def test_from_row_missing_column_becomes_empty_string():
    row = make_row()
    del row["Примечания"]
    order = Order.from_row(row)
    assert order.notes == ""


def test_order_equality_by_value():
    a = Order.from_row(make_row())
    b = Order.from_row(make_row())
    assert a == b


def test_order_inequality_when_a_field_differs():
    a = Order.from_row(make_row())
    b = Order.from_row(make_row(Статус="Договорились"))
    assert a != b


def test_order_event_holds_previous_and_current():
    order = Order.from_row(make_row())
    event = OrderEvent(
        order_id="1",
        kind="created",
        order=order,
        previous=None,
        timestamp=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    assert event.kind == "created"
    assert event.previous is None
    assert event.order is order


def test_vignette_columns_from_the_vignette_bot_show_on_the_card():
    from crmlumi.notify.order_card import format_order_card
    order = Order.from_row({"ID": "7", "Школа/класс": "Школа 13, 11 Б", "Статус": "Вёрстка",
                            "Виньетки": "12/20 заполнили, 8 одобрили",
                            "Виньетки: ссылка для учеников": "https://t.me/vbot?start=abc"})
    assert order.vignette_progress == "12/20 заполнили, 8 одобрили"
    card = format_order_card(order, shoots=[], days_in_stage=None, responsible=None)
    assert "🎓 Виньетки: 12/20 заполнили, 8 одобрили" in card and "https://t.me/vbot?start=abc" in card
    plain = format_order_card(Order.from_row({"ID": "8"}), shoots=[], days_in_stage=None, responsible=None)
    assert "Виньетки" not in plain
