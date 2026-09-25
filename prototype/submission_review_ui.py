"""本人提出と自由記載補正を同じ画面で扱う、管理者専用の表示。"""
import streamlit as st


def build_review_rows(names, submitted, adjustments, summaries, year, month,
                      reflection_builder, label_builder, day_formatter,
                      admin_leave=None):
    submitted_by_name = {item["employee"]: item for item in submitted}
    rows, details = [], {}
    for name in names:
        item = submitted_by_name.get(name, {})
        original = item.get("note", "") or item.get("note_excerpt", "")
        adjustment = adjustments.get(name, {})
        reflection = reflection_builder(original, adjustment, year, month, item.get("off_request_days", []))
        labels = label_builder(summaries.get(name, {})) if summaries is not None else []
        notes = list(reflection.get("notes", []))
        has_note = bool(original or adjustment)
        if summaries is None and has_note:
            notes.insert(0, "生成条件を取得できませんでした。再読込して確認してください。")
        elif has_note and not labels and adjustment.get("status") != "反映しない":
            notes.append("自由記載由来の条件は生成に入りません")
        review = "要確認" if notes and adjustment.get("status") != "反映しない" else "確認事項なし"
        if adjustment.get("status") == "反映しない":
            review = "反映しない"
        details[name] = dict(item=item, original=original, adjustment=adjustment,
                             reflection=reflection, labels=labels, notes=notes, review=review,
                             available=summaries is not None)
        rows.append({
            "氏名": name,
            "提出": "提出済み" if item else "未提出",
            "確認": review if has_note else "-",
            "×休み": day_formatter(item.get("off_request_days", [])),
            "△休み": day_formatter(item.get("flexible_off_days", [])),
            "出勤希望": day_formatter(item.get("work_request_days", [])),
            "有給": int(item.get("paid_leave_days", 0) or 0) + int((admin_leave or {}).get(name, 0) or 0),
            "自由記載の原文": original,
            "生成に使う自由記載条件": " / ".join(labels) if summaries is not None else "取得できませんでした",
        })
    return rows, details


def render_submission_review(rows, details, year, month, editor):
    needs_review = [name for name, item in details.items() if item["review"] == "要確認"]
    if needs_review:
        st.warning("自由記載の要確認: " + "、".join(needs_review))
    with st.expander("本人提出希望・自由記載の確認と補正", expanded=False):
        if not rows:
            st.info("対象スタッフがいません。")
            return
        names = [row["氏名"] for row in rows]
        key = f"submission_review_employee_{year}_{month}"
        table_key = f"submission_review_table_{year}_{month}"
        if st.session_state.get(key) not in names:
            st.session_state[key] = (needs_review or names)[0]

        def select_row():
            selection = st.session_state.get(table_key, {}).get("selection", {}).get("rows", [])
            if selection and 0 <= selection[0] < len(names):
                st.session_state[key] = names[selection[0]]

        st.dataframe(rows, hide_index=True, width="stretch", height=350, row_height=60,
                     on_select=select_row, selection_mode="single-row", key=table_key,
                     column_config={"氏名": st.column_config.TextColumn(width="small"),
                                    "自由記載の原文": st.column_config.TextColumn(width="large"),
                                    "生成に使う自由記載条件": st.column_config.TextColumn(width="large")})
        selected = st.selectbox("確認・補正するスタッフ", names, key=key)
        detail = details[selected]
        st.markdown("**原文（本人提出・変更不可）**")
        st.text(detail["original"] or "自由記載なし")
        st.markdown("**現在、生成に使う自由記載条件**")
        if detail["available"]:
            st.text("\n".join(detail["labels"]) or "自由記載由来の条件なし")
        else:
            st.error("生成条件を取得できていません。条件なしとは限りません。")
        for message in detail["notes"]:
            st.warning(message)
        with st.expander("自動読取・保存済み補正の内訳"):
            st.write("原文の自動読取: " + (" / ".join(detail["reflection"]["original_auto_labels"]) or "なし"))
            st.write("保存済み補正: " + (detail["adjustment"].get("corrected_text") or "なし"))
            st.write("反映方式: " + detail["reflection"]["source_label"])
        editor(detail["original"], detail["adjustment"], year, month, selected,
               detail["item"].get("off_request_days", []))
