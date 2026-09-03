"""Independent, evidence-first daily news selection (V2).

This module deliberately stops before dialogue writing.  It turns frozen RSS
leads into deduplicated events, applies deterministic evidence/risk gates, and
uses the configured text model only for editorial capacity and audience
judgement.  The legacy S/A/B script pipeline remains untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backlot.ai_text import TextAIError, _chat_json, daily_editorial_provider
from backlot.daily_automation import (
    DailyAutomationError,
    RUNS_ROOT,
    _atomic_json,
    _enrich_candidate_evidence,
    _evidence_priority,
    _now,
    _read_json,
    collect_news_candidates,
)


PROMPT_VERSION = "daily-news-selection-v2.6-entertainment-first"
OUTPUT_VERSION = "2.2"
MAX_MODEL_EVENTS = 8
ALLOWED_STORY_TYPES = {"risk", "product", "research", "business", "visual_event"}
ALLOWED_TOPIC_FAMILIES = {
    "robotics",
    "ai_model",
    "chips_compute",
    "science_space",
    "security_policy",
    "consumer_tech",
    "software_open_source",
    "gaming",
    "internet_platform",
    "other",
}
ALLOWED_EVENT_FORMS = {
    "visual_record",
    "risk_incident",
    "breakthrough",
    "price_change",
    "financing",
    "product_launch",
    "product_update",
    "trailer_announcement",
    "rumor",
}
ALLOWED_DIMENSIONS = {
    "event_core",
    "evidence_detail",
    "mechanism",
    "user_impact",
    "action_tip",
    "industry_value",
    "use_case",
    "constraint",
    "method",
    "limitation",
    "key_number",
    "uncertainty",
    "visual_moment",
}
FACT_DIMENSIONS = ALLOWED_DIMENSIONS - {"uncertainty"}

HEAT_BANDS = ((85, "H4"), (65, "H3"), (40, "H2"), (0, "H1"))
CAPACITY_LINES = {"C1": 1, "C2": 2, "C3": 3, "C4": 4}
UNDERSTANDING_EXPLANATION_SLOTS = {"U0": 0, "U1": 0, "U2": 1}
GENERIC_HEAT_MATCH_TOKENS = {
    "ai",
    "token",
    "人工智能",
    "大模型",
    "大模",
    "模型",
    "机器",
    "器人",
    "机器人",
    "人形",
    "人形机器人",
    "具身",
    "具身智能",
    "智能",
    "科技",
    "新品",
    "发布",
    "正式",
    "开源",
    "上线",
    "升级",
    "产品",
    "公司",
}

GENERIC_HEAT_ASCII_TOKENS = {
    "ai",
    "aigc",
    "api",
    "ceo",
    "pc",
    "ios",
    "android",
}

# Short technical identities are useful when a release is described very
# differently by a news site and by Douyin.  Keep this list deliberately small:
# generic category abbreviations must not be allowed to lend heat to siblings.
SHORT_HEAT_PRODUCT_IDENTITIES = {"vla", "vlm"}
HEAT_NAMED_ENTITY_TOKENS = {
    "小鹏",
    "腾讯",
    "阿里",
    "华为",
    "小米",
    "苹果",
    "谷歌",
    "百度",
    "字节",
    "英伟达",
    "长鑫",
}

SEO_PATTERNS = (
    r"售楼处|楼盘|优选好房|房价|户型图|购房|置业顾问|咨询电话",
    r"百度百科[★☆]|点击进入官网|官方网站入口|官网登录|app下载地址",
    r"一文读懂.*(?:官网|下载)|最新地址|备用网址|开户注册",
    r"选型参考|选购指南|购买建议|怎么选(?:更合适|最好)?|值不值得买",
)
RUMOUR_PATTERNS = r"泄露|爆料|曝出|消息称|据称|传闻|知情人士|黑客放出|偷跑"
STRONG_CLAIM_PATTERNS = (
    r"全球(?:首个|首款|首次|第一)|世界(?:首个|首款|首次|第一)",
    r"完全替代|全面超越|遥遥领先|(?:打破|再破|刷新).{0,24}(?:世界|人类|全球).{0,8}纪录|超越人类",
    r"(?:性能|速度|效率).{0,8}(?:提升|暴涨|增长)\s*(?:[1-9]\d{2,}%|\d+倍)",
    r"100%|零风险|绝对安全|彻底解决",
)
SOURCE_SUFFIX_RE = re.compile(r"\s+(?:[-–—|_]|·)\s*[^-–—|_]{2,30}$")
BOILERPLATE_RE = re.compile(
    r"(?:最新消息|重磅|突发|官宣|正式发布|今日|刚刚|来了|登上热搜|引发热议)",
    re.IGNORECASE,
)

PUBLISHER_ALIASES = {
    "it之家": "ithome.com",
    "ithome": "ithome.com",
    "爱范儿": "ifanr.com",
    "ifanr": "ifanr.com",
    "量子位": "qbitai.com",
    "qbitai": "qbitai.com",
    "36kr": "36kr.com",
    "36氪": "36kr.com",
}

PRODUCT_FAMILY_PATTERNS = (
    # ``\b`` treats Chinese characters as word characters, so “全新Mac
    # mini发布” has no boundary around the English name. ASCII lookarounds
    # preserve that identity and keep its heat away from sibling products.
    re.compile(r"(?i)(?<![a-z0-9])mac\s+(?:mini|studio|pro|book(?:\s+(?:air|pro))?)(?![a-z0-9])"),
    re.compile(
        r"(?i)(?<![a-z0-9])(?:redmi|iphone|ipad|galaxy|pixel|boltt)\s+"
        r"[a-z0-9][a-z0-9.+-]*(?:\s+[a-z0-9.+-]+)?(?![a-z0-9])"
    ),
)

TECH_TOPIC_PATTERN = re.compile(
    r"(?i)AI|人工智能|大模型|模型|机器人|芯片|处理器|手机|电脑|Mac|iPhone|iPad|"
    r"软件|应用|浏览器|开源|代码|开发者|游戏|显卡|服务器|算力|自动驾驶|智能驾驶|"
    r"电池|新能源|火箭|航天|卫星|量子|科研|科学|科普|网络|互联网|数据|隐私|安全|"
    r"苹果|华为|小米|荣耀|英伟达|OpenAI|微软|谷歌|百度|腾讯|阿里|字节|DeepSeek"
)


class NewsSelectionV2ValidationError(DailyAutomationError):
    """The model response cannot satisfy the frozen V2 selection contract."""

    def __init__(self, issues: list[str]):
        self.issues = list(dict.fromkeys(str(issue) for issue in issues if str(issue).strip()))
        super().__init__("；".join(self.issues))


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _title_without_publisher(value: Any) -> str:
    text = _clean(value)
    previous = ""
    while text != previous:
        previous = text
        text = SOURCE_SUFFIX_RE.sub("", text).strip()
    return BOILERPLATE_RE.sub("", text).strip(" ，。！？:：-—|")


def _publisher_key(candidate: dict[str, Any]) -> str:
    for field in ("evidence_url", "url"):
        host = urlsplit(_clean(candidate.get(field))).netloc.lower().removeprefix("www.")
        if host and host not in {"news.google.com", "google.com"}:
            if host == "ithome.com" or host.endswith(".ithome.com"):
                return "ithome.com"
            if host == "ifanr.com" or host.endswith(".ifanr.com"):
                return "ifanr.com"
            if host == "qbitai.com" or host.endswith(".qbitai.com"):
                return "qbitai.com"
            if host == "36kr.com" or host.endswith(".36kr.com"):
                return "36kr.com"
            return host
    title = _clean(candidate.get("title"))
    suffixes = re.split(r"\s+(?:[-–—|_]|·)\s*", title)
    if len(suffixes) > 1 and 2 <= len(suffixes[-1]) <= 30:
        suffix_key = re.sub(r"\W+", "", suffixes[-1].lower())
        return PUBLISHER_ALIASES.get(suffix_key, suffix_key or _clean(candidate.get("source_id")))
    return _clean(candidate.get("source_name") or candidate.get("source_id") or "unknown").lower()


def _product_families(title: Any) -> set[str]:
    text = _title_without_publisher(title).lower()
    return {
        re.sub(r"\s+", " ", match.group(0)).strip()
        for pattern in PRODUCT_FAMILY_PATTERNS
        for match in pattern.finditer(text)
    }


def _event_tokens(title: Any) -> set[str]:
    normalized = _title_without_publisher(title).lower()
    tokens = set(re.findall(r"[a-z][a-z0-9.+-]{1,}|\d+(?:\.\d+)?", normalized))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        tokens.add(chunk)
        tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return {token for token in tokens if token not in {"公司", "科技", "产品", "宣布", "推出", "发布"}}


def _heat_match_tokens(title: Any) -> set[str]:
    """Tokenize event identity without social-platform hashtag boilerplate."""
    text = re.sub(r"#[^\s#]+", " ", _title_without_publisher(title))
    return _event_tokens(text)


def _has_specific_heat_identity(shared: set[str]) -> bool:
    """Return whether a low-similarity pair shares a real product identity.

    Chinese two-character windows are valuable for high-overlap title matching,
    but they are unsafe as a relaxed identity signal: unrelated stories often
    share fragments such as ``人工``/``工智`` or ``英伟``/``伟达``.  The relaxed
    path therefore needs an explicit model token (letters plus digits), two
    independent named ASCII tokens, or one of the small approved acronyms.
    """
    ascii_tokens = {
        token.lower()
        for token in shared
        if re.fullmatch(r"[a-z][a-z0-9.+-]{2,}", token, re.IGNORECASE)
        and token.lower() not in GENERIC_HEAT_ASCII_TOKENS
    }
    if any(re.search(r"[a-z]", token) and re.search(r"\d", token) for token in ascii_tokens):
        return True
    if len({token for token in ascii_tokens if len(token) >= 4}) >= 2:
        return True
    if ascii_tokens & SHORT_HEAT_PRODUCT_IDENTITIES:
        return bool(shared & HEAT_NAMED_ENTITY_TOKENS)
    return False


def _same_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_title = _title_without_publisher(left.get("title"))
    right_title = _title_without_publisher(right.get("title"))
    if not left_title or not right_title:
        return False
    left_families, right_families = _product_families(left_title), _product_families(right_title)
    if left_families and right_families and not (left_families & right_families):
        # Shared company/series words must not merge different products such as
        # Mac mini and Mac Studio.  A polluted cluster can otherwise make one
        # sensational side claim reject a perfectly usable hot event.
        return False
    if left_families & right_families:
        return True
    if (
        re.search(r"人形?机器人", left_title)
        and re.search(r"人形?机器人", right_title)
        and re.search(r"百米|100\s*米", left_title, re.IGNORECASE)
        and re.search(r"百米|100\s*米", right_title, re.IGNORECASE)
    ):
        return True
    compact_left = re.sub(r"\W+", "", left_title.lower())
    compact_right = re.sub(r"\W+", "", right_title.lower())
    if min(len(compact_left), len(compact_right)) >= 8 and (
        compact_left in compact_right or compact_right in compact_left
    ):
        return True
    left_tokens, right_tokens = _event_tokens(left_title), _event_tokens(right_title)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    dice = 2 * overlap / (len(left_tokens) + len(right_tokens))
    long_shared = any(len(token) >= 4 for token in left_tokens & right_tokens)
    return dice >= 0.48 or (long_shared and dice >= 0.34)


def cluster_news_events(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cluster title-level leads before any heat calculation.

    Union-find keeps the result deterministic and prevents repeated aggregator
    headlines from being treated as independent stories.
    """
    rows = [dict(item) for item in candidates if isinstance(item, dict) and _clean(item.get("candidate_id"))]
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if _same_event(rows[left], rows[right]):
                union(left, right)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, candidate in enumerate(rows):
        grouped[find(index)].append(candidate)

    events: list[dict[str, Any]] = []
    for members in grouped.values():
        members.sort(
            key=lambda item: (
                item.get("evidence_status") == "ok",
                item.get("authority") == "official",
                len(_clean(item.get("evidence_excerpt"))),
            ),
            reverse=True,
        )
        candidate_ids = sorted(_clean(item.get("candidate_id")) for item in members)
        event_id = "E-" + hashlib.sha256("|".join(candidate_ids).encode("utf-8")).hexdigest()[:14].upper()
        factual_members = [item for item in members if item.get("discovery_only") is not True]
        publisher_keys = sorted({_publisher_key(item) for item in factual_members if _publisher_key(item)})
        evidence_members = [item for item in members if item.get("evidence_status") == "ok"]
        evidence_publisher_keys = sorted({_publisher_key(item) for item in evidence_members if _publisher_key(item)})
        official_members = [item for item in evidence_members if item.get("authority") == "official"]
        events.append(
            {
                "event_id": event_id,
                "canonical_title": _title_without_publisher(members[0].get("title")),
                "candidate_ids": candidate_ids,
                "publisher_keys": publisher_keys,
                "evidence_publisher_keys": evidence_publisher_keys,
                "independent_publisher_count": len(publisher_keys),
                "independent_evidence_publisher_count": len(evidence_publisher_keys),
                "cluster_size": len(members),
                "evidence_candidate_ids": [_clean(item.get("candidate_id")) for item in evidence_members],
                "official_evidence_candidate_ids": [_clean(item.get("candidate_id")) for item in official_members],
                "discovery_provenance": [
                    {
                        "candidate_id": _clean(item.get("candidate_id")),
                        "source_id": _clean(item.get("source_id")),
                        "source_name": _clean(item.get("source_name")),
                        "truth_status": _clean(item.get("truth_status")),
                        "evidence_status": _clean(item.get("evidence_status")),
                        "copy_skill_hotspot": dict(item.get("copy_skill_hotspot") or {}),
                    }
                    for item in members
                    if item.get("discovery_only") is True
                ],
                "members": members,
            }
        )
    return sorted(events, key=lambda item: (item["canonical_title"], item["event_id"]))


