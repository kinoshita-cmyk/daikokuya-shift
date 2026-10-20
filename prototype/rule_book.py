"""Read-only, allowlisted projection of the live scheduling rules for the rule book.

Never export raw submissions, employee notes, review memos, tokens or lock data.
The solver's rule functions remain the source of truth for monthly overrides.
"""
from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from datetime import date
from pathlib import Path

from . import rules
from .employee_config import employee_from_dict
from .models import Affinity, Store, EmploymentStatus, Role
from .paths import CONFIG_DIR
from .rule_config import RuleConfig
from .submission_loader import load_submissions_for_month, parse_natural_language_note
from .submission_window import now_jst
from .work_recovery import CLOSE_LONG_WORK_DESCRIPTION
from .consecutive_counts import consecutive_count_label
from .employees import employee_is_in_service

SCHEMA_VERSION = 1
STORE_NAMES = {s: s.display_name for s in Store}
BASE_URL = "https://daikokuya-shift-rules.r8pgp4xf9v.chatgpt.site"


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    # A broken source must stop publication, not silently publish default rules.
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def card(key, title, text, strength="目標", scope="全月共通", detail=""):
    return dict(id=key, title=title, text=text, strength=strength,
                scope=scope, detail=detail)


def store_name(store):
    if isinstance(store, Store):
        return store.display_name
    try:
        return Store[str(store)].display_name
    except KeyError:
        return str(store or "店舗指定なし")


def days_text(days):
    return "・".join(str(d) for d in sorted(set(days))) + "日"


def effective_staff(emp, year, month):
    override = rules.monthly_employee_store_override(year, month, emp.name)
    categories = ("主担当", "通常担当", "応援・巡回", "絶対配置不可", "緊急時の候補")
    def buckets(affinities):
        result = {key: [] for key in categories}
        for store in Store:
            if store == Store.OFF:
                continue
            value = affinities.get(store, Affinity.NONE)
            key = {
                Affinity.STRONG: categories[0], Affinity.MEDIUM: categories[1],
                Affinity.WEAK: categories[2], Affinity.NONE: categories[3],
                None: categories[4],
            }[value]
            result[key].append(store.display_name)
        return result
    changed = bool(
        override.get("primary_store") or override.get("normal_stores") is not None
        or override.get("support_stores") is not None or override.get("remove_support_stores")
    )
    base = buckets(emp.affinities)
    effective = buckets(rules.effective_employee_store_affinities(emp, year, month))
    return {
        "id": "staff:" + emp.name, "name": emp.name,
        "skill": emp.skill.value, "eco_core": bool(getattr(emp, "is_eco_core", False)),
        "roles": effective, "baseline": base if changed else None,
        "scope": f"{year}-{month:02d}" if changed else "全月共通",
        "target": rules.get_monthly_work_target(emp.name, month, emp.annual_target_days),
        "only_on_request": bool(getattr(emp, "only_on_request_days", False)),
    }


def custom_cards(config, year, month):
    result = []
    for r in config.custom_rules:
        if not r.enabled or (r.target_year is not None and r.target_year != year):
            continue
        if r.target_month is not None and r.target_month != month:
            continue
        scope = f"{year}-{month:02d}" if r.target_year or r.target_month else "全月共通"
        locations = "・".join(store_name(s) for s in r.stores)
        if r.rule_type == "required_assignment":
            text = f"{r.employee}さんを{r.day}日に{locations}へ配置。"
        elif r.rule_type == "employee_store_count":
            comparison = {"min": "以上", "max": "以下", "exact": "ちょうど", "forbid": "禁止"}.get(r.comparison, r.comparison)
            text = f"{r.employee}さん：{locations}の勤務を{r.count}回{comparison}。"
        else:
            # Manager-authored rule notes are necessary operational conditions.
            # Employee free-text and private correction memos are never used here.
            text = r.description
        result.append(card(
            "custom:" + r.id, r.name, text,
            "参考" if r.rule_type == "note" else ("絶対" if r.severity == "ERROR" else "目標"),
            scope, "メモのみ。自動生成の制約にはなりません。" if r.rule_type == "note" else "",
        ))
    return result


