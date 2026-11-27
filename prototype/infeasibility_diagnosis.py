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
from copy import deepcopy
from calendar import monthrange
from datetime import date
from itertools import combinations
from time import monotonic
from typing import Any, Callable, Optional

from .models import OperationMode, Skill, Store
from .consecutive_counts import consecutive_off_limit_windows
from .rules import (
    HARD_CONSTRAINTS,
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
    employee_max_consecutive_off: Optional[dict] = None,
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
    employee_max_consecutive_off = employee_max_consecutive_off or {}

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
    for employee in employees or []:
        name = employee.name
        if (getattr(employee, "constraint_check_excluded", False)
                and name not in employee_max_consecutive_off):
            continue
        default_max_off = int(HARD_CONSTRAINTS.get("max_consecutive_off_days", 2) or 2)
        personal_max_off = int(employee_max_consecutive_off.get(name, 0) or 0)
        max_off = min(default_max_off, personal_max_off) if personal_max_off > 0 else default_max_off
        _, exemptions = consecutive_off_limit_windows(
            days_in_month, max_off, off_requests.get(name, []), operation_modes,
        )
        for window in exemptions:
            findings.append({
                "区分": "全店休業を含む連休の自動免除", "確度": "情報",
                "内容": f"{name}: {'・'.join(map(str, window))}日は本人×休み・全店休業だけで必ず連休になるため、連休上限{max_off}日の例外です。",
                "対処": "本人の×休みと全店休業は維持します。追加の自然休で上限を超える連休は作りません。",
            })
    # 出勤希望専任スタッフ（南さん型）の「希望していない日」
    on_request_only_workdays: dict = {}
    for name, d, _store in work_requests:
        if str(name) in MANDATORY_WORK_ON_REQUEST_EMPLOYEES:
            on_request_only_workdays.setdefault(str(name), set()).add(int(d))

    # ---- 強制出勤の一覧を作り、強制休みとの衝突を探す ----------
    # (name, day, store_or_none, 理由)
    forced_work: list[tuple] = []

    for name, d, store in work_requests:
        # 生成側と同じく、出勤希望と重複した×休みを最優先する。
        if 1 <= int(d) <= days_in_month and int(d) not in off_requests.get(name, []):
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
                    "本人の×休み・連勤上限は維持し、該当の出勤指定を見直してください。"
                    "前月連勤との衝突に限り、必要なら月境界の扱いを管理者で確認してください。"
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
                    "本人の×休みと全店休業は維持したまま、"
                    "休日数の指定（自由記載・管理者補正）が実際の希望と一致するか確認してください。"
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
            required_eco = 0
            required_total = 0
            for store, cap in capacity.items():
                if cap is None or not is_store_open_on_day(year, month, d, store, mode):
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
                        "本人の×休みは維持し、応援候補または"
                        "「⚙️ 設定 → 📅 月例外 → 営業モード」で"
                        "この日を省人員に設定することを検討してください。"
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
                    "対処": (
                        "本人の×休みは維持し、応援の検討、または"
                        "「⚙️ 設定 → 📅 月例外 → 営業モード」での"
                        "省人員設定を検討してください。"
                    ),
                })

    return findings


# ============================================================
# 2. 保存設定を変更しない、優先順の緩和試算（ソルバー使用）
# ============================================================

def run_generation_attempt(generate, kwargs, attempts, label):
    """各探索の終了理由を残す。後続の緊急探索で通常探索の記録を消さない。"""
    status = {}
    actual = dict(kwargs, status_out=status)
    started = monotonic()
    try:
        shift = generate(**actual)
    except Exception as exc:
        attempts.append({"段階": label, "状態": "ERROR", "例外": type(exc).__name__,
                         "経過秒": round(monotonic() - started, 2)})
        raise
    attempts.append({
        "段階": label,
        "状態": status.get("status", "FEASIBLE" if shift is not None else "UNKNOWN"),
        "経過秒": round(monotonic() - started, 2),
        "ソルバー秒": status.get("wall_time_seconds"),
        "上限秒": kwargs.get("time_limit_seconds"),
        "警告条件も必須": bool(kwargs.get("strict_warning_constraints", True)),
        "顧問上限": kwargs.get("advisor_max_days"),
        "エコサポート探索": deepcopy(status.get("eco_support_search", {})),
        "モデル検査": status.get("model_validation_error", ""),
    })
    print(f"[shift-generation-attempt] {attempts[-1]}", flush=True)
    if isinstance(kwargs.get("status_out"), dict):
        kwargs["status_out"].clear()
        kwargs["status_out"].update(status)
    return shift