def _event_corpus(event: dict[str, Any]) -> str:
    return " ".join(
        _clean(member.get(field))
        for member in event.get("members") or []
        if isinstance(member, dict)
        for field in ("title", "summary", "evidence_excerpt")
    )


def _first_official_image(event: dict[str, Any]) -> dict[str, str]:
    """Resolve the event's official share image from its enriched evidence members."""
    for member in event.get("members") or []:
        if isinstance(member, dict) and _clean(member.get("evidence_image_url")):
            return {
                "url": _clean(member["evidence_image_url"]),
                "attribution": _clean(member.get("evidence_image_attribution")),
            }
    return {}


def evaluate_event_gates(event: dict[str, Any]) -> dict[str, Any]:
    corpus = _event_corpus(event)
    evidence_count = len(event.get("evidence_candidate_ids") or [])
    official_count = len(event.get("official_evidence_candidate_ids") or [])
    publisher_count = int(event.get("independent_evidence_publisher_count") or 0)
    platform_hot = bool(event.get("external_heat_matches"))
    reasons: list[str] = []
    wording_policy = "verified_facts"
    risk_gate = "pass"

    if not TECH_TOPIC_PATTERN.search(_clean(event.get("canonical_title"))):
        risk_gate = "fail"
        reasons.append("热榜事件不属于科技快报内容范围")

    evidence_gate = "pass" if evidence_count else "fail"
    if evidence_gate == "fail":
        reasons.append("没有可读取的原站正文证据")

    if any(re.search(pattern, corpus, re.IGNORECASE) for pattern in SEO_PATTERNS):
        risk_gate = "fail"
        reasons.append("命中导流、地产或搜索污染特征")
    elif re.search(RUMOUR_PATTERNS, corpus, re.IGNORECASE) and not official_count and publisher_count < 2:
        if platform_hot and evidence_count:
            wording_policy = "attribute_rumour"
            reasons.append("平台热点传闻仅可归因转述，不得写成官方结论")
        else:
            risk_gate = "review"
            reasons.append("传闻或泄露类信息缺少官方或两家独立来源交叉核验")

    strong_claim = any(re.search(pattern, corpus, re.IGNORECASE) for pattern in STRONG_CLAIM_PATTERNS)
    if strong_claim and not official_count and publisher_count < 2:
        if platform_hot and evidence_count:
            wording_policy = "hot_topic_expressive"
            reasons.append("平台热点保留传播性标题口径，核心对象与数字仍以正文为准")
        else:
            evidence_gate = "fail"
            reasons.append("极强结论缺少官方或两家独立来源交叉核验")

    return {
        "evidence_gate": evidence_gate,
        "risk_gate": risk_gate,
        "strong_claim": strong_claim,
        "wording_policy": wording_policy,
        "gate_reasons": list(dict.fromkeys(reasons)),
    }