def monthly_cards(year, month):
    """Use the same descriptions as the management screen, with typed labels."""
    ym = (year, month)
    metadata = []
    training = rules.tanaka_pair_training_rule(year, month)
    if training:
        metadata.append(("pair-training", training["employee"] + "さんの研修", "絶対"))
    for i, plan in enumerate(rules.monthly_training_plans(year, month)):
        metadata.append((f"training:{i}", str(plan.get("trainee")) + "さんの段階別研修",
                         "絶対" if plan.get("severity") == "ERROR" else "目標"))
    for name in rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES.get(ym, {}):
        metadata.append(("store:" + name, name + "さんの店舗区分", "目標"))
    if ym in rules.MONTHLY_YAMAMOTO_POLICIES:
        metadata.append(("yamamoto", "山本さんの勤務上限", "絶対"))
    if ym in rules.OMIYA_ANCHOR_RELAXED_MONTHS:
        metadata.append(("omiya-anchor", "大宮駅前の必須担当の例外", "月限定"))
    for name in rules.monthly_carryover_consecutive_allowances(year, month):
        metadata.append(("carryover:" + name, name + "さんの月境界の連勤上限", "絶対"))
    for first, second, _ in rules.monthly_avoid_same_off_rules(year, month):
        metadata.append(("same-off:" + first + ":" + second, first + "さん・" + second + "さんの同時休み回避", "目標"))
    for day in sorted(rules.monthly_operation_mode_overrides(year, month)):
        metadata.append(("mode:" + str(day), f"{day}日の営業体制", "絶対"))
    descriptions = rules.active_monthly_exception_descriptions(year, month)
    if len(metadata) != len(descriptions):
        raise ValueError("月別条件の表示形式が更新されています。条件ブックの対応を確認してください")
    return [card("monthly:" + key, title, text, strength, f"{year}-{month:02d}")
            for (key, title, strength), text in zip(metadata, descriptions)]


def submission_cards(sub, employees):
    """Publish only structured, effective conditions, not the input prose."""
    result = []
    for emp in employees:
        name = emp.name
        lines = []
        if sub.off_requests.get(name):
            lines.append("×休み（絶対）：" + days_text(sub.off_requests[name]))
        for label, entries in [
            ("出勤希望", sub.work_requests),
            ("出勤希望", sub.preferred_work_requests),
            ("出勤する場合の希望店舗", sub.conditional_store_requests),
        ]:
            for who, day, store in entries:
                if who == name:
                    text = f"{label}：{day}日・{store_name(store)}"
                    if text not in lines:
                        lines.append(text)
        for who, candidates, required in sub.flexible_off:
            if who == name:
                lines.append(f"候補休み：{days_text(candidates)}のうち{required}日")
        for who, candidates, required, store in sub.preferred_work_groups:
            if who == name:
                lines.append(f"候補出勤：{days_text(candidates)}のうち{required}日・{store_name(store)}")
        for label, values in [
            ("有給（申請・管理者調整込み）", sub.paid_leave_days), ("指定休日合計", sub.requested_holiday_days),
            ("連勤上限", sub.max_consecutive_work_days), ("連休上限", sub.max_consecutive_off_days),
        ]:
            if name in values:
                lines.append(f"{label}：{values[name]}日")
        for who, count in sub.preferred_consecutive_off:
            if who == name:
                lines.append(f"連休希望：{count}連休を優先")
        for rule in sub.consecutive_count_rules:
            if rule.get("employee") == name:
                # Values come from the parser's structured schema, never the original text.
                lines.append(consecutive_count_label(rule))
        if not lines and name not in sub.submitted_employees:
            continue
        result.append(card(
            "submission:" + name, name + "さんの今月の希望", "\n".join(lines) or "追加の条件なし",
            "個別", f"{sub.year}-{sub.month:02d}",
            "管理者補正を含む読取結果。原文・確認メモは非掲載。",
        ))
    return result


