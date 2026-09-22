"""連勤・連休の回数指定。読み取り、表示、生成、検証で共通の定義を使う。"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange


def consecutive_count_label(rule: dict) -> str:
    kind = "連休" if rule["kind"] == "off" else "連勤"
    comparison = {"exact": "ちょうど", "max": "最大", "min": "最低"}[rule["comparison"]]
    return f"{kind}回数: {rule['days']}{kind}を月内{comparison}{rule['count']}回"


def merge_consecutive_count_rules(original: list[dict], updates: list[dict]) -> list[dict]:
    """補正にある同じ長さの連勤・連休指定を置き換える。異なる条件は残す。"""
    keys = {(r.get("employee"), r["kind"], r["days"]) for r in updates}
    result = [dict(r) for r in original
              if (r.get("employee"), r["kind"], r["days"]) not in keys]
    for rule in updates:
        if rule not in result:
            result.append(dict(rule))
    return result


def parse_consecutive_counts(text: str) -> tuple[list[dict], list[str], str]:
    """回数を指定した句だけ取り除き、従来の日付解析に回数を渡さない。"""
    text = unicodedata.normalize("NFKC", text)
    digits = {char: str(value) for value, char in enumerate("〇一二三四五六七八九")}
    text = re.sub(r"[〇一二三四五六七八九](?=\s*(?:連勤|連休|回))",
                  lambda m: digits[m.group()], text)
    # 「月に1回だけの2連休」のように回数が先にある表現も同じ条件にする。
    text = re.sub(
        r"(?:月(?:に|内|間)?\s*)?"
        r"(?P<prefix>最大|上限|多くても|最低|少なくとも|ちょうど)?\s*"
        r"(?P<count>\d{1,2})\s*回\s*(?P<suffix>のみ|だけ|まで|以内|以下|以上)?\s*の\s*"
        r"(?P<days>\d{1,2})\s*連(?P<kind>勤|休)",
        lambda m: (f"{m['days']}連{m['kind']}を{m['prefix'] or ''}"
                   f"{m['count']}回{m['suffix'] or ''}"),
        text,
    )
    starts = list(re.finditer(r"(?P<days>\d{1,2})\s*連(?P<kind>勤|休)", text))
    rules, reviews, spans = [], [], []
    for index, start in enumerate(starts):
        stop = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        # 読点・改行以降の別の要望は従来の解析へ残す。
        separator = re.search(
            r"[。\n、,;；]|(?=有給|有休|合計|トータル|\d{1,2}\s*日は)",
            text[start.end():stop],
        )
        if separator:
            stop = start.end() + separator.start()
        clause = text[start.start():stop].strip()
        if "回" not in clause:
            continue
        spans.append((start.start(), stop))
        match = re.fullmatch(
            r"\d{1,2}\s*連[勤休]\s*(?:は|を|が|も)?\s*"
            r"(?:(?:月(?:内|間)?(?:に|で)?|1[かヶ]月に)\s*)?"
            r"(?P<prefix>最大|上限|多くても|最低|少なくとも|ちょうど)?\s*"
            r"(?P<count>\d{1,2})\s*回\s*"
            r"(?P<suffix>のみ|だけ|まで|以内|以下|以上)?\s*"
            r"(?:に|で|を)?\s*(?:です|希望(?:です|します)?|お願いします|"
            r"お願い(?:したいです|致します|いたします)?|とする|にする|してください|"
            r"してほしい(?:です)?)?\s*",
            clause,
        )
        prefix_context = text[max(0, start.start() - 18):start.start()]
        uncertain = re.search(r"(?:できれば|なるべく|可能なら|目安|最大|最低|上限)\s*$", prefix_context)
        if not match or uncertain or int(start["days"]) < 2:
            reviews.append(f"回数指定を自動反映できません: {clause}")
            continue
        upper = match["prefix"] in {"最大", "上限", "多くても"} or match["suffix"] in {"まで", "以内", "以下"}
        lower = match["prefix"] in {"最低", "少なくとも"} or match["suffix"] == "以上"
        exact = match["prefix"] == "ちょうど" or match["suffix"] in {"のみ", "だけ"}
        if sum((upper, lower, exact)) > 1:
            reviews.append(f"回数の上限・下限を確認してください: {clause}")
            continue
        rules.append({
            "kind": "off" if start["kind"] == "休" else "work",
            "days": int(start["days"]),
            "count": int(match["count"]),
            "comparison": "max" if upper else "min" if lower else "exact",
        })
    # 同じ文の矛盾した指定は一方を勝手に採用しない。
    for key in {(r["kind"], r["days"]) for r in rules}:
        group = [r for r in rules if (r["kind"], r["days"]) == key]
        lo = max([r["count"] for r in group if r["comparison"] != "max"] or [0])
        hi = min([r["count"] for r in group if r["comparison"] != "min"] or [31])
        if lo > hi:
            reviews.append(f"{key[1]}{'連休' if key[0] == 'off' else '連勤'}の回数指定が矛盾しています")
            rules = [r for r in rules if (r["kind"], r["days"]) != key]
    for start, stop in reversed(spans):
        text = text[:start] + " " * (stop - start) + text[stop:]
    return merge_consecutive_count_rules([], rules), reviews, text


def previous_run_length(prev_month, year: int, month: int, employee: str, kind: str) -> int:
    previous_year = year if month > 1 else year - 1
    previous_month = month - 1 if month > 1 else 12
    last_day = monthrange(previous_year, previous_month)[1]
    field = "last_off_days" if kind == "off" else "last_working_days"
    for item in prev_month or []:
        if getattr(item, "employee", None) == employee:
            days = set(getattr(item, field, []) or [])
            length = 0
            while last_day - length in days:
                length += 1
            return length
    return 0


def consecutive_block_windows(days_in_month: int, length: int, carry: int = 0):
    """月内で終わる、ちょうどlength日のまとまりの真偽条件。月末は当月末までで数える。"""
    for end in range(1, days_in_month + 1):
        start = end - length + 1
        requirements = [(day, True) for day in range(start, end + 1)]
        requirements.append((start - 1, False))
        if end < days_in_month:
            requirements.append((end + 1, False))
        in_month = []
        possible = True
        for day, value in requirements:
            if day >= 1:
                in_month.append((day, value))
            elif (carry > 0 and day >= 1 - carry) != value:
                possible = False
                break
        if possible:
            yield end, in_month


def matching_block_ends(states: dict[int, bool], length: int, carry: int = 0) -> list[int]:
    return [end for end, conditions in consecutive_block_windows(len(states), length, carry)
            if all(bool(states[day]) == value for day, value in conditions)]


def add_consecutive_count_constraints(model, off, rules, year, month, prev_month=None):
    """他の絶対条件とともに回数を保証する。条件を満たせなければ解なしにする。"""
    days = monthrange(year, month)[1]
    for index, rule in enumerate(rules or []):
        employee = rule["employee"]
        if employee not in off:
            raise ValueError(f"回数指定の対象者が生成対象にいません: {employee}")
        states = {day: (var if rule["kind"] == "off" else var.Not())
                  for day, var in off[employee].items()}
        carry = previous_run_length(prev_month, year, month, employee, rule["kind"])
        blocks = []
        for end, conditions in consecutive_block_windows(days, rule["days"], carry):
            literals = [states[day] if value else states[day].Not()
                        for day, value in conditions]
            block = model.NewBoolVar(f"run_count_{index}_{employee}_{end}")
            model.AddBoolAnd(literals).OnlyEnforceIf(block)
            model.AddBoolOr([literal.Not() for literal in literals]).OnlyEnforceIf(block.Not())
            blocks.append(block)
        count = sum(blocks)
        comparison = rule["comparison"]
        if comparison == "exact":
            model.Add(count == rule["count"])
        elif comparison == "max":
            model.Add(count <= rule["count"])
        elif comparison == "min":
            model.Add(count >= rule["count"])
        else:
            raise ValueError(f"回数指定の比較方法が不正です: {comparison}")
