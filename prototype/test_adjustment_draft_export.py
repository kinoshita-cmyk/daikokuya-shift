from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import openpyxl

from prototype.excel_exporter import export_shift_to_excel
from prototype.export_config import EXPORT_COLUMN_ORDER
from prototype.models import MonthlyShift, ShiftAssignment, Store
from prototype.pdf_exporter import export_shift_to_pdf


class AdjustmentDraftExportTest(unittest.TestCase):
    @staticmethod
    def _shift() -> MonthlyShift:
        return MonthlyShift(
            year=2026,
            month=9,
            assignments=[
                ShiftAssignment(employee="今津", day=1, store=Store.OFF),
                ShiftAssignment(employee="今津", day=2, store=Store.AKABANE),
            ],
        )

    def test_excel_marks_only_requested_cell_with_red_border(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "draft.xlsx"
            export_shift_to_excel(
                self._shift(),
                path,
                off_request_cells={("今津", 1)},
            )

            workbook = openpyxl.load_workbook(path)
            worksheet = workbook.active
            employee_col = 4 + EXPORT_COLUMN_ORDER.index("今津")
            requested = worksheet.cell(row=8, column=employee_col)
            ordinary = worksheet.cell(row=9, column=employee_col)

            self.assertEqual(requested.border.left.style, "thick")
            self.assertTrue(
                str(requested.border.left.color.rgb).endswith("FF0000")
            )
            self.assertEqual(ordinary.border.left.style, "thin")
            for edge in ("left", "right", "top", "bottom"):
                self.assertEqual(getattr(requested.border, edge).style, "thick")
            self.assertEqual(worksheet.cell(row=8, column=employee_col - 1).border.right.style, "thick")
            self.assertEqual(worksheet.cell(row=8, column=employee_col + 1).border.left.style, "thick")
            self.assertEqual(ordinary.border.top.style, "thick")

    def test_standard_export_still_has_no_requested_off_outline(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "standard.xlsx"
            export_shift_to_excel(self._shift(), path)
            workbook = openpyxl.load_workbook(path)
            requested = workbook.active.cell(row=8, column=4 + EXPORT_COLUMN_ORDER.index("今津"))
            self.assertEqual(requested.border.left.style, "thin")

    def test_pdf_accepts_off_request_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "draft.pdf"
            export_shift_to_pdf(
                self._shift(),
                path,
                off_request_cells={("今津", 1)},
            )

            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 1_000)


if __name__ == "__main__":
    unittest.main()
