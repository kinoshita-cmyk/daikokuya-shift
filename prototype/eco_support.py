"""独り立ち直後の配置方針。生成・検証・表示で同じ例外店舗を使う。"""
from .models import Skill, Store


APPROVED_SUPPORT_NAMES = frozenset({"大類", "田中"})
SKILL_UPDATE_KEY = "eco_support_update_20261025"
SOLO_EXCEPTION_STORES = {
    "大類": (Store.SUZURAN,),
    "田中": (Store.SUZURAN, Store.AKABANE),
}
SUPPORT_DESCRIPTION = (
    "エコサポートは原則、同じ店舗に独り立ち済みエコ担当が必要。"
    "人員不足時の例外として、同伴ありではシフトが成立しないと確認できた場合だけ、"
    "大類はすずらん、田中はすずらん・赤羽駅前で単独エコ担当を許容し、例外日数を最小化する。"
    "店舗全体の必要人数は減らさない。単独担当日は確認用の警告を表示する。"
)
SOLO_EXCEPTION_CATEGORY = "エコサポート不足時例外"
OMIYA_PAIR_CATEGORY = "大宮の下地・春山2名体制"
OMIYA_PAIR_DESCRIPTION = (
    "大宮駅前が下地・春山の2名だけになる配置を強く回避する。"
    "3名体制での同時勤務は可。本人の×休み・必要人数・技能構成・固定配置を優先し、"
    "代替が難しい場合は2名だけの配置を許容する。"
)
OMIYA_PAIR_PENALTY = 600


def solo_exception_allowed(employee, store):
    return (employee.skill == Skill.ECO_SUPPORT
            and store in SOLO_EXCEPTION_STORES.get(employee.name, ()))


def is_omiya_anchor_pair_only(names):
    return set(names) == {"下地", "春山"}