def _external_heat_matches(event: dict[str, Any], signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_tokens = _heat_match_tokens(event.get("canonical_title"))
    event_families = _product_families(event.get("canonical_title"))
    if not event_tokens:
        return []
    matches: list[dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        candidate_titles = [signal.get("title"), *(signal.get("aliases") or [])]
        best_match: tuple[float, set[str]] | None = None
        for signal_title in candidate_titles:
            signal_families = _product_families(signal_title)
            if signal_families and not (event_families & signal_families):
                continue
            shared_families = event_families & signal_families
            signal_tokens = _heat_match_tokens(signal_title)
            shared = event_tokens & signal_tokens
            if not shared:
                continue
            dice = 2 * len(shared) / max(1, len(event_tokens) + len(signal_tokens))
            # High-overlap Chinese titles remain valid.  A low-overlap pair,
            # however, must share a concrete model/product identity; generic
            # category words and hashtag campaigns are never enough.
            if dice < 0.34 and not (
                shared_families or (dice >= 0.08 and _has_specific_heat_identity(shared))
            ):
                continue
            if best_match is None or dice > best_match[0]:
                best_match = (dice, shared)
        if best_match is None:
            continue
        dice, _ = best_match
        matches.append(
            {
                "source_id": _clean(signal.get("source_id")),
                "source_name": _clean(signal.get("source_name")),
                "title": _clean(signal.get("title")),
                "rank": int(signal.get("rank") or 999),
                "heat_value": int(signal.get("heat_value") or 0),
                "heat_score": float(signal.get("heat_score") or 0),
                "match_score": round(dice, 3),
                "scope": _clean(signal.get("scope")),
                "truth_status": _clean(signal.get("truth_status")),
                "disclaimer": _clean(signal.get("disclaimer")),
                "provenance": dict(signal.get("provenance") or {}),
            }
        )
    return sorted(matches, key=lambda item: (item["rank"], -item["match_score"]))[:5]


def _platform_heat_floor(matches: list[dict[str, Any]]) -> int:
    """Translate a real public ranking into the primary heat signal.

    Media repetition is useful corroboration but must never outrank an event
    that audiences are demonstrably browsing.  Douyin receives a slightly
    stronger floor than a general-purpose Baidu list.
    """
    floors: list[int] = []
    for item in matches:
        rank = max(1, int(item.get("rank") or 999))
        source_id = _clean(item.get("source_id")).lower()
        is_douyin = "douyin" in source_id or "抖音" in _clean(item.get("source_name"))
        if is_douyin:
            floor = 96 if rank <= 10 else 88 if rank <= 20 else 80 if rank <= 50 else 72
        else:
            floor = 90 if rank <= 10 else 82 if rank <= 20 else 75 if rank <= 30 else 68 if rank <= 50 else 62
        floors.append(floor)
    return max(floors, default=0)


def observed_heat(event: dict[str, Any]) -> dict[str, Any]:
    """Compute explainable heat signals; the language model cannot alter them."""
    publisher_count = min(int(event.get("independent_publisher_count") or 0), 5)
    evidence_count = min(len(event.get("evidence_candidate_ids") or []), 4)
    official = bool(event.get("official_evidence_candidate_ids"))
    # Reposts from one publisher are not independent attention signals.  Cap
    # clustered mentions by unique publisher count so duplicate headlines
    # cannot manufacture heat.
    cluster_size = min(int(event.get("cluster_size") or 0), publisher_count, 5)
    china_high = any(
        ((member.get("china_short_video_hint") or {}).get("likely_china_relevance") == "high")
        for member in event.get("members") or []
        if isinstance(member, dict)
    )
    external_matches = event.get("external_heat_matches") if isinstance(event.get("external_heat_matches"), list) else []
    best_domestic_rank = min((int(item.get("rank") or 999) for item in external_matches if isinstance(item, dict)), default=None)
    platform_heat_score = _platform_heat_floor(external_matches)
    signals = {
        "independent_publishers": publisher_count,
        "evidence_sources": evidence_count,
        "official_confirmation": official,
        "cluster_mentions": cluster_size,
        "china_audience_relevance_hint": china_high,
        "domestic_public_heat_match_count": len(external_matches),
        "domestic_public_best_rank": best_domestic_rank,
        "platform_heat_score": platform_heat_score,
    }
    media_score = publisher_count * 13 + evidence_count * 7 + cluster_size * 4 + (18 if official else 0) + (8 if china_high else 0)
    # Without a platform signal, repeated media coverage alone is capped below
    # H3.  This prevents well-documented but low-interest parameter releases
    # from impersonating public hotspots.
    if not external_matches and not official:
        media_score = min(media_score, 64)
    score = min(100, max(media_score, platform_heat_score))
    level = next(level for threshold, level in HEAT_BANDS if score >= threshold)
    return {"observed_heat_score": score, "heat_level": level, "heat_signals": signals, "external_heat_matches": external_matches}


def prefetch_selection_evidence_v2(
    candidates: list[dict[str, Any]],
    *,
    limit: int = 18,
    max_workers: int = 4,
    priority_candidate_ids: list[str] | None = None,
) -> set[str]:
    """Bound and parallelize independent source fetches for the V2 path only."""
    selected: list[dict[str, Any]] = []
    title_keys: set[str] = set()
    priority_order = {
        _clean(candidate_id): index
        for index, candidate_id in enumerate(priority_candidate_ids or [])
        if _clean(candidate_id)
    }
    ordered = sorted(
        candidates,
        key=lambda item: (
            -priority_order.get(_clean(item.get("candidate_id")), 1_000_000),
            _evidence_priority(item),
        ),
        reverse=True,
    )
    # The tuple sort above keeps ordinary evidence priority descending while
    # preserving the explicit hot-event order ahead of it.
    if priority_order:
        ordered.sort(
            key=lambda item: (
                0 if _clean(item.get("candidate_id")) in priority_order else 1,
                priority_order.get(_clean(item.get("candidate_id")), 1_000_000),
                -_evidence_priority(item),
            )
        )
    for candidate in ordered:
        candidate_id = _clean(candidate.get("candidate_id"))
        title_key = re.sub(r"\W+", "", _title_without_publisher(candidate.get("title")).lower())[:24]
        if not candidate_id or (title_key and title_key in title_keys):
            continue
        if title_key:
            title_keys.add(title_key)
        selected.append(candidate)
        if len(selected) >= max(1, min(int(limit), 30)):
            break
    pending = [
        candidate
        for candidate in selected
        if "evidence_status" not in candidate and candidate.get("discovery_only") is not True
    ]
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 6))) as executor:
            futures = {executor.submit(_enrich_candidate_evidence, candidate): candidate for candidate in pending}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # defensive: the imported worker normally handles its own failures.
                    candidate = futures[future]
                    candidate["evidence_status"] = "failed"
                    candidate["evidence_error"] = _clean(exc)[:200]
    return {_clean(candidate.get("candidate_id")) for candidate in selected}


def prepare_selection_events(research: dict[str, Any], *, evidence_limit: int = 30) -> list[dict[str, Any]]:
    candidates = research.get("candidates") if isinstance(research.get("candidates"), list) else []
    heat_signals = research.get("heat_signals") if isinstance(research.get("heat_signals"), list) else []
    initial_events = cluster_news_events(candidates)
    for event in initial_events:
        event["external_heat_matches"] = _external_heat_matches(event, heat_signals)
    hot_events = sorted(
        (event for event in initial_events if event.get("external_heat_matches")),
        key=lambda event: min(int(item.get("rank") or 999) for item in event["external_heat_matches"]),
    )
    priority_ids: list[str] = []
    for event in hot_events:
        seen_publishers: set[str] = set()
        members = sorted(
            event.get("members") or [],
            key=lambda item: (
                str(item.get("url") or "").startswith("https://news.google.com"),
                -_evidence_priority(item),
            ),
        )
        for member in members:
            publisher = _publisher_key(member)
            if publisher in seen_publishers:
                continue
            seen_publishers.add(publisher)
            priority_ids.append(_clean(member.get("candidate_id")))
            if len(seen_publishers) >= 2:
                break
    prefetch_selection_evidence_v2(
        candidates,
        limit=max(evidence_limit, min(30, len(priority_ids) + 12)),
        priority_candidate_ids=priority_ids,
    )
    # Re-cluster after enrichment so resolved publisher hosts and evidence
    # excerpts become authoritative for gates and independent-source counts.
    events = cluster_news_events(candidates)
    for event in events:
        event["external_heat_matches"] = _external_heat_matches(event, heat_signals)
        event.update(evaluate_event_gates(event))
        event.update(observed_heat(event))
    return sorted(
        events,
        key=lambda item: (
            item["evidence_gate"] == "pass",
            item["risk_gate"] == "pass",
            item["observed_heat_score"],
            len(item.get("evidence_candidate_ids") or []),
        ),
        reverse=True,
    )


