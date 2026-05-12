"""
ルール設定の永続化・履歴管理
================================================
シフト検証ルールを JSON で管理し、UIから編集・履歴記録できるようにする。

設計方針:
- デフォルトルール（rules.py のハードコード値）は初期値として使う
- ユーザーがUIで変更すると config.json に保存される
- 変更履歴は append-only JSONL に記録（誰がいつ何を変更したか）
- validator/generator は load_active_config() で現在の設定を取得

ファイル構造:
  /Users/kinoshitayoshihide/daikokuya-shift/config/
    rule_config.json        ← 現在のアクティブ設定
    rule_history.jsonl      ← 変更履歴（追記専用）
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

from .paths import CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / "rule_config.json"
HISTORY_FILE = CONFIG_DIR / "rule_history.jsonl"


# ============================================================
# デフォルト設定
# ============================================================

# 各検証チェックの ON/OFF（カテゴリ単位）
DEFAULT_ENABLED_CHECKS = {
    "store_capacity": True,         # 店舗別必要人数
    "eco_required": True,           # 東口・西口の必須エコ
    "consec_work": True,            # 連勤チェック
    "holiday_days": True,           # 月内最低休日数
    "consec_off_3": True,           # 3連休禁止
    "two_off_per_month": True,      # 2連休回数チェック
    "off_request": True,            # 休み希望厳守
    "work_request": True,           # 出勤希望厳守
    "omiya_anchor": True,           # 大宮アンカー（春山 or 下地）
    "higashi_monday": True,         # 東口月曜休店
    "omiya_short_warning": True,    # 大宮人数少警告
}

# 数値パラメータ
DEFAULT_PARAMETERS = {
    "max_consec_work": 5,                # 最大連勤日数（ハード）
    "soft_consec_threshold": 4,          # 連勤超ペナルティの閾値（ソフト）
    "default_holiday_days": 8,           # 月内最低休日数（既定）
    "min_2off_per_month": 1,             # 2連休 月内最低回数
    "max_2off_per_month": 2,             # 2連休 月内最大回数
    "higashi_eco2_max_per_month": 3,     # 東口エコ2配置の月内最大回数
    "solver_seed": 42,                   # ソルバーシード（再現性用）
    "solver_time_limit_seconds": 120,    # ソルバーの最大実行時間
}


# ============================================================
# データクラス
# ============================================================

@dataclass
class CustomRule:
    """ユーザーが追加したカスタムルール（自然言語記述）"""
    id: str
    name: str
    description: str
    enabled: bool = True
    severity: str = "WARNING"  # "ERROR" or "WARNING"
    created_at: str = ""
    created_by: str = ""
    target_year: Optional[int] = None
    target_month: Optional[int] = None
    rule_type: str = "note"  # note / employee_store_count
    employee: str = ""
    stores: list[str] = field(default_factory=list)
    count: int = 0


@dataclass
class RuleConfig:
    """ルール設定全体"""
    version: int = 1
    updated_at: str = ""
    updated_by: str = ""
    enabled_checks: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_ENABLED_CHECKS))
    parameters: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PARAMETERS))
    custom_rules: list[CustomRule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "enabled_checks": self.enabled_checks,
            "parameters": self.parameters,
            "custom_rules": [asdict(r) for r in self.custom_rules],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RuleConfig":
        return cls(
            version=data.get("version", 1),
            updated_at=data.get("updated_at", ""),
            updated_by=data.get("updated_by", ""),
            enabled_checks={
                **DEFAULT_ENABLED_CHECKS,
                **data.get("enabled_checks", {}),
            },
            parameters={
                **DEFAULT_PARAMETERS,
                **data.get("parameters", {}),
            },
            custom_rules=[
                CustomRule(**r) for r in data.get("custom_rules", [])
            ],
        )


@dataclass
class ChangeLog:
    """設定変更履歴 1件"""
    timestamp: str
    actor: str
    category: str   # "enabled_check" / "parameter" / "custom_rule_add" / "custom_rule_remove"
    target: str     # 変更対象のキー名
    before: Any
    after: Any
    note: str = ""


# ============================================================
# 永続化マネージャ
# ============================================================

class RuleConfigManager:
    """ルール設定の読み書き・履歴管理"""

    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "rule_config.json"
        self.history_file = self.config_dir / "rule_history.jsonl"

    # ============================================================
    # 読み込み
    # ============================================================

    def load(self) -> RuleConfig:
        """現在のアクティブ設定を読み込む（無ければデフォルト）"""
        if not self.config_file.exists():
            return RuleConfig()
        with open(self.config_file, encoding="utf-8") as f:
            data = json.load(f)
        return RuleConfig.from_dict(data)

    # ============================================================
    # 書き込み（差分を履歴に記録）
    # ============================================================

    def save(self, new_config: RuleConfig, actor: str, note: str = "") -> list[ChangeLog]:
        """設定を保存し、差分を履歴に記録する"""
        old_config = self.load()
        changes = self._compute_diff(old_config, new_config, actor, note)
        # 差分があれば履歴記録
        if changes:
            with open(self.history_file, "a", encoding="utf-8") as f:
                for ch in changes:
                    f.write(json.dumps(asdict(ch), ensure_ascii=False) + "\n")
        # 設定を更新
        new_config.updated_at = datetime.now().isoformat()
        new_config.updated_by = actor
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(new_config.to_dict(), f, ensure_ascii=False, indent=2)
        return changes

    def _compute_diff(
        self, old: RuleConfig, new: RuleConfig, actor: str, note: str
    ) -> list[ChangeLog]:
        """新旧設定の差分を計算"""
        ts = datetime.now().isoformat()
        changes = []
        # ON/OFF切替
        for key in set(old.enabled_checks) | set(new.enabled_checks):
            ov = old.enabled_checks.get(key)
            nv = new.enabled_checks.get(key)
            if ov != nv:
                changes.append(ChangeLog(
                    timestamp=ts, actor=actor,
                    category="enabled_check", target=key,
                    before=ov, after=nv, note=note,
                ))
        # パラメータ
        for key in set(old.parameters) | set(new.parameters):
            ov = old.parameters.get(key)
            nv = new.parameters.get(key)
            if ov != nv:
                changes.append(ChangeLog(
                    timestamp=ts, actor=actor,
                    category="parameter", target=key,
                    before=ov, after=nv, note=note,
                ))
        # カスタムルール（追加・削除）
        old_ids = {r.id for r in old.custom_rules}
        new_ids = {r.id for r in new.custom_rules}
        for added_id in new_ids - old_ids:
            r = next(r for r in new.custom_rules if r.id == added_id)
            changes.append(ChangeLog(
                timestamp=ts, actor=actor,
                category="custom_rule_add", target=r.id,
                before=None, after=asdict(r), note=note,
            ))
        for removed_id in old_ids - new_ids:
            r = next(r for r in old.custom_rules if r.id == removed_id)
            changes.append(ChangeLog(
                timestamp=ts, actor=actor,
                category="custom_rule_remove", target=r.id,
                before=asdict(r), after=None, note=note,
            ))
        # カスタムルールの ON/OFF 切替
        for nr in new.custom_rules:
            old_r = next((r for r in old.custom_rules if r.id == nr.id), None)
            if old_r and old_r.enabled != nr.enabled:
                changes.append(ChangeLog(
                    timestamp=ts, actor=actor,
                    category="custom_rule_toggle", target=nr.id,
                    before=old_r.enabled, after=nr.enabled, note=note,
                ))
        return changes

    # ============================================================
    # 履歴
    # ============================================================

    def get_history(self, limit: int = 100) -> list[ChangeLog]:
        """変更履歴を取得（新しい順）"""
        if not self.history_file.exists():
            return []
        history = []
        with open(self.history_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    history.append(ChangeLog(**json.loads(line)))
        return list(reversed(history))[:limit]

    # ============================================================
    # ユーティリティ
    # ============================================================

    def reset_to_default(self, actor: str) -> list[ChangeLog]:
        """デフォルトに戻す"""
        return self.save(RuleConfig(), actor, note="デフォルトにリセット")


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("【ルール設定マネージャ 動作テスト】\n")

    mgr = RuleConfigManager()
    print(f"設定ディレクトリ: {mgr.config_dir}\n")

    # 初期読み込み
    cfg = mgr.load()
    print(f"[1] 初期設定:")
    print(f"  最大連勤: {cfg.parameters['max_consec_work']}")
    print(f"  既定休日数: {cfg.parameters['default_holiday_days']}")
    print(f"  カスタムルール数: {len(cfg.custom_rules)}")

    # 設定変更
    print(f"\n[2] 最大連勤を 5 → 6 に変更...")
    cfg.parameters["max_consec_work"] = 6
    changes = mgr.save(cfg, actor="代表取締役", note="動作テスト")
    print(f"  → {len(changes)}件の変更を記録")
    for ch in changes:
        print(f"    {ch.target}: {ch.before} → {ch.after}")

    # カスタムルール追加
    print(f"\n[3] カスタムルール追加...")
    cfg2 = mgr.load()
    cfg2.custom_rules.append(CustomRule(
        id="custom_test_1",
        name="楯さんは毎週日曜休み希望優先",
        description="楯さんは家族の事情で日曜は休み優先で組む",
        enabled=True,
        severity="WARNING",
        created_at=datetime.now().isoformat(),
        created_by="代表取締役",
    ))
    mgr.save(cfg2, actor="代表取締役", note="日曜休み希望ルール追加")

    # 履歴確認
    print(f"\n[4] 変更履歴:")
    for ch in mgr.get_history():
        if ch.category == "custom_rule_add":
            print(f"  {ch.timestamp[:19]} {ch.actor} がルール追加: {ch.target}")
        else:
            print(f"  {ch.timestamp[:19]} {ch.actor} が {ch.target}: {ch.before} → {ch.after}")

    # リセット
    print(f"\n[5] デフォルトにリセット...")
    mgr.reset_to_default(actor="代表取締役")
    cfg3 = mgr.load()
    print(f"  最大連勤: {cfg3.parameters['max_consec_work']} (元の5に戻ったはず)")

    print("\n✅ ルール設定マネージャ 動作確認完了")
