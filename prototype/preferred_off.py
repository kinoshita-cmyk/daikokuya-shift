"""カレンダーの△と、明示された候補休みの日数条件を区別する。"""


def submitted_flexible_off(data):
    author = data.get("author")
    marked_days = data.get("triangle_off_days")
    # 旧マジックリンクは選択した△の日数//2を自動保存していた。
    # 本人提出の既知形式だけ補正し、system取込・別形式の明示条件には触れない。
    legacy_calendar = (marked_days is None and author not in (None, "", "system")
                       and "monthly_target_workdays" in data and "base_holidays" in data
                       and len(data.get("flexible_off", [])) == 1)
    for item in data.get("flexible_off", []):
        if isinstance(item, dict):
            name, days, count = item.get("employee"), item.get("candidate_days", []), item.get("n_required", 0)
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            name, days, count = item[:3]
        else:
            continue
        if not name or not days:
            continue
        count = int(count)
        calendar_item = name == author and (
            (marked_days is not None and set(days) == set(marked_days))
            or (legacy_calendar and count == len(days) // 2)
        )
        yield name, list(days), 0 if calendar_item else count
