"""
解なし（INFEASIBLE）の原因自動診断
================================================
シフト自動生成が「解なし」になったとき、原因を人間が推理しなくても
済むように、2段階で自動診断する。

1. diagnose_known_conflicts():
   過去に実際へ起きた衝突パターンを、ソルバーを使わず即座に検査する。
   - 強制出勤と強制休みが同じ人・同じ日に重なる（2026-08 障害の型）
   - 休店日への強制配置（2026-08 障害の型）
   - 休日数の指定と×休みの矛盾
   - 連勤上限から見て出勤日数が物理的に足りない
   - 日別の人員下限に対する出勤可能者数の不足（概算）

2. probe_rule_relaxations():
   ルール群を1つずつ外して短時間の再生成を試し、
   「どの条件を外すと解が出るか」を機械的に特定する。
   （これまで人間＋AIが手作業でやっていた総当たり調査の自動化）

このモジュールは読み取り専用で、既存データ・設定は変更しない。
"""

from __future__ import annotations
import inspect
from calendar import monthrange
from datetime import date
from typing import Any, Callable, Optional

from .models import OperationMode, Skill, Store
from .rules import (
    MANDATORY_WORK_ON_REQUEST_EMPLOYEES,
    compute_prev_consecutive_run,
    get_capacity,
    is_store_open_on_day,
    month_edge_forced_assignments,
    monthly_carryover_consecutive_allowances,
)


# ============================================================
# 1. 既知パターンの即時検査（ソルバー不使用）
# ============================================================

def _max_workdays_with_consec_limit(days_in_month: int, max_consec: int) -> int:
    """連勤上限の下で、1ヶ月に物理的に可能な最大出勤日数。"""
    if max_consec <= 0:
        return 0
    cycle = max_consec + 1  # max_consec 連勤 + 1休み
    full_cycles = days_in_month // cycle
    remainder = days_in_month % cycle
    return full_cycles * max_consec + min(remainder, max_consec)


