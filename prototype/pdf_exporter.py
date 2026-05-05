"""
シフト表 PDF 出力（A4 横 1ページに収まる印刷用）
================================================
店舗掲示用に、現状のExcelレイアウトと同じデザインでPDFを生成する。
"""

from __future__ import annotations
from datetime import date
from pathlib import Path
from typing import Optional
from calendar import monthrange

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .models import MonthlyShift, Store
from .paths import OUTPUT_DIR


# 日本語フォント登録（macOS の標準フォント）
JAPANESE_FONT_PATHS = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def _register_japanese_font() -> str:
    """日本語フォントを登録し、フォント名を返す"""
    for path in JAPANESE_FONT_PATHS:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("JapFont", path))
                return "JapFont"
            except Exception:
                continue
    # フォールバック: Helvetica（日本語が表示されない可能性あり）
    return "Helvetica"


# 出力時の従業員列順
EXPORT_COLUMN_ORDER = [
    "山本", "板倉", "今津", "鈴木", "田中", "岩野", "大塚", "南",
    "黒澤", "牧野", "春山", "下地", "大類", "長尾", "野澤", "下田",
    "楯", "土井", "顧問",
]

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

# セルの色マップ
CELL_COLORS = {
    "○": colors.HexColor("#fef3c7"),
    "〇": colors.HexColor("#fef3c7"),
    "□": colors.HexColor("#dbeafe"),
    "△": colors.HexColor("#d1fae5"),
    "☆": colors.HexColor("#fce7f3"),
    "◆": colors.HexColor("#e0e7ff"),
    "×": colors.HexColor("#f3f4f6"),
    "": colors.white,
}

SHORT_STAFF_STORE_LABELS = {
    Store.AKABANE: "○赤羽",
    Store.HIGASHIGUCHI: "□東口",
    Store.OMIYA: "△大宮",
    Store.NISHIGUCHI: "☆西口",
    Store.SUZURAN: "◆すずらん",
}


def export_shift_to_pdf(
    shift: MonthlyShift,
    output_path,  # str or Path
    title: Optional[str] = None,
    header_notes: Optional[list[str]] = None,
    short_staff_days: Optional[object] = None,
) -> Path:
    """
    シフトを A4 横 1ページの PDF に出力する。
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    short_staff_days = short_staff_days or []
    days_in_month = monthrange(shift.year, shift.month)[1]
    font_name = _register_japanese_font()

    if title is None:
        title = f"{shift.year}年{shift.month}月の目標とシフト表  決定版"

    # PDF ドキュメント作成（A4横、余白小さめ）
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=8 * mm,
        rightMargin=8 * mm,
        topMargin=8 * mm,
        bottomMargin=8 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=14,
        alignment=TA_CENTER,
        spaceAfter=4,
    )
    note_style = ParagraphStyle(
        "Note",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=8,
        alignment=TA_LEFT,
        spaceAfter=2,
    )
    legend_style = ParagraphStyle(
        "Legend",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10,
        alignment=TA_CENTER,
        spaceAfter=4,
    )

    story = []
    # タイトル
    story.append(Paragraph(title, title_style))

    # ヘッダー注記
    for note in (header_notes or []):
        story.append(Paragraph(note, note_style))

    # 凡例
    legend = "○赤羽　□東口　△大宮　☆西口　◆すずらん　×休み"
    story.append(Paragraph(legend, legend_style))

    # シフトテーブル
    # ヘッダー行
    header = ["日", "曜"] + EXPORT_COLUMN_ORDER + ["人員少"]
    table_data = [header]

    # 各日のデータ
    cell_styles = []  # (row, col, color) のリスト

    def _short_staff_text(day: int) -> str:
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
        return "△" if day in short_staff_days else ""

    for d in range(1, days_in_month + 1):
        weekday = date(shift.year, shift.month, d).weekday()
        wd = WEEKDAY_JP[weekday]
        row_data = [str(d), wd]

        for emp_name in EXPORT_COLUMN_ORDER:
            a = shift.get_assignment(emp_name, d)
            value = a.store.value if a else ""
            row_data.append(value)
            color = CELL_COLORS.get(value, colors.white)
            if color != colors.white:
                col_idx = 2 + EXPORT_COLUMN_ORDER.index(emp_name)
                cell_styles.append((d, col_idx, color))

        # 人員少マーク
        short_mark = _short_staff_text(d)
        row_data.append(short_mark)
        if short_mark:
            cell_styles.append((d, 2 + len(EXPORT_COLUMN_ORDER), colors.HexColor("#fff3cd")))

        table_data.append(row_data)

    # テーブル列幅
    n_cols = len(header)
    # ページ幅約 280mm, 余白考慮で 270mm 利用可
    available_width = 270 * mm
    day_col_w = 8 * mm
    weekday_col_w = 8 * mm
    short_col_w = 28 * mm
    emp_col_w = (available_width - day_col_w - weekday_col_w - short_col_w) / len(EXPORT_COLUMN_ORDER)
    col_widths = [day_col_w, weekday_col_w] + [emp_col_w] * len(EXPORT_COLUMN_ORDER) + [short_col_w]

    # 行高
    row_height = 6.5 * mm
    row_heights = [row_height * 1.5] + [row_height] * days_in_month

    table = Table(table_data, colWidths=col_widths, rowHeights=row_heights)

    # スタイル設定
    table_style = [
        # 全体
        ("FONT", (0, 0), (-1, -1), font_name, 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        # ヘッダー
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 0), (-1, 0), font_name, 9),
    ]
    # 土日の背景
    for d in range(1, days_in_month + 1):
        weekday = date(shift.year, shift.month, d).weekday()
        if weekday == 5:  # 土
            table_style.append(("BACKGROUND", (0, d), (1, d), colors.HexColor("#dbeafe")))
        elif weekday == 6:  # 日
            table_style.append(("BACKGROUND", (0, d), (1, d), colors.HexColor("#fee2e2")))

    # セル個別色
    for row, col, color in cell_styles:
        table_style.append(("BACKGROUND", (col, row), (col, row), color))

    table.setStyle(TableStyle(table_style))
    story.append(table)
    story.append(Spacer(1, 4 * mm))

    # 末尾の注記
    footer_notes = [
        "※25日までに翌月のお休み又は出勤希望日を、ご連絡ください。",
        "※出勤基準日数（の目安）と違いがある場合は、希望するお休み日数と消化する有給休暇日数もお願いします。",
    ]
    for n in footer_notes:
        story.append(Paragraph(n, note_style))

    doc.build(story)
    return output_path


# ============================================================
# 動作テスト
# ============================================================

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