def _diagnostic_flag(kwargs, flag):
    return {**kwargs, "_diagnostic_relaxations":
            set(kwargs.get("_diagnostic_relaxations", ())) | {flag}}


def _probe_definitions(base):
    from .rules import monthly_training_plans, tanaka_pair_training_rule

    year, month = int(base["year"]), int(base["month"])
    # 順位は検討順であり、承認や安全性の順位ではない。×休み・勤務可能日・技能は外さない。
    definitions = [
        ("月別の日付指定配置", lambda kw: {**kw, "required_assignments": []},
         any(str(r.get("severity") or "ERROR").upper() == "ERROR"
             for r in base.get("required_assignments", []) if isinstance(r, dict))),
        ("店舗別の勤務回数指定", lambda kw: {**kw, "monthly_store_count_rules": []},
         bool(base.get("monthly_store_count_rules"))),
        ("月別の研修日数・組み合わせ", lambda kw: _diagnostic_flag(kw, "monthly_training"),
         bool(monthly_training_plans(year, month) or tanaka_pair_training_rule(year, month))),
        ("長尾不在時の鈴木すずらん必須", lambda kw: _diagnostic_flag(kw, "suzuran_backup"), True),
        ("連勤・連休の回数指定", lambda kw: {**kw, "consecutive_count_rules": []},
         bool(base.get("consecutive_count_rules"))),
        ("月末の最大4連勤（通常の連勤上限は維持）", lambda kw: _diagnostic_flag(kw, "month_end_four"), True),
        ("月末月初の固定配置", lambda kw: {**kw, "disable_month_edge_rules": True},
         not base.get("disable_month_edge_rules", False)),
        ("休日数の固定指定（最低休日数は維持・本人確認が必要）",
         lambda kw: {**kw, "exact_holiday_days": {}}, bool(base.get("exact_holiday_days"))),
        ("個別の連休上限（本人確認が必要）",
         lambda kw: {**kw, "employee_max_consecutive_off": {}},
         bool(base.get("employee_max_consecutive_off"))),
    ]
    return [(label, transform) for label, transform, active in definitions if active]