def diagnose_known_conflicts(
    year: int,
    month: int,
    off_requests: Optional[dict] = None,
    work_requests: Optional[list] = None,
    required_assignments: Optional[list] = None,
    prev_month: Optional[list] = None,
    consec_exceptions: Optional[list] = None,
    max_consec: int = 5,
    employee_max_consecutive_work: Optional[dict] = None,
    exact_holiday_days: Optional[dict] = None,
    holiday_overrides: Optional[dict] = None,
    operation_modes: Optional[dict] = None,
    employees: Optional[list] = None,
) -> list[dict]:
    """既知の衝突パターンを即座に検査し、日本語の所見リストを返す。

    Returns:
        list[dict]: 各所見は {"区分", "確度", "内容", "対処"} を持つ。
        確度は「確実」（論理矛盾）か「可能性」（概算による疑い）。
    """
    off_requests = off_requests or {}
    work_requests = work_requests or []
    required_assignments = required_assignments or []
    consec_exceptions = list(consec_exceptions or [])
    emp_max = employee_max_consecutive_work or {}
    exact_holiday_days = exact_holiday_days or {}
    holiday_overrides = holiday_overrides or {}
    operation_modes = operation_modes or {}

    days_in_month = monthrange(int(year), int(month))[1]
    findings: list[dict] = []

    prev_consec_map = compute_prev_consecutive_run(prev_month, year, month)
    allowances = monthly_carryover_consecutive_allowances(year, month)

    def _personal_limit(name: str) -> int:
        try:
            personal = int(emp_max.get(name, max_consec))
        except (TypeError, ValueError):
            personal = int(max_consec)
        return min(int(max_consec), personal)

    # ---- 強制休みの集合を作る ----------------------------------
    # forced_rest[(name, day)] = 理由文字列
    forced_rest: dict[tuple, str] = {}

    # ×休み希望
    for name, days in off_requests.items():
        for d in days or []:
            if 1 <= int(d) <= days_in_month:
                forced_rest[(str(name), int(d))] = "本人の×休み希望"

    # 前月持ち越しによる月初の強制休み
    for name, prev in prev_consec_map.items():
        if name in consec_exceptions:
            continue
        limit = _personal_limit(name) + int(allowances.get(name, 0))
        allowed_work = limit - int(prev)
        if allowed_work <= 0:
            forced_rest[(str(name), 1)] = (
                f"前月末から{int(prev)}連勤のため月初1日は休み必須"
                f"（連勤上限{limit}日）"
            )

    # 全店休業日
    closed_days = [
        d for d in range(1, days_in_month + 1)
        if operation_modes.get(d) == OperationMode.CLOSED
    ]
    # 出勤希望専任スタッフ（南さん型）の「希望していない日」
    on_request_only_workdays: dict = {}
    for name, d, _store in work_requests:
        if str(name) in MANDATORY_WORK_ON_REQUEST_EMPLOYEES:
            on_request_only_workdays.setdefault(str(name), set()).add(int(d))

    # ---- 強制出勤の一覧を作り、強制休みとの衝突を探す ----------
    # (name, day, store_or_none, 理由)
    forced_work: list[tuple] = []

    for name, d, store in work_requests:
        if 1 <= int(d) <= days_in_month:
            forced_work.append(
                (str(name), int(d), store, "出勤希望（絶対）")
            )

    for rule in required_assignments:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("severity") or "ERROR").upper() != "ERROR":
            continue
        name = str(rule.get("employee") or "")
        try:
            d = int(rule.get("day") or 0)
        except (TypeError, ValueError):
            continue
        if name and 1 <= d <= days_in_month:
            forced_work.append(
                (name, d, rule.get("store"), "月別の日付指定配置（絶対）")
            )

    edge_forced, edge_notes = month_edge_forced_assignments(
        year=year,
        month=month,
        days_in_month=days_in_month,
        off_requests=off_requests,
        operation_modes=operation_modes,
        prev_consec_map=prev_consec_map,
        hard_max_consec=max_consec,
        employee_max_consecutive_work=emp_max,
        consec_exceptions=consec_exceptions,
    )
    for name, d, store in edge_forced:
        forced_work.append((name, d, store, "月末月初の固定配置"))

    for note in edge_notes:
        findings.append({
            "区分": "自動免除",
            "確度": "情報",
            "内容": note,
            "対処": "免除のまま生成する場合は対応不要です。",
        })

    for name, d, store, reason in forced_work:
        rest_reason = forced_rest.get((name, d))
        if rest_reason:
            findings.append({
                "区分": "強制出勤と強制休みの衝突",
                "確度": "確実",
                "内容": (
                    f"{name}さんの{int(month)}月{d}日: "
                    f"「{reason}」と「{rest_reason}」が同時に成立できません。"
                ),
                "対処": (
                    "どちらを優先するか決めてください。"
                    "出勤を優先する場合は「⚙️ 設定 → 📅 月例外」で"
                    "境界連勤の延長を許可し、"
                    "休みを優先する場合は該当の出勤条件を取り下げてください。"
                ),
            })
        if d in closed_days:
            findings.append({
                "区分": "全店休業日への強制出勤",
                "確度": "確実",
                "内容": (
                    f"{name}さんの{int(month)}月{d}日: "
                    f"全店休業日に「{reason}」が指定されています。"
                ),
                "対処": "該当日の出勤指定を取り下げてください。",
            })
        # 休店日の店舗への強制配置
        if store is not None and isinstance(store, Store) and d not in closed_days:
            mode = operation_modes.get(d, OperationMode.NORMAL)
            if not is_store_open_on_day(year, month, d, store, mode):
                findings.append({
                    "区分": "休店日への強制配置",
                    "確度": "確実",
                    "内容": (
                        f"{name}さんの{int(month)}月{d}日: "
                        f"{store.display_name}は休店日ですが"
                        f"「{reason}」で配置指定されています。"
                    ),
                    "対処": "配置先か日付を変更してください。",
                })
        # 出勤希望専任スタッフの希望外の日への配置指定
        if (
            name in on_request_only_workdays
            and reason != "出勤希望（絶対）"
            and d not in on_request_only_workdays[name]
        ):
            findings.append({
                "区分": "出勤希望専任スタッフへの希望外配置",
                "確度": "確実",
                "内容": (
                    f"{name}さんは出勤希望日のみ勤務ですが、"
                    f"{int(month)}月{d}日に「{reason}」が指定されています。"
                ),
                "対処": "本人に出勤希望の追加を依頼するか、指定を外してください。",
            })

    # ---- 休日数と×休み・連勤上限の矛盾 -------------------------
    forced_work_days_by_name: dict = {}
    for name, d, _store, _reason in forced_work:
        forced_work_days_by_name.setdefault(name, set()).add(d)

    for name, exact_off in exact_holiday_days.items():
        try:
            exact_off = int(exact_off)
        except (TypeError, ValueError):
            continue
        requested_off = {
            int(d) for d in (off_requests.get(name) or [])
            if 1 <= int(d) <= days_in_month
        }
        n_forced_work = len(forced_work_days_by_name.get(str(name), set()))
        # ×休み ＋ 全店休業日は必ず休みになる
        min_off = len(requested_off | set(closed_days))
        if exact_off < min_off:
            findings.append({
                "区分": "休日数指定と×休みの矛盾",
                "確度": "確実",
                "内容": (
                    f"{name}さん: 休日数指定は{exact_off}日ですが、"
                    f"×休み＋全店休業日だけで{min_off}日が必ず休みになります。"
                ),
                "対処": (
                    "休日数の指定（自由記載など）を見直すか、"
                    "×休みの一部を△に変更してもらってください。"
                ),
            })
        max_off = days_in_month - n_forced_work
        if exact_off > max_off:
            findings.append({
                "区分": "休日数指定と出勤指定の矛盾",
                "確度": "確実",
                "内容": (
                    f"{name}さん: 休日数指定は{exact_off}日ですが、"
                    f"出勤指定が{n_forced_work}日あるため"
                    f"最大でも{max_off}日しか休めません。"
                ),
                "対処": "休日数指定か出勤指定のどちらかを調整してください。",
            })
        # 連勤上限から見た最大出勤日数
        required_work = days_in_month - exact_off
        possible_work = _max_workdays_with_consec_limit(
            days_in_month, _personal_limit(str(name)),
        )
        if required_work > possible_work:
            findings.append({
                "区分": "連勤上限と出勤日数の矛盾",
                "確度": "確実",
                "内容": (
                    f"{name}さん: 休日{exact_off}日の指定では"
                    f"出勤{required_work}日が必要ですが、"
                    f"連勤上限{_personal_limit(str(name))}日では"
                    f"最大{possible_work}日しか出勤できません。"
                ),
                "対処": "休日数を増やすか、連勤上限の扱いを見直してください。",
            })

    # holiday_overrides（最低休日数）版
    for name, min_off_req in holiday_overrides.items():
        try:
            min_off_req = int(min_off_req)
        except (TypeError, ValueError):
            continue
        n_forced_work = len(forced_work_days_by_name.get(str(name), set()))
        max_off = days_in_month - n_forced_work
        if min_off_req > max_off:
            findings.append({
                "区分": "最低休日数と出勤指定の矛盾",
                "確度": "確実",
                "内容": (
                    f"{name}さん: 最低休日{min_off_req}日が必要ですが、"
                    f"出勤指定が{n_forced_work}日あるため"
                    f"最大でも{max_off}日しか休めません。"
                ),
                "対処": "休日数か出勤指定のどちらかを調整してください。",
            })

    # ---- 日別の人員下限に対する概算チェック ---------------------
    if employees:
        eco_names = {
            str(e.name) for e in employees
            if getattr(e, "skill", None) == Skill.ECO
        }
        all_names = {str(e.name) for e in employees}
        for d in range(1, days_in_month + 1):
            mode = operation_modes.get(d, OperationMode.NORMAL)
            if mode == OperationMode.CLOSED:
                continue
            capacity = get_capacity(mode)
            weekday = date(int(year), int(month), d).weekday()
            required_eco = 0
            required_total = 0
            for store, cap in capacity.items():
                if cap is None or weekday in cap.closed_dow:
                    continue
                required_eco += int(cap.eco_min)
                required_total += int(cap.eco_min) + int(cap.ticket_min)
            resting = {
                name for (name, day), _r in forced_rest.items() if day == d
            }
            available_eco = len(eco_names - resting)
            available_total = len(all_names - resting)
            if available_eco < required_eco:
                findings.append({
                    "区分": "エコ人員不足の可能性",
                    "確度": "可能性",
                    "内容": (
                        f"{int(month)}月{d}日: 必要エコ{required_eco}名に対し、"
                        f"休み確定を除くと出勤可能なエコが{available_eco}名です。"
                    ),
                    "対処": (
                        "該当日の×休みの調整を依頼するか、"
                        "営業モード（省人員）の適用を検討してください。"
                    ),
                })
            elif available_total < required_total:
                findings.append({
                    "区分": "総人員不足の可能性",
                    "確度": "可能性",
                    "内容": (
                        f"{int(month)}月{d}日: 必要総数{required_total}名に対し、"
                        f"休み確定を除く出勤可能者が{available_total}名です。"
                    ),
                    "対処": "該当日の×休みの調整や応援の検討をしてください。",
                })

    return findings