def build_month(year, month, config, employees, ledger, sub=None):
    inactive_names = {e.name for e in employees if not employee_is_in_service(e, year, month)}
    employees = [e for e in employees if employee_is_in_service(e, year, month)]
    scope = f"{year}-{month:02d}"
    p = config.parameters
    core = "・".join(e.name for e in employees if getattr(e, "is_eco_core", False))
    sections = {key: [] for key in ("basic", "store", "individual")}
    overrides = {
        "最大連勤": f"基本の最大連勤は{p['max_consec_work']}日。個別指定がある場合は個別条件を使用します。",
        "4連勤超過回避": f"生成時は{rules.HARD_CONSTRAINTS['max_consecutive_work_days']}連勤を超える勤務をできるだけ避けます。",
        "2連休": f"月内に最低{p['min_2off_per_month']}回、最大{p['max_2off_per_month']}回を目標にします。個別の連休上限・回数指定は別途適用します。",
        "月別基準勤務日数": f"{month}月の基準は{rules.STANDARD_265_MONTHLY_WORK_TARGETS[month]}日（年間265日対象者）。実出勤＋有給で判定します。不足{rules.WORK_TARGET_SHORTFALL_WARNING_DIFF_DAYS}日以上・超過{rules.WORK_TARGET_OVERAGE_WARNING_DIFF_DAYS}日以上は警告、差が{rules.WORK_TARGET_ERROR_DIFF_DAYS}日以上はエラー。個別指定・補助要員は別扱いです。",
        "飛び石勤務の回避": f"「休み・出勤・休み」の1日だけの単独出勤を指します。エコ主力（{core or '対象者なし'}）は強く回避。その他は重要条件を崩さない範囲で回避。「出勤・休み・出勤」や2連勤は対象外です。",
        "5連勤の近接回避": CLOSE_LONG_WORK_DESCRIPTION,
        "今津の月曜赤羽優先": rules.IMAZU_MONDAY_DESCRIPTION,
        "今津の土日赤羽優先": rules.IMAZU_WEEKEND_DESCRIPTION,
        "大宮駅前アンカー": (
            "この月は、春山さん・下地さんのどちらか必須の条件を緩和します。"
            if rules.is_omiya_anchor_relaxed_month(year, month)
            else "大宮駅前には" + "・".join(rules.OMIYA_ANCHOR_STAFF) + "のどちらか1名が必ずいるようにします。"
        ),
        "店舗別最大人数": "、".join(f"{s.display_name}{limit.max_total}名" for s, limit in rules.STORE_STAFFING_LIMITS.items()) + "まで。",
        "店舗標準人数": "、".join(f"{s.display_name}{limit.standard_total}名" for s, limit in rules.STORE_STAFFING_LIMITS.items()) + "を目指します。",
        "1日全体人数上限": f"全店舗の出勤人数は1日最大{rules.GLOBAL_DAILY_STAFFING_LIMIT.max_total}名。",
        "全体標準人数": f"全店舗の出勤人数は1日標準{rules.GLOBAL_DAILY_STAFFING_LIMIT.standard_total}名。",
        "月内最低巡回条件": "固定の最低巡回回数は現在なし。" if not rules.STORE_ROTATION_MINIMUMS else "従業員別の巡回指定を適用します。",
        "月末月初の固定配置": (
            "月初1日と月末最終日は、" + "・".join(rules.MONTH_END_START_OMIYA_STAFF)
            + "を大宮駅前へ配置。店長の自店舗固定："
            + "、".join(f"{name}＝{store.display_name}" for name, store in rules.MONTH_EDGE_HOME_STORE_ASSIGNMENTS.items())
            + "。本人の×休み希望・対象店の休業日は固定配置の対象外です。"
        ),
        "月初固定勤務者の月末連勤上限": (
            "・".join(rules.MONTH_EDGE_FIXED_EMPLOYEES)
            + f"は月末時点で最大{rules.MONTH_END_MAX_CONSECUTIVE_FOR_FIXED_STAFF}連勤まで。"
        ),
        "大宮駅前2名体制の構成員": (
            "終日2名体制には" + "・".join(rules.OMIYA_TWO_PERSON_EXCLUDED_STAFF)
            + "を含めません。これらのスタッフを配置する日は合計3名以上にします。"
        ),
        "エコメンバー同店舗勤務NG": (
            "次の各グループ内の2名以上を同じ店舗に配置しません："
            + " / ".join("・".join(group) for group in rules.FORBIDDEN_SAME_STORE_GROUPS)
        ),
        "自由記載の月間休日指定": "読取済みの合計休日日数・出勤日数は、生成時の指定日数に反映します。最終的な指定内容は今月の個別条件に表示します。",
        "自由記載の希望": "自由記載は内容によって絶対条件と目標に分かれます。読取済みの構造化された条件のみを生成に渡します。",
        "絶対担当": "配置可能な店舗が1店舗のみの従業員は、その店舗だけが勤務候補です。下の従業員別店舗区分が現在の設定です。",
        "今津の赤羽比率": "今津さんの主担当・通常・応援は下の最新店舗区分に従います。月曜・土日は別途、赤羽駅前を強く優先します。",
    }
    skip = {"山本補助", "従業員別の店舗適性"}
    for item in ledger.get("rules", []):
        title = item.get("ルール", "")
        if item.get("分類") == "月別例外" or title in skip:
            continue
        text = overrides.get(title, item.get("内容", ""))
        strength = {"絶対条件": "絶対", "強い目標": "目標", "弱い目標": "ゆるめ"}.get(item.get("分類"), "参考")
        group = "store" if any(word in title for word in ("店舗", "赤羽東口", "大宮", "全体", "エコメンバー", "月末月初")) else "basic"
        if any(word in title for word in ("南の", "今津", "顧問", "新入社員", "山本")):
            group = "individual"
        sections[group].append(card(
            "base:" + title, title, text, strength,
            scope if title == "大宮駅前アンカー" and rules.is_omiya_anchor_relaxed_month(year, month) else "全月共通",
        ))
    sections["basic"].append(card("base:eco-support", "エコサポートの配置",
        "エコサポートは、独り立ちしたエコ担当と一緒に配置します。エコサポートだけでエコ担当の最低人数を満たす扱いにはしません。", "絶対"))
    policy = rules.yamamoto_monthly_policy(year, month)
    sections["individual"].append(card(
        "base:yamamoto", "山本さんの補助勤務",
        f"赤羽駅前で通常スタッフだけでは足りない日だけ補助配置します。それ以外の追加は手動。今月の上限は{policy['max_days']}日、連続{policy['max_consecutive']}日まで。上限まで出勤させる目標ではありません。本人の×休みは厳守します。",
        "絶対", scope if (year, month) in rules.MONTHLY_YAMAMOTO_POLICIES else "全月共通"))
    inactive_monthly_ids = {
        f"monthly:{kind}:{name}" for name in inactive_names for kind in ("store", "carryover")
    }
    monthly = [c for c in monthly_cards(year, month) if c["id"] not in inactive_monthly_ids]
    inactive_custom_ids = {"custom:" + r.id for r in config.custom_rules if r.employee in inactive_names}
    for c in custom_cards(config, year, month):
        if c["id"] in inactive_custom_ids:
            continue
        (sections["individual"] if c["scope"] == "全月共通" else monthly).append(c)
    sections["store"].append(card(
        "base:keyholders", "店舗の鍵担当", "\n".join(
            f"{s.display_name}：" + "・".join(names) for s, names in rules.STORE_KEYHOLDERS.items()
        ) + "\nすずらんは大宮駅前から" + "・".join(rules.SUZURAN_KEY_SUPPORT_FROM_OMIYA)
        + "が開け締め応援の候補です。", "ゆるめ", detail="配置の絶対条件ではなく、検証時の確認事項です。",
    ))
    capacity = [
        {"id": "capacity:" + s.name, "store": s.display_name,
         "standard": limit.standard_total, "maximum": limit.max_total,
         "eco": rules.NORMAL_CAPACITY[s].eco_min,
         "closed": "月曜休店" if rules.NORMAL_CAPACITY[s].closed_dow == (0,) else "なし"}
        for s, limit in rules.STORE_STAFFING_LIMITS.items()
    ]
    return {
        "year": year, "month": month, "monthly": monthly, **sections, "capacity": capacity,
        "staff": [effective_staff(e, year, month) for e in employees],
        "submissions": submission_cards(sub, employees) if sub else [],
        "pending_count": len(sub.pending_employees) if sub else len(employees),
        "submission_warning_count": len(sub.load_warnings) if sub else 0,
        "review_count": sum(
            bool(parse_natural_language_note(note, year, month).review_messages)
            or (bool(note.strip()) and name not in sub.parsed_note_summaries)
            for name, note in sub.natural_language_notes.items()
        ) if sub else 0,
        "parameters": dict(config.parameters),
        "disabled_checks": [k for k, enabled in config.enabled_checks.items() if not enabled],
    }


