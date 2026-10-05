"""Единственное место в проекте, импортирующее gspread."""

from __future__ import annotations

import gspread
from gspread.utils import rowcol_to_a1


class GspreadSheetClient:
    """Читает лист как список словарей "заголовок -> значение".

    Строка находится по значению в первой колонке (ID заказа или ID съёмки).
    """

    def __init__(
        self,
        sheet_id: str,
        worksheet_name: str,
        service_account_file: str,
        *,
        create_header: list[str] | None = None,
    ) -> None:
        client = gspread.service_account(filename=service_account_file)
        spreadsheet = client.open_by_key(sheet_id)
        self.created = False
        try:
            self._worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            if create_header is None:
                raise
            self._worksheet = spreadsheet.add_worksheet(
                title=worksheet_name, rows=1000, cols=len(create_header)
            )
            self._worksheet.append_row(create_header)
            self.created = True

    def fetch_rows(self) -> list[dict[str, str]]:
        # Всё строками: иначе gspread превратит телефон «+7999…» в число без плюса.
        return self._worksheet.get_all_records(numericise_ignore=["all"])

    def _find_row(self, row_id: str) -> int:
        cell = self._worksheet.find(row_id, in_column=1)
        if cell is None:
            raise ValueError(f"Строка {row_id!r} не найдена в таблице")
        return cell.row

    def update_order(self, order_id: str, updates: dict[str, str]) -> None:
        """Записывает значения в конкретные колонки строки с этим ID.

        Одним запросом и как есть (RAW): иначе Google сам превращает «2026-10-12»
        в дату в формате локали таблицы, а «+7999…» — в число без плюса.
        """
        row = self._find_row(order_id)
        header = self._worksheet.row_values(1)
        data = [
            {"range": rowcol_to_a1(row, header.index(column_name) + 1), "values": [[value]]}
            for column_name, value in updates.items()
        ]
        if data:
            self._worksheet.batch_update(data, raw=True)

    def append_row(self, row: dict[str, str]) -> None:
        """Добавляет новую строку в конец листа, в порядке колонок заголовка."""
        header = self._worksheet.row_values(1)
        values = [row.get(column_name, "") for column_name in header]
        self._worksheet.append_row(values)

    def ensure_columns(self, names: list[str]) -> list[str]:
        """Дописывает в заголовок недостающие колонки. Возвращает добавленные."""
        header = self._worksheet.row_values(1)
        missing = [name for name in names if name not in header]
        if not missing:
            return []
        needed = len(header) + len(missing)
        if self._worksheet.col_count < needed:
            self._worksheet.add_cols(needed - self._worksheet.col_count)
        start = len(header) + 1
        self._worksheet.batch_update(
            [
                {"range": rowcol_to_a1(1, start + offset), "values": [[name]]}
                for offset, name in enumerate(missing)
            ],
            raw=True,
        )
        return missing

    def delete_row(self, row_id: str) -> None:
        self._worksheet.delete_rows(self._find_row(row_id))
