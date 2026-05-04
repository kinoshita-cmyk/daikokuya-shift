"""
シフトデータ・希望データのバックアップ機構
================================================
運用開始後、データ消失を防ぐため、以下を自動でスナップショット化する：
- 従業員の希望提出データ（毎月）
- 確定したシフト（毎月）
- 編集履歴（誰がいつ何を変更したか）

設計方針：
- ローカルファイル + クラウドDB の二重バックアップ
- スナップショット: JSON形式でタイムスタンプ付きファイルに保存
- 履歴: 削除はせず、論理削除フラグのみ立てる

ファイル構成：
  backups/
    YYYY-MM/
      preferences_YYYY-MM-DD_HHMMSS.json    ← 希望データのスナップショット
      shift_finalized_YYYY-MM-DD_HHMMSS.json ← 確定シフト
      edits_YYYY-MM-DD.jsonl                 ← 編集履歴（追記専用）
"""

from __future__ import annotations
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import (
    MonthlyShift, ShiftAssignment, Store, OperationMode,
    DayPreference, PreferenceMark,
)
from .paths import BACKUP_DIR as DEFAULT_BACKUP_DIR


class ShiftBackup:
    """シフト関連データのバックアップを管理する"""

    def __init__(self, backup_dir: Optional[Path] = None):
        self.backup_dir = backup_dir or DEFAULT_BACKUP_DIR
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _get_month_dir(self, year: int, month: int) -> Path:
        d = self.backup_dir / f"{year}-{month:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d_%H%M%S")

    # ============================================================
    # シフトのバックアップ
    # ============================================================

    def save_shift(
        self, shift: MonthlyShift,
        kind: str = "draft",  # "draft" / "finalized"
        author: str = "system",
        note: str = "",
    ) -> Path:
        """シフトをスナップショット化"""
        month_dir = self._get_month_dir(shift.year, shift.month)
        ts = self._timestamp()
        file_path = month_dir / f"shift_{kind}_{ts}.json"

        data = {
            "year": shift.year,
            "month": shift.month,
            "kind": kind,
            "author": author,
            "note": note,
            "saved_at": datetime.now().isoformat(),
            "operation_modes": {
                str(d): m.value for d, m in shift.operation_modes.items()
            },
            "assignments": [
                {
                    "employee": a.employee,
                    "day": a.day,
                    "store": a.store.name,
                    "is_paid_leave": a.is_paid_leave,
                }
                for a in shift.assignments
            ],
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return file_path

    def load_shift(self, file_path: Path) -> MonthlyShift:
        """スナップショットからシフトを復元"""
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        shift = MonthlyShift(year=data["year"], month=data["month"])
        shift.operation_modes = {
            int(d): OperationMode(m) for d, m in data["operation_modes"].items()
        }
        shift.assignments = [
            ShiftAssignment(
                employee=a["employee"],
                day=a["day"],
                store=Store[a["store"]],
                is_paid_leave=a.get("is_paid_leave", False),
            )
            for a in data["assignments"]
        ]
        return shift

    def list_shifts(self, year: int, month: int) -> list[Path]:
        """その月のシフトスナップショット一覧（時系列順）"""
        month_dir = self._get_month_dir(year, month)
        return sorted(month_dir.glob("shift_*.json"))

    def get_latest_shift(
        self, year: int, month: int, kind: Optional[str] = None
    ) -> Optional[MonthlyShift]:
        """最新のシフトを取得（kind 指定可）"""
        files = self.list_shifts(year, month)
        if kind:
            files = [f for f in files if f.name.startswith(f"shift_{kind}_")]
        if not files:
            return None
        return self.load_shift(files[-1])

    # ============================================================
    # 希望データのバックアップ
    # ============================================================

    def save_preferences(
        self, year: int, month: int,
        off_requests: dict[str, list[int]],
        work_requests: list[tuple],
        flexible_off: list[tuple],
        natural_language_notes: dict[str, str],
        author: str = "system",
    ) -> Path:
        """希望データをスナップショット化"""
        month_dir = self._get_month_dir(year, month)
        ts = self._timestamp()
        file_path = month_dir / f"preferences_{ts}.json"

        data = {
            "year": year,
            "month": month,
            "saved_at": datetime.now().isoformat(),
            "author": author,
            "off_requests": off_requests,
            "work_requests": [
                {"employee": n, "day": d, "store": s.name if s else None}
                for (n, d, s) in work_requests
            ],
            "flexible_off": [
                {"employee": n, "candidate_days": dd, "n_required": nn}
                for (n, dd, nn) in flexible_off
            ],
            "natural_language_notes": natural_language_notes,
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return file_path

    def load_preferences(self, file_path: Path) -> dict:
        """希望データを復元"""
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)

    # ============================================================
    # 編集履歴の記録（追記専用）
    # ============================================================

    def log_edit(
        self,
        year: int, month: int,
        employee: str, day: int,
        before_store: str, after_store: str,
        actor: str,
        reason: str = "",
    ) -> None:
        """シフト編集を1件記録（JSONL形式で追記）"""
        month_dir = self._get_month_dir(year, month)
        log_file = month_dir / f"edits_{datetime.now().strftime('%Y-%m-%d')}.jsonl"

        entry = {
            "timestamp": datetime.now().isoformat(),
            "actor": actor,
            "employee": employee,
            "day": day,
            "before": before_store,
            "after": after_store,
            "reason": reason,
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_edit_history(
        self, year: int, month: int, day_filter: Optional[int] = None
    ) -> list[dict]:
        """編集履歴を取得"""
        month_dir = self._get_month_dir(year, month)
        history = []
        for log_file in sorted(month_dir.glob("edits_*.jsonl")):
            with open(log_file, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        entry = json.loads(line)
                        if day_filter is None or entry["day"] == day_filter:
                            history.append(entry)
        return history

    # ============================================================
    # 希望シフト提出状況の取得
    # ============================================================

    def get_submission_status(
        self, year: int, month: int, expected_employees: list[str],
    ) -> dict:
        """
        指定月の希望提出状況を取得する。

        Args:
            year: 対象年
            month: 対象月
            expected_employees: 提出が期待される従業員の名前リスト

        Returns:
            {
              "submitted": [{"employee": ..., "submitted_at": ..., "file": ...}, ...],
              "not_submitted": ["田中", "大塚", ...],
              "summary": {
                "total_expected": 18,
                "total_submitted": 14,
                "total_pending": 4,
                "completion_rate": 0.78
              }
            }
        """
        month_dir = self._get_month_dir(year, month)
        submission_map: dict[str, dict] = {}  # 従業員名 → 最新の提出情報

        # 提出ファイルを走査して、各従業員の最新提出を取得
        for file in sorted(month_dir.glob("preferences_*.json")):
            try:
                with open(file, encoding="utf-8") as f:
                    data = json.load(f)
                author = data.get("author", "")
                if not author or author == "system":
                    continue
                saved_at = data.get("saved_at", "")
                # 最新のものを保持
                if (author not in submission_map
                        or saved_at > submission_map[author].get("submitted_at", "")):
                    # off_requestsの中身を集計（提出内容のサマリー）
                    off_count = sum(
                        len(v) for v in data.get("off_requests", {}).values()
                    )
                    flex_count = len(data.get("flexible_off", []))
                    note_text = ""
                    notes = data.get("natural_language_notes", {})
                    if isinstance(notes, dict):
                        note_text = notes.get(author, "")
                    submission_map[author] = {
                        "employee": author,
                        "submitted_at": saved_at,
                        "file": file.name,
                        "off_request_count": off_count,
                        "flexible_off_count": flex_count,
                        "has_note": bool(note_text and note_text.strip()),
                        "note_excerpt": note_text[:50] + ("..." if len(note_text) > 50 else ""),
                    }
            except (json.JSONDecodeError, OSError):
                continue

        # 提出済み・未提出の振り分け
        submitted_list = list(submission_map.values())
        submitted_list.sort(key=lambda x: x.get("submitted_at", ""), reverse=True)
        submitted_names = set(submission_map.keys())
        not_submitted = [
            name for name in expected_employees if name not in submitted_names
        ]

        total_expected = len(expected_employees)
        total_submitted = sum(1 for n in expected_employees if n in submitted_names)
        return {
            "submitted": submitted_list,
            "not_submitted": not_submitted,
            "summary": {
                "total_expected": total_expected,
                "total_submitted": total_submitted,
                "total_pending": total_expected - total_submitted,
                "completion_rate": (
                    total_submitted / total_expected if total_expected > 0 else 0
                ),
            },
        }


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("【バックアップ機構の動作テスト】\n")

    backup = ShiftBackup()
    print(f"バックアップ先: {backup.backup_dir}\n")

    # Excel から読み込んだシフトを保存してみる
    from .excel_loader import load_shift_from_excel

    print("[1/3] Excelからシフトを読み込み...")
    from .paths import MAY_2026_SHIFT_XLSX
    shift, _ = load_shift_from_excel(str(MAY_2026_SHIFT_XLSX))

    print("[2/3] バックアップを保存...")
    saved_path = backup.save_shift(
        shift, kind="finalized", author="顧問（手動作成）",
        note="2026年5月の確定版シフト（Excelから取り込み）"
    )
    print(f"  → 保存先: {saved_path}")
    print(f"  → ファイルサイズ: {saved_path.stat().st_size:,} bytes")

    print("\n[3/3] 保存したファイルから復元できるか確認...")
    restored = backup.load_shift(saved_path)
    print(f"  → アサインメント数: {len(restored.assignments)} 件")
    print(f"  → 5/1 の配置:")
    for a in restored.get_day_assignments(1)[:5]:
        print(f"      {a.employee:6s} → {a.store.display_name}")
    print("  ...")

    # 編集履歴のテスト
    print("\n[追加] 編集履歴の記録テスト...")
    backup.log_edit(
        year=2026, month=5, employee="田中", day=6,
        before_store="休み", after_store="赤羽駅前店",
        actor="代表取締役", reason="出勤希望に合わせて修正",
    )
    history = backup.get_edit_history(2026, 5)
    print(f"  → 記録された編集: {len(history)} 件")
    for h in history:
        print(f"      {h['timestamp']}: {h['employee']} 5/{h['day']}日 "
              f"{h['before']} → {h['after']}（{h['actor']}: {h['reason']}）")

    print("\n✅ バックアップ機構 動作確認完了")