def build_snapshot(today=None, *, config_dir=CONFIG_DIR, include_submissions=True):
    """Read current local runtime data only. No network, no mutations, no solver."""
    today = today or now_jst().date()
    config_dir = Path(config_dir)
    config = RuleConfig.from_dict(read_json(config_dir / "rule_config.json", {}))
    # These are read internally by shared loaders. Refuse their silent fallback
    # if a source file is broken, so the public book keeps its last good copy.
    for filename in ("monthly_exceptions.json", "natural_language_adjustments.json"):
        read_json(config_dir / filename, {})
    employee_data = read_json(config_dir / "employees.json", None)
    if employee_data is None:
        from .employees import ALL_EMPLOYEES
        employees = list(ALL_EMPLOYEES)
    else:
        employees = [employee_from_dict(d) for d in employee_data["employees"]]
    ledger = read_json(config_dir / "rule_ledger_v1_0.json", {})
    if not ledger.get("rules"):
        raise ValueError("ルール台帳がないため、条件ブックの公開を見送りました")
    # Current month and the following 12 months always inherit the same live baseline.
    start = today.year * 12 + today.month - 1
    months = [(n // 12, n % 12 + 1) for n in range(start - 1, start + 13)]
    paid_adjustments = read_json(config_dir / "admin_paid_leave_adjustments.json", {}).get("adjustments", [])
    books = {}
    for year, month in months:
        names = [e.name for e in employees
                 if e.role not in (Role.ADVISOR, Role.REPRESENTATIVE)
                 and employee_is_in_service(e, year, month)]
        sub = load_submissions_for_month(year, month, names, sync_remote=False) if include_submissions else None
        if sub:
            for adj in paid_adjustments:
                if int(adj.get("year", 0)) == year and int(adj.get("month", 0)) == month:
                    name = adj.get("employee", "")
                    sub.paid_leave_days[name] = sub.paid_leave_days.get(name, 0) + int(adj.get("days", 0) or 0)
        books[f"{year}-{month:02d}"] = build_month(year, month, config, employees, ledger, sub)
    content_hash = digest(books)
    return {
        "schema": SCHEMA_VERSION, "generated_at": now_jst().isoformat(timespec="seconds"),
        "content_hash": content_hash, "default_month": f"{today.year}-{today.month:02d}",
        "source": "シフトシステムの設定・従業員マスタ・月別例外・提出条件",
        "months": books,
    }
