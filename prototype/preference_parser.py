"""
自然言語希望解析（Claude API活用）
================================================
従業員が自由記述で書いた希望を Claude API で構造化された制約に変換する。

入力例:
    「5連勤は避けてください。月末は実家に帰るので30日と31日は休みたいです。
     基本的には赤羽中心で勤務希望ですが、たまに大宮も行けます。」

出力（構造化）:
    {
      "off_requests": [30, 31],
      "store_preference": {"AKABANE": "STRONG", "OMIYA": "WEAK"},
      "max_consecutive_work": 4,
      "natural_language_summary": "..."
    }

注意:
- ANTHROPIC_API_KEY 環境変数が必要
- API呼び出しは課金される（Sonnetで約 $0.003/件）
- プロンプトキャッシュで2回目以降は安くなる
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from typing import Optional

try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# ============================================================
# システムプロンプト
# ============================================================

SYSTEM_PROMPT = """\
あなたは大黒屋（ブランド買取店）のシフト管理システムの一部として動作する、
従業員からの自然言語による希望を構造化データに変換するアシスタントです。

# 大黒屋の店舗
1. 赤羽駅前店 (AKABANE)
2. 赤羽東口店 (HIGASHIGUCHI) ※月曜定休
3. 大宮駅前店 (OMIYA)
4. 大宮西口店 (NISHIGUCHI)
5. 大宮すずらん通り店 (SUZURAN)

# 出力形式
必ず以下のJSON形式で返してください（コードブロック不要、生のJSONのみ）：

{
  "off_requests": [日付の配列, 例: [3, 4, 13, 14]],
  "work_requests": [
    {"day": 日付, "store": "店舗ID or null"}
  ],
  "flexible_off_requests": [
    {"candidate_days": [候補日の配列], "n_required": 必要休み日数}
  ],
  "store_preferences": {
    "AKABANE": "STRONG/MEDIUM/WEAK/NONE",
    "HIGASHIGUCHI": "STRONG/MEDIUM/WEAK/NONE",
    "OMIYA": "STRONG/MEDIUM/WEAK/NONE",
    "NISHIGUCHI": "STRONG/MEDIUM/WEAK/NONE",
    "SUZURAN": "STRONG/MEDIUM/WEAK/NONE"
  },
  "max_consecutive_work": 数値（指定があれば、なければnull）,
  "min_holiday_days": 数値（その月の最低休日数指定があれば、なければnull）,
  "summary": "希望内容の要約（経営者向け、1-2文）",
  "ambiguous_points": ["明確化が必要な点があれば配列で記載、なければ空配列"]
}

# 解析ルール
- 日付の解釈は文脈から推測（例：「月末3日」= 29, 30, 31）
- 「絶対休みたい」→ off_requests
- 「できれば休み」→ flexible_off_requests
- 「○日と○日のどちらか」→ flexible_off_requests に candidate_days と n_required=1
- 店舗の好みは強度別に分類:
  - STRONG: 「中心に」「メインで」「ほとんど」「専属で」
  - MEDIUM: 「たまに」「時々」「適度に」
  - WEAK: 「少しだけ」「希望すれば」「補填で」
  - NONE: 「行きたくない」「避けたい」（未言及はnullではなく省略）