def probe_rule_relaxations(
    base_kwargs: dict,
    time_limit_per_probe: int = 20,
    progress_callback: Optional[Callable[[str], Any]] = None,
    total_time_limit: int = 180,
    max_pair_probes: int = 12,
) -> list[dict]:
    """通常条件、単独緩和、2条件の組合せの順に試算。保存・本シフトへの反映はしない。

    本人×休み、出勤可能日、前月からの連勤、技能構成、人数下限は維持する。
    全組合せを網羅しないため、失敗しても「どの条件も原因でない」とは断定しない。
    """
    from .generator import generate_shift

    valid_params = set(inspect.signature(generate_shift).parameters)
    results: list[dict] = []

    base = deepcopy(base_kwargs)
    base["strict_warning_constraints"] = False
    base.pop("_diagnostic_relaxations", None)
    definitions = _probe_definitions(base)
    started = monotonic()

    def trial(selected, kind):
        remaining = float(total_time_limit) - (monotonic() - started)
        if remaining < 1:
            return None
        label = " + ".join(definitions[i][0] for i in selected) or "なし（現行の絶対条件を維持）"
        if progress_callback:
            try:
                progress_callback(f"{kind}: {label} を確認中...")
            except Exception:
                pass
        kwargs = deepcopy(base)
        for index in selected:
            kwargs = definitions[index][1](kwargs)
        kwargs["time_limit_seconds"] = max(1, min(int(time_limit_per_probe), int(remaining)))
        kwargs["verbose"] = False
        status_box: dict = {}
        kwargs["status_out"] = status_box
        unsupported_diagnostic = bool(kwargs.get("_diagnostic_relaxations")) and "_diagnostic_relaxations" not in valid_params
        kwargs = {k: v for k, v in kwargs.items() if k in valid_params}
        trial_started = monotonic()
        try:
            if unsupported_diagnostic:
                raise RuntimeError("generator.py に診断機能の更新が反映されていません")
            shift = generate_shift(**kwargs)
        except Exception as exc:
            status, outcome = "ERROR", f"試算エラー（{type(exc).__name__}）。条件緩和ではなくプログラムの確認が必要です"
        else:
            status = status_box.get("status", "FEASIBLE" if shift is not None else "UNKNOWN")
            if shift is not None:
                outcome = "✅ 解あり（保存・反映はしていません）"
            elif status == "INFEASIBLE":
                outcome = "解なし（この変更だけでは成立せず。原因候補からは除外できません）"
            elif status == "MODEL_INVALID":
                outcome = "モデル不正。ルールを外す前にプログラムの確認が必要です"
            else:
                outcome = "時間内に判定できず（成立しないとは断定できません）"
        row = {"試算区分": kind, "検討順": ",".join(str(i + 1) for i in selected) or "-",
               "外した条件": label, "結果": outcome, "判定": status,
               "所要秒": round(monotonic() - trial_started, 2)}
        results.append(row)
        return row

    baseline = trial((), "現行条件")
    if not baseline or baseline["判定"] in ("FEASIBLE", "OPTIMAL", "MODEL_INVALID", "ERROR"):
        return results
    for index in range(len(definitions)):
        if trial((index,), "単独の変更") is None:
            break
    if not any(row["判定"] in ("FEASIBLE", "OPTIMAL") for row in results):
        # 影響の小さい変更群から先に比較。2条件を同時に変えないと成立しない場合も調べる。
        pairs = sorted(combinations(range(len(definitions)), 2), key=lambda pair: (max(pair), sum(pair)))
        for pair in pairs[:max(0, int(max_pair_probes))]:
            row = trial(pair, "2条件の組み合わせ")
            if row is None or row["判定"] in ("FEASIBLE", "OPTIMAL"):
                break

    if float(total_time_limit) - (monotonic() - started) < 1:
        results.append({"試算区分": "調査上限", "検討順": "-", "外した条件": "未調査の候補あり",
                        "結果": "調査時間の上限です。未調査の候補は成立可否不明です",
                        "判定": "NOT_RUN", "所要秒": ""})

    return results


def summarize_probe_results(results: list[dict]) -> str:
    """緩和探索の結果を1〜2文の日本語まとめにする。"""
    if any(r.get("試算区分") == "現行条件" and r.get("判定") in ("FEASIBLE", "OPTIMAL") for r in results):
        return "現行の絶対条件のまま完成案が見つかりました。ルール変更は不要です。試算案は保存していません。"
    culprits = [
        r["外した条件"] for r in results
        if str(r.get("結果", "")).startswith("✅")
    ]
    if culprits:
        return (
            "次の条件を外すと解が見つかりました: "
            + "、".join(culprits)
            + "。変更候補であり、自動適用はしていません。×休み・必要人数・技能構成は維持しています。"
        )
    if any(r.get("判定") in ("ERROR", "MODEL_INVALID") for r in results):
        return "試算にプログラム上のエラーが含まれています。条件の矛盾とは区別して修正が必要です。"
    if any("時間内に判定できず" in str(r.get("結果", "")) for r in results):
        return (
            "調べた範囲では完成案が見つかりませんでした（一部は時間内に判定できず）。"
            "成立不能の証明ではありません。未調査の組み合わせや日別の人員配置の確認が必要です。"
        )
    return (
        "調べた条件・組み合わせでは解が見つかりませんでした。"
        "すべてのルールを網羅した調査ではありません。未調査の組み合わせや人員条件の確認が必要です。"
    )
