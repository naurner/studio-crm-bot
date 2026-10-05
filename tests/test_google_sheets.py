import gspread
import pytest
from gspread.utils import a1_to_rowcol

from crmlumi.google_sheets import GspreadSheetClient


class FakeWorksheet:
    def __init__(self, rows):
        self._rows = rows

    def get_all_records(self, numericise_ignore=None):
        assert numericise_ignore == ["all"]
        return self._rows


class FakeSpreadsheet:
    def __init__(self, worksheet):
        self._worksheet = worksheet

    def worksheet(self, name):
        assert name == "Заказы"
        return self._worksheet


class FakeGspreadClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, sheet_id):
        assert sheet_id == "sheet-123"
        return self._spreadsheet


def test_fetch_rows_delegates_to_gspread(monkeypatch):
    rows = [{"ID": "1"}]
    fake_client = FakeGspreadClient(FakeSpreadsheet(FakeWorksheet(rows)))

    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: fake_client,
    )

    client = GspreadSheetClient("sheet-123", "Заказы", "creds.json")
    assert client.fetch_rows() == rows


class FakeWorksheetWithUpdates(FakeWorksheet):
    def __init__(self, rows, header):
        super().__init__(rows)
        self._header = header
        self.updates: list[tuple[int, int, str]] = []
        self.appended: list[list[str]] = []

    def row_values(self, row_number):
        return self._header

    def find(self, value, in_column=None):
        for i, row in enumerate(self._rows, start=2):
            if row.get(self._header[0]) == value:
                return type("Cell", (), {"row": i})()
        return None

    def batch_update(self, data, raw=True):
        assert raw is True
        for item in data:
            row, col = a1_to_rowcol(item["range"])
            self.updates.append((row, col, item["values"][0][0]))

    def append_row(self, values):
        self.appended.append(values)


def test_update_order_writes_matching_columns(monkeypatch):
    header = ["ID", "Школа/класс", "Ответственный фотограф", "Статус"]
    rows = [{"ID": "1", "Школа/класс": "11А"}, {"ID": "2", "Школа/класс": "11Б"}]
    fake_worksheet = FakeWorksheetWithUpdates(rows, header)
    fake_client = FakeGspreadClient(FakeSpreadsheet(fake_worksheet))

    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: fake_client,
    )

    client = GspreadSheetClient("sheet-123", "Заказы", "creds.json")
    client.update_order(
        "2", {"Ответственный фотограф": "Алексей", "Статус": "Съёмка назначена"}
    )

    assert (3, 3, "Алексей") in fake_worksheet.updates
    assert (3, 4, "Съёмка назначена") in fake_worksheet.updates


def test_update_order_raises_when_order_not_found(monkeypatch):
    header = ["ID", "Школа/класс"]
    fake_worksheet = FakeWorksheetWithUpdates([], header)
    fake_client = FakeGspreadClient(FakeSpreadsheet(fake_worksheet))

    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: fake_client,
    )

    client = GspreadSheetClient("sheet-123", "Заказы", "creds.json")
    with pytest.raises(ValueError):
        client.update_order("missing", {"Статус": "x"})


def test_append_row_writes_values_in_header_order(monkeypatch):
    header = ["ID", "Школа/класс", "Статус"]
    fake_worksheet = FakeWorksheetWithUpdates([], header)
    fake_client = FakeGspreadClient(FakeSpreadsheet(fake_worksheet))

    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: fake_client,
    )

    client = GspreadSheetClient("sheet-123", "Заказы", "creds.json")
    client.append_row({"ID": "5", "Школа/класс": "11А", "Статус": "Лид"})

    assert fake_worksheet.appended == [["5", "11А", "Лид"]]


def test_append_row_fills_missing_columns_with_empty_string(monkeypatch):
    header = ["ID", "Школа/класс", "Статус"]
    fake_worksheet = FakeWorksheetWithUpdates([], header)
    fake_client = FakeGspreadClient(FakeSpreadsheet(fake_worksheet))

    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: fake_client,
    )

    client = GspreadSheetClient("sheet-123", "Заказы", "creds.json")
    client.append_row({"ID": "5", "Статус": "Лид"})

    assert fake_worksheet.appended == [["5", "", "Лид"]]


class FakeWorksheetFull(FakeWorksheetWithUpdates):
    def __init__(self, rows, header):
        super().__init__(rows, header)
        self.deleted_rows: list[int] = []

    def delete_rows(self, row_number):
        self.deleted_rows.append(row_number)


class FakeSpreadsheetWithoutSheet:
    def __init__(self):
        self.added: list[tuple[str, int]] = []
        self.created_sheet = None

    def worksheet(self, name):
        raise gspread.WorksheetNotFound(name)

    def add_worksheet(self, title, rows, cols):
        self.added.append((title, cols))
        self.created_sheet = FakeWorksheetFull([], [])
        return self.created_sheet


def test_missing_worksheet_is_created_with_header(monkeypatch):
    spreadsheet = FakeSpreadsheetWithoutSheet()
    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: FakeGspreadClient(spreadsheet),
    )
    client = GspreadSheetClient("sheet-123", "Съёмки", "creds.json", create_header=["ID съёмки", "Дата"])
    assert client.created is True
    assert spreadsheet.added == [("Съёмки", 2)]
    assert spreadsheet.created_sheet.appended == [["ID съёмки", "Дата"]]


def test_missing_worksheet_without_header_still_fails(monkeypatch):
    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: FakeGspreadClient(FakeSpreadsheetWithoutSheet()),
    )
    with pytest.raises(gspread.WorksheetNotFound):
        GspreadSheetClient("sheet-123", "Заказы", "creds.json")


def test_ensure_columns_appends_missing_headers(monkeypatch):
    header = ["ID", "Статус"]
    worksheet = FakeWorksheetFull([], header)
    worksheet.col_count = 2
    worksheet.added_cols = 0

    def add_cols(count):
        worksheet.added_cols += count

    worksheet.add_cols = add_cols
    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: FakeGspreadClient(FakeSpreadsheet(worksheet)),
    )
    client = GspreadSheetClient("sheet-123", "Заказы", "creds.json")
    assert client.ensure_columns(["ID", "Статус", "Ссылка на макет"]) == ["Ссылка на макет"]
    assert worksheet.added_cols == 1
    assert worksheet.updates == [(1, 3, "Ссылка на макет")]
    worksheet._header = ["ID", "Статус", "Ссылка на макет"]
    assert client.ensure_columns(["ID", "Ссылка на макет"]) == []


def test_delete_row_removes_matching_row(monkeypatch):
    header = ["ID", "Школа/класс"]
    worksheet = FakeWorksheetFull([{"ID": "1"}, {"ID": "2"}], header)
    monkeypatch.setattr(
        "crmlumi.google_sheets.gspread.service_account",
        lambda filename: FakeGspreadClient(FakeSpreadsheet(worksheet)),
    )
    client = GspreadSheetClient("sheet-123", "Заказы", "creds.json")
    client.delete_row("2")
    assert worksheet.deleted_rows == [3]