def _model_event_packet(event: dict[str, Any]) -> dict[str, Any]:
    members = []
    for member in event.get("members") or []:
        if not isinstance(member, dict):
            continue
        members.append(
            {
                "candidate_id": _clean(member.get("candidate_id")),
                "title": _clean(member.get("title"))[:300],
                "summary": _clean(member.get("summary"))[:500],
                "source_name": _clean(member.get("source_name")),
                "authority": _clean(member.get("authority")),
                "evidence_status": _clean(member.get("evidence_status")),
                "evidence_excerpt": _clean(member.get("evidence_excerpt"))[:900],
            }
        )
    return {
        key: event.get(key)
        for key in (
            "event_id",
            "canonical_title",
            "candidate_ids",
            "evidence_candidate_ids",
            "official_evidence_candidate_ids",
            "independent_publisher_count",
            "independent_evidence_publisher_count",
            "evidence_gate",
            "risk_gate",
            "strong_claim",
            "wording_policy",
            "gate_reasons",
            "observed_heat_score",
            "heat_level",
            "heat_signals",
            "external_heat_matches",
            "discovery_provenance",
        )
    } | {"members": members}


def selection_prompt_v2() -> str:
    return """你是 OpenMontage 面向中国抖音公域的科技快报主编，负责候选事件的市场判断与写稿前资源规划，禁止写主持人台词。
系统已经完成事件去重、证据门、风险门和观测热度计算；你不得修改 heat_level、observed_heat_score、门状态或捏造来源。
你的评分会直接决定最终入选顺序：优先选择已经进入国内平台讨论场、三秒能懂、有现场画面、反差、槽点或全民话题的事件；不要让资料更齐全但用户无感的海外手机参数、小版本更新或厂商通稿压过明显的国内热点。
你必须对输入中的每一个 event_id 输出一条 assessment，且只输出 JSON：
{
  "assessments":[{
    "event_id":"输入中的ID",
    "story_type":"risk|product|research|business|visual_event",
    "content_capacity":"C1|C2|C3|C4",
    "understanding_cost":"U0|U1|U2",
    "editorial_potential_score":0,
    "audience_fit_score":0,
    "visual_potential_score":0,
    "selection_reason":"不超过100字",
    "coverage_plan":[{"dim":"允许维度","claim":"仅复述证据支持的写稿要点，不是台词","source_candidate_ids":["输入候选ID"]}]
  }]
}
规则：
1. C 只表示可靠事实能支撑多少个独立信息维度：C1=1、C2=2、C3=3、C4=4个及以上；同一事实换说法不计数。U 只表示大众理解成本，不能提高 C，也不能救活 C1。
2. coverage_plan 只能使用这些维度：event_core,evidence_detail,mechanism,user_impact,action_tip,industry_value,use_case,constraint,method,limitation,key_number,uncertainty,visual_moment。每个维度最多一次；每条必须绑定本事件 evidence_candidate_ids 内的来源。
3. 风险类优先事件核心/套路或机制/用户影响/行动提示；产品类优先事件核心/差异或证据/使用场景/限制；科研类优先事件核心/方法/结果证据/局限；商业类优先事件核心/关键数字/行业价值/不确定性；视觉事件优先事件核心/视觉瞬间/真实检验/意义。
4. 栏目本质是有趣的科技杂谈，不是严肃新闻联播。平台浏览热度、趣味性、画面感、槽点和观众一听就想接话的程度排在第一位。命中抖音/百度公开热榜的科技事件，应显著提高三项软评分；不能让资料完整但无聊的小众参数稿压过国内热点。
5. editorial_potential_score 评估反差、情绪、讨论空间；audience_fit_score 评估中国抖音普通观众的可感知价值；visual_potential_score 评估是否有动作、人物、现场或鲜明产品画面。三项均为0到100，只做软排序，不能捏造事实。
6. coverage_plan中的公司、人物、核心数字、时间、价格及法律安全结论必须由evidence_excerpt支持。wording_policy为hot_topic_expressive时，可保留已在平台广泛传播且不改变事件本质的“世界纪录、超越人类、最强”等标题性称呼，后续脚本可把它当节目化包装；attribute_rumour仍须明确归因。
7. 没有平台热度、国内关联弱、主要靠价格和参数构成的信息，只能给较低editorial_potential；证据不足就降低 C，只有一个事实维度必须判 C1。
8. U0无需解释；U1需要把已有信息说得更通俗但不增加篇幅；U2后续可规划一个解释槽，但解释槽不能新增事实。
9. 你要做真实的中国短视频市场判断，但不直接输出最终组合、不分配主持人，也不生成标题党台词；最终组合由代码依据你的评分、热度和多样性约束生成。"""