# ============================================================
# 2. ルール群を1つずつ外す緩和探索（ソルバー使用）
# ============================================================

# (表示名, base_kwargs を変換する関数)
PROBE_DEFINITIONS: list[tuple[str, Callable[[dict], dict]]] = [
    (
        "前月からの連勤持ち越し",
        lambda kw: {**kw, "prev_month": []},
    ),
    (
        "月末月初の固定配置",
        lambda kw: {**kw, "disable_month_edge_rules": True},
    ),
    (
        "出勤希望（絶対）",
        lambda kw: {**kw, "work_requests": []},
    ),
    (
        "×休み希望",
        lambda kw: {**kw, "off_requests": {}},
    ),
    (
        "柔軟休み（△・どちらか休み）",
        lambda kw: {**kw, "flexible_off": []},
    ),
    (
        "自由記載から反映した条件（休日数・連勤上限・日付指定）",
        lambda kw: {
            **kw,
            "exact_holiday_days": {},
            "employee_max_consecutive_work": {},
            "employee_max_consecutive_off": {},
            "required_assignments": [],
        },
    ),
]


def probe_rule_relaxations(
    base_kwargs: dict,
    time_limit_per_probe: int = 20,
    progress_callback: Optional[Callable[[str], Any]] = None,
) -> list[dict]:
    """ルール群を1つずつ外して再生成を試し、原因の候補を特定する。

    Args:
        base_kwargs: 本番の generate_shift に渡した引数一式
        time_limit_per_probe: 1回の試算あたりのソルバー制限秒数
        progress_callback: 進捗表示用（"月末月初の固定配置 を確認中..." など）

    Returns:
        list[dict]: {"外した条件", "結果", "所要秒"} のリスト。
        「解あり」になった条件が、解なしの原因（の一部）。
    """
    from .generator import generate_shift

    valid_params = set(inspect.signature(generate_shift).parameters)
    results: list[dict] = []

    for label, transform in PROBE_DEFINITIONS:
        if progress_callback:
            try:
                progress_callback(f"「{label}」を外して試算中...")
            except Exception:
                pass
        kwargs = transform(dict(base_kwargs))
        kwargs["time_limit_seconds"] = int(time_limit_per_probe)
        kwargs["verbose"] = False
        status_box: dict = {}
        kwargs["status_out"] = status_box
        kwargs = {k: v for k, v in kwargs.items() if k in valid_params}
        try:
            shift = generate_shift(**kwargs)
        except Exception as exc:
            results.append({
                "外した条件": label,
                "結果": f"試算エラー（{type(exc).__name__}）",
                "所要秒": "",
            })
            continue
        if shift is not None:
            outcome = "✅ 解あり（この条件が原因の可能性大）"
        elif status_box.get("status") == "INFEASIBLE":
            outcome = "解なし（この条件は原因ではない）"
        else:
            outcome = "時間内に判定できず"
        results.append({
            "外した条件": label,
            "結果": outcome,
            "所要秒": status_box.get("wall_time_seconds", ""),
        })

    return results


def summarize_probe_results(results: list[dict]) -> str:
    """緩和探索の結果を1〜2文の日本語まとめにする。"""
    culprits = [
        r["外した条件"] for r in results
        if str(r.get("結果", "")).startswith("✅")
    ]
    if culprits:
        return (
            "次の条件を外すと解が見つかりました: "
            + "、".join(culprits)
            + "。この条件と他の条件の組み合わせが解なしの原因です。"
        )
    if any("時間内に判定できず" in str(r.get("結果", "")) for r in results):
        return (
            "単独の条件では原因を特定できませんでした（一部は時間内に判定できず）。"
            "複数条件の組み合わせ、または人員数そのものが原因の可能性があります。"
        )
    return (
        "どの条件を単独で外しても解は見つかりませんでした。"
        "複数条件の組み合わせ、または人員数そのものが原因の可能性があります。"
    )
