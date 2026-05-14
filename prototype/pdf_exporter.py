"""
シフト表 PDF 出力（Excel印刷レイアウト準拠・A4縦1ページ）
================================================
Excel 出力を A4 縦 1ページに縮小印刷した時と同じ構成で PDF を生成する。
"""

from __future__ import annotations
from datetime import date
from pathlib import Path
from typing import Optional
from calendar import monthrange

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

from .models import MonthlyShift, Store
from .paths import OUTPUT_DIR
from .excel_exporter import (
    COLUMN_WIDTHS,
    DEFAULT_FOOTER_NOTES,
    EXPORT_COLUMN_ORDER,
    SHORT_STAFF_STORE_LABELS,
)


WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

# Excel の列幅単位をPDFのポイントへ近似変換する係数。
# 実際のExcel印刷も縮小率がかかるため、相対幅が合うことを優先する。
EXCEL_WIDTH_TO_POINTS = 5.25

ROW_HEIGHTS = {
    "title": 90.0,
    "comment": 90.0,
    "comment_last": 93.0,
    "legend": 45.0,
    "header": 45.0,
    "data": 45.0,
    "spacer": 46.0,
    "footer": 49.0,
}

FONT_SIZES = {
    "title": 60.0,
    "comment": 43.0,
    "legend": 36.0,
    "header": 24.0,
    "cell": 35.0,
    "footer": 27.0,
}

JAPANESE_TTF_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def _register_japanese_font() -> str:
    """日本語が文字化けしないPDFフォント名を返す。"""
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        return "HeiseiKakuGo-W5"
    except Exception:
        pass

    for path in JAPANESE_TTF_PATHS:
        if not Path(path).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("DaikokuyaJP", path))
            return "DaikokuyaJP"
        except Exception:
            continue

    # 最後の保険。日本語表示は弱いが、PDF生成自体は止めない。
    return "Helvetica"


def _short_staff_text(short_staff_days: object, day: int) -> str:
    if isinstance(short_staff_days, dict):
        stores = short_staff_days.get(day, set())
        order = list(SHORT_STAFF_STORE_LABELS)
        labels = []
        for store in sorted(
            stores,
            key=lambda s: order.index(s) if s in order else len(order),
        ):
            labels.append(SHORT_STAFF_STORE_LABELS.get(store, getattr(store, "value", str(store))))
        return " ".join(labels)
    return "△" if day in (short_staff_days or []) else ""


def _fit_font_size(text: str, font_name: str, size: float, max_width: float, max_height: float) -> float:
    """セル内に文字が収まるよう、フォントサイズだけを控えめに縮める。"""
    text = str(text or "")
    if not text:
        return size
    fitted = size
    while fitted > 3.5:
        try:
            text_width = pdfmetrics.stringWidth(text, font_name, fitted)
        except Exception:
            text_width = len(text) * fitted
        if text_width <= max_width * 0.92 and fitted <= max_height * 0.72:
            return fitted
        fitted -= 0.5
    return fitted


def _draw_text(
    c: canvas.Canvas,
    text: object,
    x: float,
    y: float,
    w: float,
    h: float,
    font_name: str,
    font_size: float,
    align: str = "center",
    bold: bool = False,
) -> None:
    text = "" if text is None else str(text)
    if not text:
        return
    size = _fit_font_size(text, font_name, font_size, w, h)
    c.setFont(font_name, size)
    text_y = y + (h - size) / 2 + size * 0.18
    if align == "left":
        c.drawString(x + max(2.0, size * 0.35), text_y, text)
    else:
        c.drawCentredString(x + w / 2, text_y, text)


def _draw_cell(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    text: object = "",
    font_name: str = "Helvetica",
    font_size: float = 9,
    align: str = "center",
    fill_color=colors.white,
    stroke_color=colors.black,
    line_width: float = 0.35,
) -> None:
    c.setFillColor(fill_color)
    c.setStrokeColor(stroke_color)
    c.setLineWidth(line_width)
    c.rect(x, y, w, h, stroke=1, fill=1)
    c.setFillColor(colors.black)
    _draw_text(c, text, x, y, w, h, font_name, font_size, align=align)