- 不明確な点は ambiguous_points に記載（強い推測は避ける）
"""


# ============================================================
# データクラス
# ============================================================

@dataclass
class ParsedPreference:
    """解析結果"""
    off_requests: list[int] = field(default_factory=list)
    work_requests: list[dict] = field(default_factory=list)
    flexible_off_requests: list[dict] = field(default_factory=list)
    store_preferences: dict[str, str] = field(default_factory=dict)
    max_consecutive_work: Optional[int] = None
    min_holiday_days: Optional[int] = None
    summary: str = ""
    ambiguous_points: list[str] = field(default_factory=list)
    raw_response: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ParsedPreference":
        return cls(
            off_requests=data.get("off_requests", []),
            work_requests=data.get("work_requests", []),
            flexible_off_requests=data.get("flexible_off_requests", []),
            store_preferences=data.get("store_preferences", {}),
            max_consecutive_work=data.get("max_consecutive_work"),
            min_holiday_days=data.get("min_holiday_days"),
            summary=data.get("summary", ""),
            ambiguous_points=data.get("ambiguous_points", []),
        )


# ============================================================
# パーサー本体
# ============================================================

class PreferenceParser:
    """Claude API を用いた希望解析パーサー"""

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-sonnet-4-5"):
        if not HAS_ANTHROPIC:
            raise ImportError(
                "anthropic パッケージがインストールされていません。"
                "`pip3 install anthropic` を実行してください。"
            )
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY が設定されていません。"
                "環境変数または引数で API キーを指定してください。"
            )
        self.client = Anthropic(api_key=self.api_key)
        self.model = model

    def parse(
        self,
        natural_language: str,
        target_year: int,
        target_month: int,
        employee_name: str = "",
    ) -> ParsedPreference:
        """自然言語をパース"""
        user_message = (
            f"対象月: {target_year}年{target_month}月\n"
            f"従業員: {employee_name or '（不明）'}\n"
            f"希望内容:\n{natural_language}\n\n"
            f"上記の希望をJSON形式で構造化してください。"
        )

        # プロンプトキャッシュを活用（システムプロンプトを cache_control 指定）
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = response.content[0].text.strip()
        # コードブロックが含まれる場合は除去
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            raw_text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            # フォールバック: 最初の { 〜 最後の } を抽出
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(raw_text[start : end + 1])
            else:
                raise ValueError(f"JSON解析失敗: {raw_text[:200]}") from e

        result = ParsedPreference.from_dict(data)
        result.raw_response = raw_text
        return result


# ============================================================
# 動作テスト
# ============================================================

EXAMPLES = [
    "5連勤は避けてください。月末は実家に帰るので30日と31日は休みたいです。基本的には赤羽中心で勤務希望ですが、たまに大宮も行けます。",
    "1日、3日、4日、5日、6日、16日、17日、30日は休み希望です。すずらんメインでお願いします。東口と西口は月2回ずつ程度なら大丈夫です。",
    "今月は10日間だけの勤務にしたいです。具体的には1日〜5日と17日〜24日、それに31日は休みたいです。すずらん・大宮・赤羽どこでも構いません。",
]


def main():
    print("【希望解析パーサー 動作テスト】\n")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠ ANTHROPIC_API_KEY が設定されていません。")
        print("\n以下のコマンドで設定してください：")
        print('  export ANTHROPIC_API_KEY="sk-ant-..."')
        print("\nAPIキーは https://console.anthropic.com/ で取得できます。")
        print("\nダミーのテスト出力例（API呼び出しなし）:")
        print("-" * 60)
        sample = ParsedPreference(
            off_requests=[30, 31],
            store_preferences={"AKABANE": "STRONG", "OMIYA": "WEAK"},
            max_consecutive_work=4,
            summary="月末2日休み希望、赤羽中心の勤務希望",
        )
        print(json.dumps(sample.__dict__, ensure_ascii=False, indent=2, default=str))
        return

    parser = PreferenceParser()
    for i, example in enumerate(EXAMPLES, 1):
        print(f"=== 例 {i} ===")
        print(f"入力: {example}\n")
        try:
            result = parser.parse(example, target_year=2026, target_month=6)
            print(f"📝 要約: {result.summary}")
            print(f"📅 休み希望: {result.off_requests}")
            print(f"📅 柔軟休み: {result.flexible_off_requests}")
            print(f"🏢 出勤希望: {result.work_requests}")
            print(f"🎯 店舗希望: {result.store_preferences}")
            print(f"⏰ 最大連勤: {result.max_consecutive_work}")
            print(f"📊 最低休日: {result.min_holiday_days}")
            if result.ambiguous_points:
                print(f"⚠ 不明確: {result.ambiguous_points}")
            print()
        except Exception as e:
            print(f"❌ エラー: {e}\n")


if __name__ == "__main__":
    main()