def _validate_assessments(raw: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[str] = []
    assessments = raw.get("assessments") if isinstance(raw.get("assessments"), list) else []
    event_by_id = {_clean(event.get("event_id")): event for event in events}
    rows_by_id: dict[str, dict[str, Any]] = {}
    for row in assessments:
        if not isinstance(row, dict):
            issues.append("assessment 必须是对象")
            continue
        event_id = _clean(row.get("event_id"))
        if event_id not in event_by_id:
            issues.append(f"模型引用了未知事件 {event_id or '[空]'}")
            continue
        if event_id in rows_by_id:
            issues.append(f"事件 {event_id} 被重复评估")
            continue
        rows_by_id[event_id] = row
    missing = sorted(set(event_by_id) - set(rows_by_id))
    if missing:
        issues.append(f"模型遗漏 {len(missing)} 个事件评估")

    normalized: list[dict[str, Any]] = []
    for event_id, event in event_by_id.items():
        row = rows_by_id.get(event_id)
        if not row:
            continue
        story_type = _clean(row.get("story_type"))
        reported_capacity = _clean(row.get("content_capacity")).upper()
        understanding = _clean(row.get("understanding_cost")).upper()
        if story_type not in ALLOWED_STORY_TYPES:
            issues.append(f"{event_id} story_type 无效")
        if understanding not in UNDERSTANDING_EXPLANATION_SLOTS:
            issues.append(f"{event_id} understanding_cost 无效")
        scores: dict[str, int] = {}
        for field in ("editorial_potential_score", "audience_fit_score", "visual_potential_score"):
            value = row.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 100:
                issues.append(f"{event_id} {field} 必须为0到100")
                value = 0
            scores[field] = round(float(value))

        plan = row.get("coverage_plan") if isinstance(row.get("coverage_plan"), list) else []
        normalized_plan: list[dict[str, Any]] = []
        seen_dims: set[str] = set()
        valid_source_ids = set(event.get("evidence_candidate_ids") or [])
        for item in plan:
            if not isinstance(item, dict):
                issues.append(f"{event_id} coverage_plan 项必须是对象")
                continue
            dim = _clean(item.get("dim"))
            claim = _clean(item.get("claim"))
            source_ids = item.get("source_candidate_ids") if isinstance(item.get("source_candidate_ids"), list) else []
            source_ids = list(dict.fromkeys(_clean(value) for value in source_ids if _clean(value)))
            if dim not in ALLOWED_DIMENSIONS:
                issues.append(f"{event_id} 使用了未知信息维度 {dim or '[空]'}")
            if dim in seen_dims:
                issues.append(f"{event_id} 重复使用信息维度 {dim}")
            seen_dims.add(dim)
            if len(claim) < 4:
                issues.append(f"{event_id} {dim or 'coverage'} 缺少有效要点")
            if not source_ids or not set(source_ids) <= valid_source_ids:
                issues.append(f"{event_id} {dim or 'coverage'} 未绑定有效正文证据")
            normalized_plan.append(
                {
                    "claim_id": f"{event_id}-{dim}",
                    "dim": dim,
                    "claim": claim[:240],
                    "source_candidate_ids": source_ids,
                }
            )

        supported_fact_count = len({item["dim"] for item in normalized_plan if item["dim"] in FACT_DIMENSIONS})
        expected_capacity = f"C{min(4, max(1, supported_fact_count))}"
        normalized.append(
            {
                "event_id": event_id,
                "story_type": story_type,
                # Capacity is derived from the validated coverage plan.  A model
                # overrating is a reason to filter this event, not to abort the
                # entire candidate packet.
                "content_capacity": expected_capacity,
                "reported_content_capacity": reported_capacity,
                "capacity_normalized": reported_capacity != expected_capacity,
                "understanding_cost": understanding,
                **scores,
                "selection_reason": _clean(row.get("selection_reason"))[:200],
                "coverage_plan": normalized_plan,
                "supported_fact_dimension_count": supported_fact_count,
            }
        )
    if issues:
        raise NewsSelectionV2ValidationError(issues)
    return normalized


def _low_value_parameter_update(event: dict[str, Any], assessment: dict[str, Any]) -> bool:
    """Identify niche product briefs that are useful backups, not lead stories."""
    contract = " ".join(
        [
            _clean(event.get("canonical_title")),
            *[_clean(item.get("claim")) for item in assessment.get("coverage_plan") or [] if isinstance(item, dict)],
        ]
    )
    parameter_count = len(
        re.findall(
            r"(?i)(?<![a-z0-9])\d+(?:\.\d+)?\s*(?:gb|tb|mah|hz|w|mp|英寸|核|元|美元|新加坡元)|(?<![a-z0-9])[45]g(?![a-z0-9])",
            contract,
        )
    )
    foreign_or_overseas = bool(re.search(r"海外发布|印度|新加坡|东南亚|欧洲市场|海外市场", contract))
    has_public_heat = bool(event.get("external_heat_matches"))
    return (
        assessment.get("story_type") == "product"
        and not has_public_heat
        and foreign_or_overseas
        and parameter_count >= 2
    )


def _editorial_non_event(event: dict[str, Any]) -> bool:
    """Reject evergreen SEO comparisons that are not a dated news event."""
    title = _clean(event.get("canonical_title"))
    return bool(
        re.search(
            r"(?:怎么选|如何选|选购|购买建议|对比评测|横向对比|全面对比|优缺点|值不值得买|\+?FAQ|常见问题)",
            title,
            re.IGNORECASE,
        )
    )


def _selection_rank(event: dict[str, Any], assessment: dict[str, Any]) -> float:
    heat = float(event.get("observed_heat_score") or 0)
    platform_bonus = 12.0 if event.get("external_heat_matches") else 0.0
    low_value_penalty = 32.0 if _low_value_parameter_update(event, assessment) else 0.0
    return round(
        heat * 0.40
        + float(assessment["editorial_potential_score"]) * 0.25
        + float(assessment["audience_fit_score"]) * 0.20
        + float(assessment["visual_potential_score"]) * 0.15
        + platform_bonus
        - low_value_penalty,
        2,
    )


def _classification_corpus(event: dict[str, Any], assessment: dict[str, Any]) -> str:
    return " ".join(
        [
            _clean(event.get("canonical_title")),
            *[
                _clean(item.get("claim"))
                for item in assessment.get("coverage_plan") or []
                if isinstance(item, dict)
            ],
        ]
    )


def _topic_family(event: dict[str, Any], assessment: dict[str, Any]) -> str:
    """Classify audience-perceived subject matter without spending another model call."""
    corpus = _classification_corpus(event, assessment)
    rules = (
        ("robotics", r"人形?机器人|具身(?:智能|AI)|机器狗|机械臂"),
        ("security_policy", r"诈骗|漏洞|泄露|隐私|安全事件|攻击|监管|整治|政策|违法|召回"),
        ("gaming", r"游戏|玩家|实机|预告片|科隆|主机|FPS|Boss|电竞|Steam"),
        ("chips_compute", r"芯片|处理器|显卡|服务器|算力|半导体|SoC|GPU|CPU"),
        ("science_space", r"火箭|航天|卫星|量子|科研|科学|实验室|固态电池|核聚变"),
        ("software_open_source", r"开源|浏览器|操作系统|软件|开发工具|GitHub|Firefox|火狐"),
        ("ai_model", r"大模型|人工智能|AI模型|GPT|OpenAI|DeepSeek|Qwen|通义|智能体"),
        ("internet_platform", r"微信|抖音|快手|微博|平台治理|社交平台|电商平台"),
        ("consumer_tech", r"手机|电脑|耳机|平板|Mac|iPhone|iPad|折叠屏|汽车|车机|家电"),
    )
    for family, pattern in rules:
        if re.search(pattern, corpus, re.IGNORECASE):
            return family
    return "other"


def _event_form(event: dict[str, Any], assessment: dict[str, Any]) -> str:
    corpus = _classification_corpus(event, assessment)
    rules = (
        ("risk_incident", r"诈骗|漏洞|泄露|事故|召回|攻击|处罚|风险"),
        ("price_change", r"涨价|降价|调价|直降|免费|提价|优惠"),
        ("financing", r"融资|募资|估值|投资方|战略投资|并购"),
        ("visual_record", r"百米|100\s*米|比赛|运动会|现场|挑战|跑出|夺冠|纪录"),
        ("rumor", r"爆料|消息称|曝出|曝光|据称|传闻|知情人士"),
        ("trailer_announcement", r"实机|预告片|预告|配音阵容|科隆游戏展|演示公布"),
        ("product_update", r"新增支持|版本更新|升级|内测|功能更新|兼容"),
        ("breakthrough", r"突破|首个|首次|刷新|量产|标准立项|研发完成"),
        ("product_launch", r"发布|亮相|推出|开售|预订|上线|官宣"),
    )
    for form, pattern in rules:
        if re.search(pattern, corpus, re.IGNORECASE):
            return form
    fallback = {
        "risk": "risk_incident",
        "visual_event": "visual_record",
        "research": "breakthrough",
        "business": "financing",
        "product": "product_launch",
    }
    return fallback.get(_clean(assessment.get("story_type")), "product_launch")


def _classified_pair(
    pair: tuple[dict[str, Any], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    event, assessment = pair
    enriched = dict(assessment)
    enriched["topic_family"] = _topic_family(event, assessment)
    enriched["event_form"] = _event_form(event, assessment)
    return event, enriched


def _episode_score(
    pairs: tuple[tuple[dict[str, Any], dict[str, Any]], ...] | list[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[float, dict[str, Any]]:
    """Score a complete episode; a weak third story may reduce, not inflate, quality."""
    rows = list(pairs)
    if not rows:
        return 0.0, {"blocking_issues": ["empty_episode"]}
    individual = [_selection_rank(event, assessment) for event, assessment in rows]
    families = [_clean(assessment.get("topic_family")) or _topic_family(event, assessment) for event, assessment in rows]
    forms = [_clean(assessment.get("event_form")) or _event_form(event, assessment) for event, assessment in rows]
    public_heat_count = sum(bool(event.get("external_heat_matches")) for event, _assessment in rows)
    high_heat_count = sum(event.get("heat_level") in {"H3", "H4"} for event, _assessment in rows)
    h1_count = sum(event.get("heat_level") == "H1" for event, _assessment in rows)
    h1_family_forms = Counter(
        (families[index], forms[index])
        for index, (event, _assessment) in enumerate(rows)
        if event.get("heat_level") == "H1"
    )
    duplicate_h1_count = sum(max(0, count - 1) for count in h1_family_forms.values())
    weak_announcement_count = sum(
        event.get("heat_level") == "H1"
        and not event.get("external_heat_matches")
        and forms[index] in {"trailer_announcement", "product_update"}
        for index, (event, _assessment) in enumerate(rows)
    )
    weak_rumor_count = sum(
        event.get("heat_level") == "H1"
        and not event.get("external_heat_matches")
        and forms[index] == "rumor"
        for index, (event, _assessment) in enumerate(rows)
    )
    low_value_count = sum(_low_value_parameter_update(event, assessment) for event, assessment in rows)
    unique_family_count = len(set(families))
    unique_form_count = len(set(forms))
    has_visual_mix = "visual_record" in forms and any(form != "visual_record" for form in forms)
    score = (
        sum(individual) / len(individual)
        + max(0, unique_family_count - 1) * 8.0
        + max(0, unique_form_count - 1) * 4.0
        + min(1, public_heat_count) * 10.0
        + min(1, high_heat_count) * 5.0
        + max(0, len(rows) - 2) * 4.0
        + (4.0 if has_visual_mix else 0.0)
        - duplicate_h1_count * 18.0
        - max(0, weak_announcement_count - 1) * 12.0
        - weak_rumor_count * 18.0
        - low_value_count * 14.0
    )
    blocking_issues: list[str] = []
    # Diversity is a public-feed guard, not a reason to discard a genuinely
    # strong same-domain day.  Only a mostly H1/H2 narrow portfolio is blocked;
    # several independently strong H3/H4 stories may still form one episode.
    if len(rows) >= 3 and unique_family_count < 2 and high_heat_count < 2 and h1_count > 1:
        blocking_issues.append("episode_topic_family_too_narrow")
    if duplicate_h1_count:
        blocking_issues.append("duplicate_h1_family_and_form")
    if high_heat_count and weak_announcement_count > 1:
        blocking_issues.append("high_heat_lead_followed_by_weak_announcements")
    if low_value_count > 1:
        blocking_issues.append("too_many_low_value_parameter_updates")
    return round(score, 2), {
        "topic_families": families,
        "event_forms": forms,
        "topic_family_count": unique_family_count,
        "event_form_count": unique_form_count,
        "public_heat_count": public_heat_count,
        "high_heat_count": high_heat_count,
        "duplicate_h1_count": duplicate_h1_count,
        "weak_announcement_count": weak_announcement_count,
        "weak_rumor_count": weak_rumor_count,
        "low_value_parameter_count": low_value_count,
        "blocking_issues": blocking_issues,
    }


def _lead_rank(event: dict[str, Any], assessment: dict[str, Any]) -> float:
    """Rank only the opening slot; selection quality and hook value are different jobs."""
    contract = " ".join(
        [
            _clean(event.get("canonical_title")),
            *[_clean(item.get("claim")) for item in assessment.get("coverage_plan") or [] if isinstance(item, dict)],
        ]
    )
    direct_public_value = 15.0 if re.search(r"国补|补贴|降价|涨价|直降|诈骗|泄露|风险|召回|免费|省钱", contract) else 0.0
    platform_value = 24.0 if event.get("external_heat_matches") else 0.0
    return round(
        float(assessment["audience_fit_score"]) * 0.30
        + float(assessment["editorial_potential_score"]) * 0.20
        + float(assessment.get("visual_potential_score") or 0) * 0.25
        + float(event.get("observed_heat_score") or 0) * 0.10
        + direct_public_value
        + platform_value,
        2,
    )


def _selection_fact_overlap(
    left: tuple[dict[str, Any], dict[str, Any]],
    right: tuple[dict[str, Any], dict[str, Any]],
) -> bool:
    """Detect the same factual event surviving title-level clustering.

    Headlines can frame one launch as an executive quote, an investment story,
    or a product announcement.  Shared distinctive model identifiers across
    frozen claims are stronger evidence of duplication than headline wording.
    """
    def contract(pair: tuple[dict[str, Any], dict[str, Any]]) -> str:
        event, assessment = pair
        return " ".join(
            [
                _clean(event.get("canonical_title")),
                *[_clean(item.get("claim")) for item in assessment.get("coverage_plan") or []],
            ]
        )

    left_text, right_text = contract(left), contract(right)
    identifier_re = re.compile(
        r"(?i)(?<![a-z0-9])(?=[a-z0-9.+-]*\d)[a-z][a-z0-9.+-]{1,}(?![a-z0-9])"
    )
    left_ids = {token.lower() for token in identifier_re.findall(left_text)}
    right_ids = {token.lower() for token in identifier_re.findall(right_text)}
    shared_ids = left_ids & right_ids
    if len(shared_ids) >= 2:
        return True
    if not shared_ids:
        return False

    generic_bigrams = {
        "公司", "集团", "发布", "科技", "产品", "芯片", "模型", "研发",
        "投入", "支持", "国内", "首款", "新一", "一代", "正式", "宣布",
    }

    def chinese_bigrams(text: str) -> set[str]:
        return {
            chunk[index:index + 2]
            for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text)
            for index in range(len(chunk) - 1)
            if chunk[index:index + 2] not in generic_bigrams
        }

    return bool(chinese_bigrams(left_text) & chinese_bigrams(right_text))


def _story_record(
    event: dict[str, Any],
    assessment: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    max_fact_lines = CAPACITY_LINES[assessment["content_capacity"]]
    explanation_slots = UNDERSTANDING_EXPLANATION_SLOTS[assessment["understanding_cost"]]
    heat_unit_cap = (
        4 if index == 1 and event.get("heat_level") in {"H4", "H3"}
        else 3 if event.get("heat_level") != "H1"
        else 2
    )
    planned_units = min(heat_unit_cap, max_fact_lines + explanation_slots)
    official_image = _first_official_image(event)
    topic_family = _clean(assessment.get("topic_family")) or _topic_family(event, assessment)
    event_form = _clean(assessment.get("event_form")) or _event_form(event, assessment)
    return {
        "selection_id": f"S{index:02d}" if index > 0 else "",
        "event_id": event["event_id"],
        "canonical_title": event["canonical_title"],
        "candidate_ids": event["candidate_ids"],
        "evidence_candidate_ids": event["evidence_candidate_ids"],
        "evidence_gate": event["evidence_gate"],
        "risk_gate": event["risk_gate"],
        "heat_level": event["heat_level"],
        "observed_heat_score": event["observed_heat_score"],
        "heat_signals": event["heat_signals"],
        "external_heat_matches": event.get("external_heat_matches") or [],
        "public_heat": bool(event.get("external_heat_matches")),
        "wording_policy": event.get("wording_policy") or "verified_facts",
        "story_type": assessment["story_type"],
        "topic_family": topic_family,
        "event_form": event_form,
        "content_capacity": assessment["content_capacity"],
        "understanding_cost": assessment["understanding_cost"],
        "max_fact_lines": max_fact_lines,
        "explanation_slots": explanation_slots,
        "allocated_planning_units": planned_units,
        "editorial_potential_score": assessment["editorial_potential_score"],
        "audience_fit_score": assessment["audience_fit_score"],
        "visual_potential_score": assessment["visual_potential_score"],
        "selection_score": _selection_rank(event, assessment),
        "selection_reason": assessment["selection_reason"],
        "editorial_flags": [
            *(["platform_hot"] if event.get("external_heat_matches") else []),
            *(["niche_overseas_parameter_update"] if _low_value_parameter_update(event, assessment) else []),
            *(["narrow_strong_wording"] if event.get("wording_policy") == "narrow_unverified_superlative" else []),
        ],
        "official_image_url": official_image.get("url", ""),
        "official_image_attribution": official_image.get("attribution", ""),
        "coverage_plan": assessment["coverage_plan"][:planned_units],
        "available_coverage_plan": assessment["coverage_plan"],
        "heat_unit_cap": heat_unit_cap,
    }


def _ordered_episode_pairs(
    pairs: tuple[tuple[dict[str, Any], dict[str, Any]], ...] | list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows = list(pairs)
    lead = max(rows, key=lambda pair: _lead_rank(*pair))
    rest = sorted(
        (pair for pair in rows if pair is not lead),
        key=lambda pair: (_selection_rank(*pair), pair[0].get("observed_heat_score") or 0),
        reverse=True,
    )
    return [lead, *rest]


def _rank_episode_combinations(
    eligible: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return bounded, auditable episode portfolios including compact fallbacks."""
    pool = [_classified_pair(pair) for pair in eligible[:10]]
    if len(pool) < 2:
        return []
    max_size = min(4, len(pool))
    candidates: list[dict[str, Any]] = []
    for size in range(max_size, 1, -1):
        for combo in combinations(pool, size):
            score, diagnostics = _episode_score(combo)
            if size == 4:
                low_heat_marginals = [
                    score - _episode_score(tuple(row for row in combo if row is not pair))[0]
                    for pair in combo
                    if pair[0].get("heat_level") in {"H1", "H2"} and not pair[0].get("external_heat_matches")
                ]
                marginal_floor = min(low_heat_marginals) if low_heat_marginals else None
                diagnostics["minimum_low_heat_story_marginal"] = (
                    round(marginal_floor, 2) if marginal_floor is not None else None
                )
                # Four stories are useful only when the fourth earns its
                # screen time.  A low-heat tail that adds less than eight
                # portfolio points usually creates duplicate subject matter,
                # English-term overhead and duration padding.  Keep the
                # stronger three-story version instead of forcing 90 seconds.
                if marginal_floor is not None and marginal_floor < 8.0:
                    diagnostics["blocking_issues"].append("low_marginal_fourth_story")
            ordered = _ordered_episode_pairs(combo)
            digest = hashlib.sha256(
                "|".join(event["event_id"] for event, _assessment in ordered).encode("utf-8")
            ).hexdigest()[:12]
            candidates.append(
                {
                    "combination_id": f"EC-{digest.upper()}",
                    "episode_score": score,
                    "story_count": size,
                    "duration_profile": "compact_high_value" if size == 2 else "full_episode",
                    "diagnostics": diagnostics,
                    "pairs": ordered,
                }
            )
    valid = [item for item in candidates if not item["diagnostics"]["blocking_issues"]]
    ranked = sorted(
        valid or candidates,
        key=lambda item: (
            not item["diagnostics"]["blocking_issues"],
            item["diagnostics"]["public_heat_count"],
            item["diagnostics"]["high_heat_count"],
            item["episode_score"],
            item["story_count"],
        ),
        reverse=True,
    )
    results: list[dict[str, Any]] = []
    seen_event_sets: set[tuple[str, ...]] = set()
    for item in ranked:
        event_set = tuple(sorted(event["event_id"] for event, _assessment in item["pairs"]))
        if event_set in seen_event_sets:
            continue
        seen_event_sets.add(event_set)
        full_score = float(item["episode_score"])
        marginal: dict[str, float] = {}
        for event, _assessment in item["pairs"]:
            reduced = tuple(pair for pair in item["pairs"] if pair[0]["event_id"] != event["event_id"])
            reduced_score = _episode_score(reduced)[0] if reduced else 0.0
            marginal[event["event_id"]] = round(full_score - reduced_score, 2)
        weakest_first = sorted(marginal, key=lambda event_id: (marginal[event_id], event_id))
        replacement_priority = {event_id: index + 1 for index, event_id in enumerate(weakest_first)}
        stories = []
        for index, (event, assessment) in enumerate(item["pairs"], 1):
            story = _story_record(event, assessment, index=index)
            story["marginal_contribution"] = marginal[event["event_id"]]
            story["replacement_priority"] = replacement_priority[event["event_id"]]
            stories.append(story)
        results.append(
            {
                "combination_id": item["combination_id"],
                "rank": len(results) + 1,
                "episode_score": item["episode_score"],
                "story_count": item["story_count"],
                "duration_profile": item["duration_profile"],
                "event_ids": [story["event_id"] for story in stories],
                "topic_families": list(dict.fromkeys(story["topic_family"] for story in stories)),
                "event_forms": list(dict.fromkeys(story["event_form"] for story in stories)),
                "blocking_issues": item["diagnostics"]["blocking_issues"],
                "selected_stories": stories,
            }
        )
        if len(results) >= max(1, int(limit)):
            break
    return results


def build_selection_result(
    research: dict[str, Any],
    events: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    *,
    model: str,
    provider: str = "default",
    preferred_provider: str | None = None,
    provider_fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_by_id = {_clean(event.get("event_id")): event for event in events}
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    assessed_ids = {_clean(item.get("event_id")) for item in assessments}
    rejections: list[dict[str, Any]] = [
        {
            "event_id": event["event_id"],
            "reasons": [
                *(["evidence_gate_not_pass"] if event.get("evidence_gate") != "pass" else []),
                *(["risk_gate_not_pass"] if event.get("risk_gate") != "pass" else []),
                *event.get("gate_reasons", []),
            ],
        }
        for event in events
        if event["event_id"] not in assessed_ids
        and (event.get("evidence_gate") != "pass" or event.get("risk_gate") != "pass")
    ]
    for assessment in assessments:
        event = event_by_id[assessment["event_id"]]
        reasons: list[str] = []
        if event.get("evidence_gate") != "pass":
            reasons.append("evidence_gate_not_pass")
        if event.get("risk_gate") != "pass":
            reasons.append("risk_gate_not_pass")
        if assessment.get("content_capacity") == "C1":
            reasons.append("content_capacity_c1")
        if _editorial_non_event(event):
            reasons.append("editorial_comparison_not_news_event")
        if reasons:
            rejections.append({"event_id": event["event_id"], "reasons": reasons})
        else:
            eligible.append((event, assessment))

    if len(eligible) < 2:
        raise NewsSelectionV2ValidationError([f"通过两道门且容量达到C2的事件只有 {len(eligible)} 条，至少需要2条"])
    eligible.sort(key=lambda pair: (_selection_rank(*pair), pair[0]["observed_heat_score"]), reverse=True)
    raw_eligible_count = len(eligible)
    unique_eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pair in eligible:
        duplicate_of = next(
            (kept[0]["event_id"] for kept in unique_eligible if _selection_fact_overlap(pair, kept)),
            "",
        )
        if duplicate_of:
            rejections.append(
                {
                    "event_id": pair[0]["event_id"],
                    "reasons": ["duplicate_frozen_fact_event"],
                    "duplicate_of": duplicate_of,
                }
            )
            continue
        unique_eligible.append(pair)
    eligible = unique_eligible
    if len(eligible) < 2:
        raise NewsSelectionV2ValidationError(
            [f"事实去重后达到C2的独立事件只有 {len(eligible)} 条，继续从后续候选补位"]
        )
    episode_combinations = _rank_episode_combinations(eligible, limit=3)
    if not episode_combinations:
        raise NewsSelectionV2ValidationError(["没有形成可用的整期候选组合"])
    selected = episode_combinations[0]["selected_stories"]

    planned_units_total = sum(item["allocated_planning_units"] for item in selected)
    # Nine units is a useful 90-second planning target, not a publication
    # gate.  Rejecting an otherwise strong episode here pressures the selector
    # to add a low-value parameter story or the writer to repeat itself.  A
    # compact, high-value brief is preferable and remains subject to the
    # independent script-quality review.
    duration_profile = "full_90s" if planned_units_total >= 9 else "compact_high_value"
    planning_warnings = (
        []
        if planned_units_total >= 9
        else [f"仅有 {planned_units_total} 个可靠信息单元；允许自然缩短，禁止以低价值选题或重复台词凑时长"]
    )

    all_h = [item["heat_level"] for item in selected]
    weak_h1_rumor_count = sum(
        item.get("heat_level") == "H1"
        and not item.get("external_heat_matches")
        and item.get("event_form") == "rumor"
        for item in selected
    )
    selected_event_ids = {item["event_id"] for item in selected}
    hot_rescue_queue = [
        {
            "event_id": event["event_id"],
            "canonical_title": event["canonical_title"],
            "external_heat_matches": event.get("external_heat_matches") or [],
            "evidence_gate": event.get("evidence_gate"),
            "risk_gate": event.get("risk_gate"),
            "gate_reasons": event.get("gate_reasons") or [],
        }
        for event in events
        if event.get("external_heat_matches")
        and event["event_id"] not in selected_event_ids
        and (event.get("evidence_gate") != "pass" or event.get("risk_gate") != "pass")
    ]
    selected_families = {story["topic_family"] for story in selected}
    qualified_backups = []
    for event, raw_assessment in eligible:
        if event["event_id"] in selected_event_ids:
            continue
        _event, assessment = _classified_pair((event, raw_assessment))
        story = _story_record(event, assessment, index=0)
        story["platform_hot"] = bool(event.get("external_heat_matches"))
        story["low_value_parameter_update"] = _low_value_parameter_update(event, assessment)
        story["difference_fit_score"] = round(
            story["selection_score"]
            + (12.0 if story["topic_family"] not in selected_families else -10.0)
            + (8.0 if story["platform_hot"] else 0.0)
            - (14.0 if story["low_value_parameter_update"] else 0.0),
            2,
        )
        qualified_backups.append(story)
    qualified_backups.sort(key=lambda item: (item["difference_fit_score"], item["selection_score"]), reverse=True)
    qualified_backups = qualified_backups[:8]
    return {
        "version": OUTPUT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "target_date": _clean(research.get("target_date")),
        "generated_at": _now(),
        "model": model,
        "provider": provider,
        "selection_audit": {
            "role": "china_short_video_editor",
            "preferred_provider": preferred_provider or provider,
            "actual_provider": provider,
            "provider_fallback": provider_fallback,
            "model": model,
        },
        "mode": "selection_only_no_script",
        "selected_stories": selected,
        "selection_summary": {
            "selected_count": len(selected),
            "selected_combination_id": episode_combinations[0]["combination_id"],
            "episode_score": episode_combinations[0]["episode_score"],
            "episode_combination_count": len(episode_combinations),
            "planned_units_total": planned_units_total,
            "duration_profile": duration_profile,
            "planning_warnings": planning_warnings,
            "heat_distribution": {level: all_h.count(level) for level in ("H4", "H3", "H2", "H1")},
            "public_heat_selected_count": sum(bool(item.get("external_heat_matches")) for item in selected),
            "low_value_parameter_selected_count": sum("niche_overseas_parameter_update" in item.get("editorial_flags", []) for item in selected),
            "weak_h1_rumor_selected_count": weak_h1_rumor_count,
            "fixed_heat_or_capacity_combination_used": False,
            "rescue_recommended": bool(
                len(selected) == 2
                or episode_combinations[0].get("blocking_issues")
                or weak_h1_rumor_count
            ),
        },
        "episode_combinations": episode_combinations,
        "funnel": {
            "candidate_count": len(research.get("candidates") or []),
            "clustered_event_count": len(events),
            "model_assessed_event_count": len(assessments),
            "raw_eligible_event_count": raw_eligible_count,
            "eligible_event_count": len(eligible),
            "selected_event_count": len(selected),
            "hot_rescue_queue": hot_rescue_queue,
            "qualified_backups": qualified_backups,
            "rejections": rejections,
        },
        "validation": {"valid": True, "contract": PROMPT_VERSION},
    }


def select_daily_news_v2(
    research: dict[str, Any],
    *,
    max_revision_rounds: int = 1,
    model_events_limit: int = MAX_MODEL_EVENTS,
) -> dict[str, Any]:
    candidates = research.get("candidates") if isinstance(research.get("candidates"), list) else []
    if len(candidates) < 3:
        raise DailyAutomationError("指定日期内候选少于3条，无法进行V2选题")
    events = prepare_selection_events(research)
    pass_events = [event for event in events if event["evidence_gate"] == "pass" and event["risk_gate"] == "pass"]
    if len(pass_events) < 3:
        raise NewsSelectionV2ValidationError([f"证据门和风险门均通过的事件只有 {len(pass_events)} 条，至少需要3条"])
    batch_size = max(3, min(int(model_events_limit), MAX_MODEL_EVENTS))
    accumulated_assessments: list[dict[str, Any]] = []
    last_issues: list[str] = []
    model = ""
    preferred_provider = daily_editorial_provider()
    active_provider = preferred_provider
    provider_fallback: dict[str, Any] | None = None
    best_result: dict[str, Any] | None = None
    rescue_rounds_used = 0
    for offset in range(0, len(pass_events), batch_size):
        packet_events = pass_events[offset:offset + batch_size]
        payload: dict[str, Any] = {
            "target_date": research.get("target_date"),
            "prompt_version": PROMPT_VERSION,
            "events": [_model_event_packet(event) for event in packet_events],
        }
        batch_assessments: list[dict[str, Any]] | None = None
        last_issues = []
        for revision in range(max(0, int(max_revision_rounds)) + 1):
            if last_issues:
                payload["validation_errors_to_fix"] = last_issues
                payload["revision_round"] = revision
            try:
                raw, model = _chat_json(
                    selection_prompt_v2(),
                    payload,
                    timeout_seconds=180,
                    temperature=0.0,
                    provider=active_provider,
                )
            except TextAIError as exc:
                transient = re.search(
                    r"超时|HTTP 50[234]|连接|中断|premature|ChunkedEncoding|ProxyError|RemoteDisconnected",
                    str(exc),
                    re.IGNORECASE,
                )
                if transient and revision < max_revision_rounds:
                    last_issues = ["上轮服务暂时不可用，请按原结构重新输出"]
                    continue
                if active_provider == "doubao":
                    # Doubao is the editorial owner. Luna/default is allowed
                    # only as an auditable availability fallback; it must not
                    # overrule a valid Doubao market judgement.
                    provider_fallback = {
                        "from": "doubao",
                        "to": "default",
                        "reason": str(exc)[:300],
                        "revision_round": revision,
                    }
                    active_provider = "default"
                    raw, model = _chat_json(
                        selection_prompt_v2(),
                        payload,
                        timeout_seconds=180,
                        temperature=0.0,
                        provider=active_provider,
                    )
                else:
                    raise
            try:
                batch_assessments = _validate_assessments(raw, packet_events)
                break
            except NewsSelectionV2ValidationError as exc:
                last_issues = exc.issues
        if batch_assessments is None:
            raise NewsSelectionV2ValidationError(last_issues or ["V2选题未通过结构化校验"])
        accumulated_assessments.extend(batch_assessments)
        try:
            result = build_selection_result(
                research,
                events,
                accumulated_assessments,
                model=model,
                provider=active_provider,
                preferred_provider=preferred_provider,
                provider_fallback=provider_fallback,
            )
            if (
                best_result is None
                or float((result.get("selection_summary") or {}).get("episode_score") or 0)
                > float((best_result.get("selection_summary") or {}).get("episode_score") or 0)
            ):
                best_result = result
            has_more = offset + batch_size < len(pass_events)
            rescue_recommended = bool((result.get("selection_summary") or {}).get("rescue_recommended"))
            if rescue_recommended and has_more and rescue_rounds_used < 1:
                # Do not rerun the whole research stage. Assess at most one
                # additional evidence-approved batch so a narrow first batch
                # can gain a genuinely different replacement candidate.
                rescue_rounds_used += 1
                continue
            summary = result.setdefault("selection_summary", {})
            summary["rescue_rounds_used"] = rescue_rounds_used
            summary["rescue_mode"] = "remaining_evidence_pool" if rescue_rounds_used else "not_needed"
            return result
        except NewsSelectionV2ValidationError as exc:
            # A valid batch may simply contain low-capacity material. Continue
            # with the next evidence-approved batch instead of stopping the day.
            last_issues = exc.issues
            if offset + batch_size >= len(pass_events):
                raise
    if best_result is not None:
        summary = best_result.setdefault("selection_summary", {})
        summary["rescue_rounds_used"] = rescue_rounds_used
        summary["rescue_mode"] = "remaining_evidence_pool" if rescue_rounds_used else "not_needed"
        return best_result
    raise NewsSelectionV2ValidationError(last_issues or ["通过筛选的高价值素材不足"])


def _run_dir(target: date | str) -> Path:
    target_value = date.fromisoformat(target) if isinstance(target, str) else target
    return RUNS_ROOT / target_value.isoformat()


def run_news_selection_v2(target: date | str, *, trigger: str = "manual") -> dict[str, Any]:
    """Collect/reuse research and persist V2 without touching legacy artifacts."""
    target_value = date.fromisoformat(target) if isinstance(target, str) else target
    run_dir = _run_dir(target_value)
    research_path = run_dir / "news_research.json"
    audited_research_path = run_dir / "news_research_v2.json"
    selection_path = run_dir / "topic_selection_v2.json"
    run_path = run_dir / "news_selection_v2_run.json"
    manifest: dict[str, Any] = {
        "version": OUTPUT_VERSION,
        "target_date": target_value.isoformat(),
        "trigger": _clean(trigger) or "manual",
        "started_at": _now(),
        "status": "running",
        "research_artifact": str(research_path),
        "audited_research_artifact": str(audited_research_path),
        "selection_artifact": str(selection_path),
        "legacy_topic_selection_untouched": True,
    }
    _atomic_json(run_path, manifest)
    try:
        # Reuse the enriched V2 evidence packet on same-day editorial reruns.
        # If the raw collector artifact is newer, prefer it so genuinely new
        # candidates are not hidden by an older audit snapshot.
        audited_is_current = (
            audited_research_path.is_file()
            and (
                not research_path.is_file()
                or audited_research_path.stat().st_mtime >= research_path.stat().st_mtime
            )
        )
        research = _read_json(audited_research_path if audited_is_current else research_path)
        if not research:
            research = collect_news_candidates(target_value)
            _atomic_json(research_path, research)
        result = select_daily_news_v2(research)
        # Evidence enrichment mutates the in-memory copy.  Persist it separately so
        # V2 remains auditable without changing the legacy script input artifact.
        _atomic_json(audited_research_path, research)
        _atomic_json(selection_path, result)
        manifest.update(
            {
                "finished_at": _now(),
                "status": "succeeded",
                "result_summary": result["selection_summary"],
            }
        )
        _atomic_json(run_path, manifest)
        return {"run": manifest, "selection": result}
    except Exception as exc:
        manifest.update({"finished_at": _now(), "status": "failed", "error": _clean(exc)[:500]})
        _atomic_json(run_path, manifest)
        raise


def read_news_selection_v2(target: date | str) -> dict[str, Any] | None:
    return _read_json(_run_dir(target) / "topic_selection_v2.json")


def read_news_selection_v2_run(target: date | str) -> dict[str, Any] | None:
    return _read_json(_run_dir(target) / "news_selection_v2_run.json")