def export_shift_to_pdf(
    shift: MonthlyShift,
    output_path,  # str or Path
    title: Optional[str] = None,
    header_notes: Optional[list[str]] = None,
    footer_notes: Optional[list[str]] = None,
    short_staff_days: Optional[object] = None,
) -> Path:
    """
    シフトを Excel 印刷イメージに合わせた A4 縦 1ページ PDF に出力する。
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    short_staff_days = short_staff_days or []
    days_in_month = monthrange(shift.year, shift.month)[1]
    title = title or f"{shift.year}年{shift.month}月の目標とシフト表  決定版"
    header_notes = list(header_notes or ["", "", ""])[:3]
    while len(header_notes) < 3:
        header_notes.append("")
    footer_notes = footer_notes or DEFAULT_FOOTER_NOTES

    font_name = _register_japanese_font()
    page_width, page_height = A4
    margin_x = 14.0
    margin_y = 14.0

    columns = ["B", "C"] + [chr(ord("D") + i) for i in range(len(EXPORT_COLUMN_ORDER))] + ["W", "X", "Y"]
    col_widths_base = [COLUMN_WIDTHS[col] * EXCEL_WIDTH_TO_POINTS for col in columns]
    row_heights_base = (
        [ROW_HEIGHTS["title"], ROW_HEIGHTS["comment"], ROW_HEIGHTS["comment"], ROW_HEIGHTS["comment_last"]]
        + [ROW_HEIGHTS["legend"], ROW_HEIGHTS["header"]]
        + [ROW_HEIGHTS["data"]] * days_in_month
        + [ROW_HEIGHTS["spacer"]]
        + [ROW_HEIGHTS["footer"]] * len(footer_notes)
    )

    base_width = sum(col_widths_base)
    base_height = sum(row_heights_base)
    scale = min(
        (page_width - margin_x * 2) / base_width,
        (page_height - margin_y * 2) / base_height,
    )
    table_width = base_width * scale
    table_height = base_height * scale
    start_x = (page_width - table_width) / 2
    start_y = page_height - margin_y

    col_widths = [w * scale for w in col_widths_base]
    row_heights = [h * scale for h in row_heights_base]
    line_width = max(0.25, 0.9 * scale)

    pdf = canvas.Canvas(str(output_path), pagesize=A4)
    pdf.setTitle(title)

    def col_x(index: int) -> float:
        return start_x + sum(col_widths[:index])

    current_y = start_y

    def draw_merged_row(text: str, height: float, font_key: str, align: str = "center") -> None:
        nonlocal current_y
        h = height
        y = current_y - h
        _draw_cell(
            pdf,
            start_x,
            y,
            table_width,
            h,
            text=text,
            font_name=font_name,
            font_size=FONT_SIZES[font_key] * scale,
            align=align,
            line_width=line_width,
        )
        current_y = y

    draw_merged_row(title, row_heights[0], "title")
    draw_merged_row(header_notes[0], row_heights[1], "comment")
    draw_merged_row(header_notes[1], row_heights[2], "comment")
    draw_merged_row(header_notes[2], row_heights[3], "comment")
    legend = f"{shift.year}年{shift.month}月のシフト表　○赤羽　□東口　△大宮　☆西口　◆すずらん"
    draw_merged_row(legend, row_heights[4], "legend")

    # ヘッダー行
    header_h = row_heights[5]
    y = current_y - header_h
    _draw_cell(
        pdf, col_x(0), y, col_widths[0] + col_widths[1], header_h,
        text=f"{shift.month}月", font_name=font_name,
        font_size=FONT_SIZES["header"] * scale, line_width=line_width,
    )
    for i, name in enumerate(EXPORT_COLUMN_ORDER):
        col_idx = 2 + i
        _draw_cell(
            pdf, col_x(col_idx), y, col_widths[col_idx], header_h,
            text=name, font_name=font_name,
            font_size=FONT_SIZES["header"] * scale, line_width=line_width,
        )
    right_date_idx = 2 + len(EXPORT_COLUMN_ORDER)
    _draw_cell(
        pdf,
        col_x(right_date_idx),
        y,
        col_widths[right_date_idx] + col_widths[right_date_idx + 1],
        header_h,
        text=f"{shift.month}月",
        font_name=font_name,
        font_size=FONT_SIZES["header"] * scale,
        line_width=line_width,
    )
    _draw_cell(
        pdf,
        col_x(right_date_idx + 2),
        y,
        col_widths[right_date_idx + 2],
        header_h,
        text="人員少",
        font_name=font_name,
        font_size=FONT_SIZES["header"] * scale,
        line_width=line_width,
    )
    current_y = y

    # データ行
    data_h = ROW_HEIGHTS["data"] * scale
    for day in range(1, days_in_month + 1):
        y = current_y - data_h
        wd = WEEKDAY_JP[date(shift.year, shift.month, day).weekday()]
        _draw_cell(pdf, col_x(0), y, col_widths[0], data_h, day, font_name, FONT_SIZES["cell"] * scale, line_width=line_width)
        _draw_cell(pdf, col_x(1), y, col_widths[1], data_h, wd, font_name, FONT_SIZES["cell"] * scale, line_width=line_width)

        for i, emp_name in enumerate(EXPORT_COLUMN_ORDER):
            col_idx = 2 + i
            assignment = shift.get_assignment(emp_name, day)
            value = assignment.store.value if assignment else ""
            _draw_cell(
                pdf, col_x(col_idx), y, col_widths[col_idx], data_h,
                value, font_name, FONT_SIZES["cell"] * scale, line_width=line_width,
            )

        _draw_cell(
            pdf, col_x(right_date_idx), y, col_widths[right_date_idx], data_h,
            day, font_name, FONT_SIZES["cell"] * scale, line_width=line_width,
        )
        _draw_cell(
            pdf, col_x(right_date_idx + 1), y, col_widths[right_date_idx + 1], data_h,
            wd, font_name, FONT_SIZES["cell"] * scale, line_width=line_width,
        )

        short_text = _short_staff_text(short_staff_days, day)
        _draw_cell(
            pdf,
            col_x(right_date_idx + 2),
            y,
            col_widths[right_date_idx + 2],
            data_h,
            short_text,
            font_name,
            FONT_SIZES["cell"] * scale,
            fill_color=colors.HexColor("#FFF59D") if short_text else colors.white,
            line_width=line_width,
        )
        current_y = y

    # Excelと同じ空白行
    current_y -= ROW_HEIGHTS["spacer"] * scale

    for note in footer_notes:
        draw_merged_row(note, ROW_HEIGHTS["footer"] * scale, "footer", align="left")

    pdf.showPage()
    pdf.save()
    return output_path


if __name__ == "__main__":
    from .generator import generate_shift, determine_operation_modes
    from .may_2026_data import (
        OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER, FLEXIBLE_OFF_REQUESTS,
    )
    from .rules import MAY_2026_HOLIDAY_OVERRIDES

    print("【PDF出力テスト】\n")
    print("[1/2] AI でシフトを生成中...")
    modes = determine_operation_modes(2026, 5)
    shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS, work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER, flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES, operation_modes=modes,
        consec_exceptions=["野澤"], max_consec_override=5,
        time_limit_seconds=120, verbose=False,
    )

    print("[2/2] PDFに書き出し中...")
    output_path = export_shift_to_pdf(
        shift=shift,
        output_path=str(OUTPUT_DIR / "2026年5月_AI生成シフト.pdf"),
        header_notes=[
            "AI 自動生成版（OR-Tools CP-SAT）",
            "5月は全体にお休みを増やしています。GW出勤分お体を癒してください。",
        ],
    )
    print(f"  → 保存先: {output_path}")
    print(f"  → ファイルサイズ: {output_path.stat().st_size:,} bytes")
    print("\n✅ 完了。PDFビューアで確認できます。")
