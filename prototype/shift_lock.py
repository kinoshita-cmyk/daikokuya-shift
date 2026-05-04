"""
シフトロック管理
================================================
確定したシフトを「ロック」して、誤って再生成・上書きされないようにする。

仕組み:
- locks/ ディレクトリにロック情報を保存
- ロック中はシフト再生成を拒否
- 確定版シフトは backups/ に永続保存され、いつでも読み込める
- ロックを外せば再編集可能

ファイル構造:
  /Users/kinoshitayoshihide/daikokuya-shift/locks/
    YYYY-MM.lock       ← ロック情報（JSON）
"""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .paths import LOCK_DIR as DEFAULT_LOCK_DIR


@dataclass
class LockInfo:
    """ロック情報"""
    year: int
    month: int
    locked_at: str          # ISO8601
    locked_by: str
    note: str               # メモ（例: "5月分 確定版（顧問承認済み）"）
    snapshot_file: str      # backups内の保存ファイル名


class ShiftLockManager:
    """シフトのロック状態を管理する"""

    def __init__(self, lock_dir: Optional[Path] = None):
        self.lock_dir = lock_dir or DEFAULT_LOCK_DIR
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def _lock_path(self, year: int, month: int) -> Path:
        return self.lock_dir / f"{year}-{month:02d}.lock"

    # ============================================================
    # ロック・解除
    # ============================================================

    def lock(
        self,
        year: int, month: int,
        locked_by: str,
        snapshot_file: str,
        note: str = "",
    ) -> Path:
        """シフトをロックする"""
        info = LockInfo(
            year=year, month=month,
            locked_at=datetime.now().isoformat(),
            locked_by=locked_by,
            note=note,
            snapshot_file=snapshot_file,
        )
        path = self._lock_path(year, month)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(info), f, ensure_ascii=False, indent=2)
        return path

    def unlock(self, year: int, month: int) -> bool:
        """ロックを外す（履歴は残らないが、バックアップ自体は残る）"""
        path = self._lock_path(year, month)
        if not path.exists():
            return False
        # 削除前に履歴アーカイブとして残す
        archive_dir = self.lock_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        archive_path = archive_dir / f"{year}-{month:02d}_unlocked_{ts}.json"
        path.rename(archive_path)
        return True

    # ============================================================
    # 確認
    # ============================================================

    def is_locked(self, year: int, month: int) -> bool:
        """ロックされているか確認"""
        return self._lock_path(year, month).exists()

    def get_lock_info(self, year: int, month: int) -> Optional[LockInfo]:
        """ロック情報を取得"""
        path = self._lock_path(year, month)
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return LockInfo(**data)

    def list_locks(self) -> list[LockInfo]:
        """全てのロック情報を一覧"""
        result = []
        for path in sorted(self.lock_dir.glob("*.lock")):
            with open(path, encoding="utf-8") as f:
                result.append(LockInfo(**json.load(f)))
        return result


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("【シフトロック機構の動作テスト】\n")

    mgr = ShiftLockManager()
    print(f"ロックディレクトリ: {mgr.lock_dir}\n")

    # 5月をロック
    print("[1] 2026年5月をロック...")
    path = mgr.lock(
        year=2026, month=5,
        locked_by="代表取締役",
        snapshot_file="shift_finalized_2026-05-03_120000.json",
        note="2026年5月分 確定版（顧問承認済み）",
    )
    print(f"  → {path}")

    # 確認
    print("\n[2] ロック状態を確認...")
    print(f"  is_locked(2026, 5): {mgr.is_locked(2026, 5)}")
    print(f"  is_locked(2026, 6): {mgr.is_locked(2026, 6)}")

    info = mgr.get_lock_info(2026, 5)
    if info:
        print(f"\n[3] ロック情報:")
        print(f"  年月: {info.year}年{info.month}月")
        print(f"  ロック日時: {info.locked_at}")
        print(f"  実行者: {info.locked_by}")
        print(f"  メモ: {info.note}")

    # 一覧
    print("\n[4] ロック一覧:")
    for lock in mgr.list_locks():
        print(f"  - {lock.year}年{lock.month}月: {lock.note}")

    # 解除
    print("\n[5] ロック解除...")
    print(f"  unlock 結果: {mgr.unlock(2026, 5)}")
    print(f"  解除後 is_locked: {mgr.is_locked(2026, 5)}")

    print("\n✅ ロック機構 動作確認完了")
