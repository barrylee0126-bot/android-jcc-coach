"""Explicitly selected hypotheses and public citations, never match rules."""

from .core import InterestAssumption


BASE_SOURCE = "https://www.taptap.cn/moment/540934479900837024"
CAP_EXCEPTION_SOURCE = (
    "https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/"
    "gizmos-gadgets-set-mechanic-overview-hextech-augments/"
)
MODE_EXCEPTION_SOURCE = (
    "https://teamfighttactics.leagueoflegends.com/en-sg/news/game-updates/"
    "teamfight-tactics-hyper-roll/"
)


def ten_gold_five_cap_hypothesis() -> InterestAssumption:
    """Historical secondary source; explicitly unverified for this match."""
    return InterestAssumption(
        assumption_id="historical-10g-1g-cap5-unbound-v1",
        gold_per_step=10, interest_per_step=1, max_interest=5,
        basis_kind="sourced_hypothesis",
        basis_note="历史国服社区术语说明支持10金一档、封顶5金；非游戏官方，未核实本局模式、补丁、强化及计息时点。",
        source_refs=(BASE_SOURCE,),
    )


def source_catalog() -> list[dict]:
    """Return fresh citation metadata; no network or archived article content."""
    return [
        {
            "url": BASE_SOURCE, "title": "金铲铲之战黑话术语科普！",
            "publisher": "TapTap 用户：戏仔-金铲铲", "source_kind": "historical_secondary",
            "published_on": "2024-05-18", "accessed_on": "2026-11-02",
            "supports": "正文说明每满10金币得1金币利息、上限5金币，并提及特殊情况。",
            "limit": "社区用户发帖，含交易推广；不是游戏官方，不证明本局规则或结算顺序。",
        },
        {
            "url": CAP_EXCEPTION_SOURCE,
            "title": "Gizmos & Gadgets Set Mechanic Overview: Hextech Augments",
            "publisher": "Riot Games / Riot Prism", "source_kind": "historical_primary_other_game",
            "published_on": "2021-10-21", "accessed_on": "2026-11-02",
            "supports": "官方历史TFT说明中，特定强化可将利息上限提高为70金币对应7金币。",
            "limit": "仅证明上限存在例外，不证明国服本局有此强化、名称或数值；不自动生成规则。",
        },
        {
            "url": MODE_EXCEPTION_SOURCE, "title": "Teamfight Tactics: Hyper Roll",
            "publisher": "Riot Games / Llord Llama", "source_kind": "historical_primary_other_game",
            "published_on": "2021-04-14", "accessed_on": "2026-11-02",
            "supports": "官方历史TFT模式说明中，Hyper Roll不提供利息收入。",
            "limit": "仅证明模式差异，不证明当前国服模式或规则；不自动选择零利息参数。",
        },
    ]
