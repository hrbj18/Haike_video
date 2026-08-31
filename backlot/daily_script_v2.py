"""Generate an auditable dual-host test script from news-selection V2."""

from __future__ import annotations

import json
import hashlib
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from backlot.ai_text import TextAIError, _atomic_write_text, _chat_json, daily_editorial_provider
from backlot.daily_automation import DailyAutomationError, RUNS_ROOT, _atomic_json, _now, _read_json


PROMPT_VERSION = "daily-script-from-selection-v3.1-doubao-conversation-style"
SCRIPT_REPAIR_RULE_VERSION = "deterministic-line-trim-v4"
TARGET_LINE_MAX = 42
HARD_LINE_MAX = 70
TARGET_TOTAL_MAX = 420
HARD_TOTAL_MIN = 280
HARD_TOTAL_MAX = 600
EDITORIAL_DURATION_MAX_CHARS = 520
SPEAKERS = ("yaya", "mengmeng")
FUNCTIONS = {"hook", "fact", "question", "answer", "plain_explain", "impact", "constraint", "quip", "closing"}
NARROWED_HYPE_PATTERN = re.compile(
    r"快过人类|超越人类|(?:打破|再破|刷新).{0,12}纪录|夺冠|全场最快|世界纪录|人类纪录"
)
UNGROUNDED_COMPARISON_PATTERN = re.compile(
    r"比(?:之前|上一代|原来).{0,10}(?:更|高|低|快|慢|贵|便宜|提升|下降)|"
    r"门槛比之前|(?:拉高|抬高).{0,6}门槛|门槛.{0,6}(?:拉高|抬高)"
)
UNGROUNDED_RELEASE_STATUS_PATTERN = re.compile(r"(?:已经|已)(?:开卖|开售|发售|上市)")
UNGROUNDED_REMOTE_PATTERN = re.compile(r"(?:没有|无需|不靠|全程没有)(?:人工)?遥控")
UNGROUNDED_EPISODE_METADATA_PATTERN = re.compile(
    r"涨价|降价|破纪录|夺冠|快过人类|第一次|首次|首个|首款|首秀|已经开售|已经开卖"
)
UNGROUNDED_HEADLINE_HYPE_PATTERN = re.compile(r"第一次|首次|首个|首款|首秀|唯一|史上最|全球最|世界最")

# 黄金范例库：把人工确认过、传播效果好的成稿（例如豆包网页版写出的满意稿）
# 存成 JSON 后，作为 few-shot 注入写稿 prompt，让模型照葫芦画瓢地学节奏。
GOLDEN_SCRIPTS_DIR = Path(__file__).resolve().parent / "golden_scripts"


class DailyScriptV2ValidationError(DailyAutomationError):
    def __init__(self, issues: list[str]):
        self.issues = list(dict.fromkeys(issue for issue in issues if issue))
        super().__init__("；".join(self.issues))


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalized_number_tokens(text: Any) -> set[str]:
    """Compare factual numbers by value while preserving percent semantics."""
    normalized: set[str] = set()
    for raw in re.findall(r"\d+(?:\.\d+)?%?", _clean(text)):
        percent = raw.endswith("%")
        value = raw[:-1] if percent else raw
        if "." in value:
            value = value.rstrip("0").rstrip(".") or "0"
        value = value.lstrip("0") or "0"
        normalized.add(value + ("%" if percent else ""))
    return normalized


def _trim_complete_suffix(text: Any, *, maximum: int = HARD_LINE_MAX) -> str:
    """Delete only a complete suffix at existing punctuation."""
    value = _clean(text)
    if len(value) <= maximum:
        return value
    sentence_boundaries = [
        match.end()
        for match in re.finditer(r"[。！？!?；;]", value)
        if 14 <= match.end() <= maximum
    ]
    phrase_boundaries = [
        match.end()
        for match in re.finditer(r"[，,]", value)
        if 14 <= match.end() <= maximum
    ]
    boundaries = sentence_boundaries or phrase_boundaries
    return value[:max(boundaries)].rstrip("，,；; ") if boundaries else value


def _writer_provider() -> str:
    """豆包负责中文台词；未配置时回落默认模型。"""
    return daily_editorial_provider()


def _reviewer_provider() -> str:
    """豆包用独立请求冷审中国短视频传播质量。"""
    return daily_editorial_provider()


def _anonymize_golden_text(value: Any, redact_terms: list[str]) -> str:
    text = _clean(value)
    for term in sorted({_clean(item) for item in redact_terms if _clean(item)}, key=len, reverse=True):
        text = text.replace(term, "[对象]")
    text = re.sub(r"(?<![A-Za-z])[A-Za-z][A-Za-z0-9.+/-]*(?:\s+[A-Za-z0-9.+/-]+)*(?![A-Za-z])", "[型号]", text)
    text = re.sub(r"\d+(?:\.\d+)?(?:%|％|亿|万|元|美元|年|月|日|纳米|分)?", "[数字]", text)
    text = re.sub(r"(?:\[对象\]){2,}", "[对象]", text)
    text = re.sub(r"(?:\[型号\]){2,}", "[型号]", text)
    return text


def _validated_golden_example(path: Path) -> tuple[dict[str, Any] | None, str]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return None, "invalid_json"
    if data.get("approved") is not True or _clean(data.get("approved_by")).lower() != "user":
        return None, "not_user_approved"
    example_id = _clean(data.get("id") or path.stem)
    approved_at = _clean(data.get("approved_at"))
    source_kind = _clean(data.get("source_kind"))
    lines = data.get("lines") if isinstance(data.get("lines"), list) else []
    if not example_id or not approved_at or not source_kind or not 4 <= len(lines) <= 20:
        return None, "incomplete_metadata"
    redact_terms = [_clean(item) for item in data.get("redact_terms") or [] if _clean(item)]
    cleaned_lines: list[dict[str, str]] = []
    previous_speaker = ""
    for line in lines:
        if not isinstance(line, dict):
            return None, "invalid_line"
        speaker = _clean(line.get("speaker_name"))
        text = _clean(line.get("text"))
        if speaker not in {"雅雅", "檬檬"} or not text or speaker == previous_speaker:
            return None, "invalid_role_alternation"
        cleaned_lines.append({"speaker_name": speaker, "text": _anonymize_golden_text(text, redact_terms)})
        previous_speaker = speaker
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "id": example_id,
        "title": _anonymize_golden_text(data.get("title"), redact_terms),
        "approved_at": approved_at,
        "source_kind": source_kind,
        "sha256": digest,
        "lines": cleaned_lines,
    }, "approved"


def _load_golden_examples(limit: int = 3) -> list[dict[str, Any]]:
    directory = GOLDEN_SCRIPTS_DIR
    examples: list[dict[str, Any]] = []
    if not directory.is_dir():
        return examples
    for path in sorted(directory.glob("*.json")):
        if len(examples) >= limit:
            break
        example, _reason = _validated_golden_example(path)
        if example:
            examples.append(example)
    return examples


def golden_script_status(limit: int = 3) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    for path in sorted(GOLDEN_SCRIPTS_DIR.glob("*.json")) if GOLDEN_SCRIPTS_DIR.is_dir() else []:
        example, reason = _validated_golden_example(path)
        rows.append({
            "file": path.name,
            "status": reason,
            "id": _clean((example or {}).get("id")),
            "sha256": _clean((example or {}).get("sha256")),
        })
    loaded = [row for row in rows if row["status"] == "approved"][:max(0, int(limit))]
    return {
        "available_count": len([row for row in rows if row["status"] == "approved"]),
        "loaded_count": len(loaded),
        "loaded": loaded,
        "ignored": [row for row in rows if row["status"] != "approved"],
    }


def _script_slots(selection: dict[str, Any]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    speaker_index = 0
    for story in selection.get("selected_stories") or []:
        count = int(story.get("allocated_planning_units") or 0)
        coverage = [item for item in story.get("coverage_plan") or [] if item.get("claim_id")]
        claims = [item.get("claim_id") for item in coverage]
        dimension_by_claim = {item.get("claim_id"): item.get("dim") for item in coverage}
        claim_groups: list[list[str]] = [[claim] for claim in claims[:count]]
        while len(claim_groups) < count:
            claim_groups.append([])
        if len(claims) > count and claim_groups:
            claim_groups[-1].extend(claims[count:])
        for story_slot_index in range(count):
            if count == 2:
                dialogue_move = ("story_open_with_consequence", "decision_boundary")[story_slot_index]
            elif count == 3:
                dialogue_move = (
                    "story_open_with_consequence",
                    "name_and_explain_previous_object",
                    "plain_language_payoff",
                )[story_slot_index]
            else:
                dialogue_move = (
                    "story_open_with_consequence",
                    "concrete_user_payoff",
                    "followup_translation",
                    "decision_boundary",
                )[min(story_slot_index, 3)]
            slots.append(
                {
                    "turn_id": f"T{len(slots) + 1:03d}",
                    "kind": "story",
                    "story_id": story["selection_id"],
                    "speaker_id": SPEAKERS[speaker_index % 2],
                    "allowed_claim_ids": claims,
                    "required_claim_ids": claim_groups[story_slot_index],
                    "required_information_dimension": (
                        dimension_by_claim.get(claim_groups[story_slot_index][0], "")
                        if claim_groups[story_slot_index]
                        else "plain_explain"
                    ),
                    "required_dialogue_move": dialogue_move,
                }
            )
            speaker_index += 1
    slots.append(
        {
            "turn_id": f"T{len(slots) + 1:03d}",
            "kind": "outro",
            "story_id": "",
            "speaker_id": SPEAKERS[speaker_index % 2],
            "allowed_claim_ids": [],
            "required_claim_ids": [],
            "required_information_dimension": "closing",
            "required_dialogue_move": "behavior_question",
        }
    )
    return slots


def _prompt(golden_examples: list[dict[str, Any]] | None = None) -> str:
    prompt = """你是中国抖音上的科技杂谈双主持编剧。节目追求热点、幽默、画面感和两个活人聊天的感觉，不是严肃新闻播报。只输出JSON：
{
  "episode_title":"标题",
  "episode_summary":"两句话内简介",
  "story_identities":[{"story_id":"S01","event_identity":"口播中实际说出的公司+产品或事件短名"}],
  "story_headlines":[{"story_id":"S01","headline":"8—30字播出小标题"}],
  "lines":[{"turn_id":"T001","text":"完整中文台词"}]
}
写作优先级和底线：
1. 热点吸引力、幽默互动、观众听懂和记住，优先于新闻稿式严谨。公司人物、核心数字、价格日期、官方状态和法律安全结论不能乱写；其他地方允许常识性称呼、夸张形容、类比、吐槽和带“未来/可能/如果”的展望。
2. lines只需按input_slots给定的turn_id和顺序输出台词，角色、story_id、claim绑定和信息维度由代码补齐。每句要围绕该槽对应的claim，但不要复述claim或念技术清单。
3. T001以“每日科技快讯来了”开场，立即打出最炸的数字反差、利益或名场面。90秒目标380—500字；单句建议24—46字，最多70字。
4. 雅雅是生活化吃瓜视角，负责惊讶、具体追问和脑洞；檬檬是懂技术但嘴不木的搭档，负责接梗、解释、轻吐槽和现实边界。两个人都要像熟人聊天，禁止“一人报事实，另一人继续补事实”。
5. 每条新闻至少出现一次追问、接梗、轻吐槽或生活类比。技术名词超过两个就概括成人话，例如把“电机、构型、散热、算法升级”说成“从腿到大脑都重新练了一遍”。
6. 可以说“这也太狠了吧、没错、精准、太提气了、这下有意思了、有点东西”，也可以自创新鲜但不低俗的表达；不要说“好家伙、家人们、值得关注、值得期待、拭目以待”。
7. 新新闻用极短且不重复的转场，马上说具体公司、产品或事件。wording_policy为hot_topic_expressive时，可沿用canonical_title里的“世界纪录、快过人类、夺冠、最快”等传播称呼。
8. 结尾不要泛问“你更期待哪个”，要问具体场景、能力、习惯或理由，让观众随手就能接一句。
9. story_headlines只写对象和最有记忆点的变化，8—30字；数字必须来自coverage_plan。event_identity必须是口播实际出现的4—24字短名。
10. 若提供上一版和返工问题，保留已好的内容，只重写点名的问题；不要为了合规把整篇重新变成广播稿。
"""

    if golden_examples:
        few_shot = "\n风格范例（真实成稿，只学节奏、口吻与承接，禁止复制其中任何事实、公司、型号或数字）：\n"
        for index, example in enumerate(golden_examples, 1):
            title = _clean(example.get("title"))
            few_shot += f"\n【范例{index}】{title}\n"
            for line in example["lines"]:
                few_shot += f"{_clean(line.get('speaker_name'))}：{_clean(line.get('text'))}\n"
        prompt += few_shot
    return prompt


def _editorial_policy(selection: dict[str, Any]) -> dict[str, Any]:
    levels = [_clean(story.get("heat_level")).upper() for story in selection.get("selected_stories") or []]
    has_public_breakout = any(level in {"H3", "H4"} for level in levels)
    return (
        {"quality_band": "premium", "required_total": 85, "hook_min": 16, "dialogue_min": 16, "information_density_min": 20}
        if has_public_breakout
        else {"quality_band": "fallback_publishable", "required_total": 78, "hook_min": 14, "dialogue_min": 15, "information_density_min": 19}
    )


def _prioritize_script_lead(selection: dict[str, Any]) -> dict[str, Any]:
    """Keep stable story IDs while moving the clearest public consequence to T001."""
    prioritized = deepcopy(selection)
    stories = prioritized.get("selected_stories") if isinstance(prioritized.get("selected_stories"), list) else []
    if len(stories) < 2:
        return prioritized

    def lead_score(story: dict[str, Any]) -> float:
        contract = " ".join(
            [
                _clean(story.get("canonical_title")),
                *[_clean(item.get("claim")) for item in story.get("coverage_plan") or [] if isinstance(item, dict)],
            ]
        )
        direct = 15.0 if re.search(r"国补|补贴|降价|涨价|直降|诈骗|泄露|风险|召回|免费|省钱", contract) else 0.0
        douyin_ranks = [
            int(match.get("rank") or 999)
            for match in story.get("external_heat_matches") or []
            if isinstance(match, dict)
            and (
                "douyin" in _clean(match.get("source_id")).lower()
                or "抖音" in _clean(match.get("source_name"))
            )
        ]
        douyin_lead_bonus = 0.0
        if douyin_ranks:
            best_rank = min(douyin_ranks)
            douyin_lead_bonus = 12.0 if best_rank <= 3 else 8.0 if best_rank <= 10 else 4.0
        return (
            float(story.get("audience_fit_score") or 0) * 0.45
            + float(story.get("editorial_potential_score") or 0) * 0.25
            + float(story.get("observed_heat_score") or 0) * 0.15
            + direct
            + douyin_lead_bonus
        )

    lead = max(stories, key=lead_score)
    prioritized["selected_stories"] = [lead, *(story for story in stories if story is not lead)]
    return prioritized


def _review_prompt(policy: dict[str, Any] | None = None) -> str:
    policy = policy or {"quality_band": "premium", "required_total": 85, "hook_min": 16, "dialogue_min": 16, "information_density_min": 20}
    return f"""你是独立的中国抖音公域科技快报责任编辑，不是合规检查员。你没有参与本稿选题或写作，必须按冷启动可发布短视频标准独立评价传播质量，不改写事实，只输出JSON：
{{
  "scores":{{"hook":0,"dialogue":0,"information_density":0,"public_value":0,"interaction":0}},
  "total":0,
  "issues":["可直接执行的具体问题"],
  "verdict":"pass|revise"
}}
评分上限依次为20、20、25、20、15，总分100。当前素材档位为{policy['quality_band']}，本轮交付线为总分{policy['required_total']}，且hook不低于{policy['hook_min']}、dialogue不低于{policy['dialogue_min']}、information_density不低于{policy['information_density_min']}。节目首先追求热点传播、幽默互动和记忆点，其次才是新闻式严谨。结构合规、没有英文或字数达标都不是高分理由；只要没有捏造公司人物、核心数字、价格日期、官方状态及法律安全结论，就不要因夸张形容、常识性称呼、类比或主观吐槽扣事实分。
重点检查：
1. 第一行是否同时有栏目身份、具体对象和明确利益、后果、数字反差或视觉名场面，而非正确废话；机器人比赛、硬件实测等娱乐热点只要具体成绩或画面足够抓人，不得强行要求省钱、避坑等实用影响；
2. 相邻台词是否真像两位有性格的熟人在聊热点，是否有追问、接梗、轻吐槽或生活类比；只有一人报事实、另一人继续补事实的技术清单，即使准确也属于人机广播，dialogue不得高于13分；
3. 同一新闻每句是否切换到独立信息维度，是否充分发挥高热题材，而非换句式复述；
4. 普通国内观众是否三秒能懂、能记住具体事件，并获得实用影响、娱乐看点或现实边界之一；不是每条新闻都必须提供行动建议；
5. 结尾是否有真实行为分享或站队空间，不是只有标准答案的问题；“你更期待哪个/哪类”若没有具体能力、场景或理由可供表达，interaction不得高于10分。
评审边界：coverage_plan约束硬事实，不约束主持人的幽默、类比和主观评论。只有公司人物张冠李戴、核心数字/价格/日期被改写、把未发生状态说成已发生，或凭空作出违法与安全定性，才属于硬事实越界；“世界纪录、这也太狠了、终于不是走秀”等不改变事件本质的节目化表达可以接受。两句H1按“说清、说透、有趣”评价。`reply_to`只是结构指针，仍需听实际承接。结尾只需围绕有共同维度的热点制造可聊话题。90秒是规划目标，预计超过120秒再压缩。
低于本轮交付线或任一强制单项不达标时必须revise，否则必须pass。issues必须指出对应turn_id和修改方向。"""


def _structured_review_issue(issue: Any) -> dict[str, Any]:
    """Turn reviewer prose into a stable recovery contract."""
    if isinstance(issue, dict):
        message = _clean(issue.get("message") or issue.get("issue"))
    else:
        message = _clean(issue)
    turn_ids = list(dict.fromkeys(re.findall(r"T\d{3}", message)))
    lowered = message.lower()
    if re.search(r"(?:预计|总时长|时长).{0,18}(?:超过|过长|压缩)|超过\s*120\s*秒", message):
        code, scope, action = "episode_duration_over_target", "episode", "compress"
    elif re.search(r"同质|题材重复|后半段.{0,12}(?:弱|拖|平)|选题.{0,12}(?:弱|重复)|组合.{0,12}(?:失衡|单一)", message):
        code, scope, action = "episode_topic_homogeneity", "episode", "reselect"
    elif re.search(
        r"(?:未在|超出).{0,20}(?:coverage_plan|冻结claim|支撑)|"
        r"(?:coverage_plan|冻结claim).{0,20}(?:未明确|不支持|之外)|"
        r"来源外|事实边界|与冻结.{0,12}不一致",
        message,
        re.IGNORECASE,
    ) and re.search(r"公司|人物|主体|型号|数字|金额|价格|日期|时间|发布|开售|上市|违法|诈骗|安全|伤亡|官方状态", message):
        code, scope, action = "factual_boundary", "turn" if turn_ids else "story", "repair_lines"
    elif re.search(r"结尾|互动|评论|标准答案|二选一", message):
        code, scope, action = "interaction_weak", "turn" if turn_ids else "episode", "repair_lines"
    elif re.search(r"重复|复述|信息增量|同一维度", message):
        code, scope, action = "information_repetition", "turn" if turn_ids else "story", "repair_lines"
    elif re.search(r"对话|承接|广播|书面|口语|活人", message):
        code, scope, action = "dialogue_flow", "turn" if turn_ids else "episode", "repair_lines"
    else:
        code, scope, action = "general_editorial", "turn" if turn_ids else "episode", "repair_lines"
    return {
        "code": code,
        "scope": scope,
        "turn_ids": turn_ids,
        "message": message,
        "suggested_action": action,
        "hard_fact_boundary": code == "factual_boundary",
        "raw_kind": type(issue).__name__.lower(),
        "normalized_from": lowered[:80],
    }


def validate_editorial_review(raw: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy or {"quality_band": "premium", "required_total": 85, "hook_min": 16, "dialogue_min": 16, "information_density_min": 20}
    limits = {"hook": 20, "dialogue": 20, "information_density": 25, "public_value": 20, "interaction": 15}
    scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    normalized: dict[str, int] = {}
    validation_issues: list[str] = []
    for key, maximum in limits.items():
        try:
            value = int(scores.get(key))
        except (TypeError, ValueError):
            validation_issues.append(f"传播复验缺少{key}评分")
            continue
        if not 0 <= value <= maximum:
            validation_issues.append(f"传播复验{key}评分超出0—{maximum}")
        normalized[key] = value
    if validation_issues:
        raise DailyScriptV2ValidationError(validation_issues)
    calculated = sum(normalized.values())
    raw_issues = raw.get("issues") if isinstance(raw.get("issues"), list) else []
    structured_issues = [_structured_review_issue(item) for item in raw_issues]
    reported_issues = [item["message"] for item in structured_issues if item["message"]]
    verdict = _clean(raw.get("verdict")).lower()
    factual_boundary_issue = any(item.get("hard_fact_boundary") is True for item in structured_issues)
    duration_blocking_issue = any(
        item.get("code") == "episode_duration_over_target" for item in structured_issues
    )
    passed = (
        calculated >= int(policy["required_total"])
        and normalized["hook"] >= int(policy["hook_min"])
        and normalized["dialogue"] >= int(policy["dialogue_min"])
        and normalized["information_density"] >= int(policy["information_density_min"])
        and verdict in {"pass", "revise"}
        and not factual_boundary_issue
        and not duration_blocking_issue
    )
    if not passed and not reported_issues:
        reported_issues = ["传播质量未达85分，需具体重写钩子、对话承接或信息维度"]
    return {
        "scores": normalized,
        "total": calculated,
        "issues": reported_issues,
        "structured_issues": structured_issues,
        "verdict": "pass" if passed else "revise",
        "passed": passed,
        "quality_band": policy["quality_band"],
        "required_total": int(policy["required_total"]),
    }


def _review_scores_meet_policy(review: dict[str, Any], policy: dict[str, Any]) -> bool:
    scores = review.get("scores") if isinstance(review.get("scores"), dict) else {}
    try:
        return (
            review.get("passed") is True
            and _clean(review.get("verdict")) == "pass"
            and not (review.get("issues") or [])
            and sum(int(scores[key]) for key in ("hook", "dialogue", "information_density", "public_value", "interaction"))
            >= int(policy["required_total"])
            and int(scores["hook"]) >= int(policy["hook_min"])
            and int(scores["dialogue"]) >= int(policy["dialogue_min"])
            and int(scores["information_density"]) >= int(policy["information_density_min"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _review_has_issue(review: dict[str, Any], code: str) -> bool:
    structured = review.get("structured_issues") if isinstance(review.get("structured_issues"), list) else []
    if any(isinstance(item, dict) and _clean(item.get("code")) == code for item in structured):
        return True
    return any(_structured_review_issue(issue)["code"] == code for issue in review.get("issues") or [])


def _dialogue_move_issues(script: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    lines = script.get("lines") or []
    first_story_id = next((item.get("story_id") for item in lines if item.get("story_id")), "")
    opening_unit = "".join(_clean(item.get("text")) for item in lines[:2])
    for item in lines:
        text = _clean(item.get("text"))
        move = _clean(item.get("required_dialogue_move"))
        turn_id = _clean(item.get("turn_id"))
        story_id = _clean(item.get("story_id"))
        if turn_id == "T001" and not re.search(
            r"你|普通人|用户|钱包|体验|风险|成本|手机|电脑|通勤|购买|"
            r"比赛|赛场|现场|画面|冲刺|百米|\d+(?:\.\d+)?秒|涨价|降价|破纪录|名场面|"
            r"游戏|新游|定档|上线|全平台|开黑",
            opening_unit,
        ):
            issues.append("T001-T002 首个对话单元必须落到可感知影响、明确反差或视觉名场面")
        if move == "story_open_with_consequence" and story_id and story_id != first_story_id:
            if not re.match(r"^(?:再看|接着|还有|最后|另一边)", text):
                issues.append(f"{turn_id} 切换新闻必须用极短转场并立即进入具体事件")
    lead_phrases = ("再看", "接着", "还有", "另一边", "最后", "具体看", "而且", "不过先别急", "也就是说", "换成大白话")
    for phrase in lead_phrases:
        repeated = [_clean(item.get("turn_id")) for item in lines if _clean(item.get("text")).startswith(phrase)]
        if len(repeated) > 1:
            issues.append(f"主持短语“{phrase}”重复用于{','.join(repeated)}，必须改成自然追问、指代或因果承接")
    conversational_markers = re.compile(
        r"说白了|相当于|就像|这也|这下|终于|别急|先别|真有|看着|听着|难怪|妥妥|"
        r"不用|免得|省得|换设备|更顺手|不卡壳|狂喜|摸鱼|马拉松|比.+更|不是.+而是|[？?!！]"
    )
    technical_term_pattern = re.compile(
        r"(?i)iOS|Android|鸿蒙|模拟器|Wi-?Fi|AI|GPU|CPU|芯片|处理器|模型|算力|"
        r"吸力|尘袋|数据互通|跨设备|验证码|机器人|出货量|占比"
        r"|关节|构型|散热|空气动力学|算法|系统"
    )
    for item in lines:
        text = _clean(item.get("text"))
        technical_terms = technical_term_pattern.findall(text)
        if len(technical_terms) >= 3 and text.count("、") + text.count("，") >= 4 and not conversational_markers.search(text):
            issues.append(
                f"{_clean(item.get('turn_id'))} 连续罗列技术名词，必须改成生活类比、接梗或一句可感知判断"
            )
    return issues


def _normalize_dialogue_connectors(raw: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    repaired = deepcopy(raw)
    slots = _script_slots(selection)
    lines = repaired.get("lines") if isinstance(repaired.get("lines"), list) else []
    story_order = [story.get("selection_id") for story in selection.get("selected_stories") or []]
    first_story = story_order[0] if story_order else ""
    for index, slot in enumerate(slots):
        if index >= len(lines) or not isinstance(lines[index], dict):
            continue
        text = _clean(lines[index].get("text"))
        move = slot.get("required_dialogue_move")
        story_id = slot.get("story_id")
        if move == "story_open_with_consequence" and story_id and story_id != first_story:
            if not re.match(r"^(?:再看|接着|还有|最后|另一边)", text):
                story_index = story_order.index(story_id) if story_id in story_order else 1
                connector_by_index = {
                    1: "接着，",
                    2: "另一边，",
                }
                text = (
                    "最后，"
                    if story_index == len(story_order) - 1
                    else connector_by_index.get(story_index, "还有，")
                ) + text
        lines[index]["text"] = text
    return repaired


EVENT_IDENTITY_RULE_VERSION = "event-identity-v2"
EVENT_IDENTITY_TIME_ONLY_PATTERN = re.compile(
    r"^(?:(?:\d{4}年)?\d{1,2}月(?:\d{1,2}日)?(?:凌晨|早上|上午|中午|下午|晚上|深夜)?|"
    r"(?:今日|昨日|当日|目前|刚刚|凌晨|早上|上午|中午|下午|晚上|深夜))$"
)
EVENT_IDENTITY_GENERIC_PATTERN = re.compile(
    r"^(?:最新(?:科技)?消息|正式发布|集中升温|行业升温|重磅消息|引发热议|新变化)$"
)


def _event_identity_contract(story: dict[str, Any]) -> str:
    return " ".join(
        [
            _clean(story.get("canonical_title")),
            *[
                _clean(item.get("claim"))
                for item in story.get("coverage_plan") or []
                if isinstance(item, dict)
            ],
        ]
    )


def _compact_identity_text(value: Any) -> str:
    return re.sub(r"\s+", "", _clean(value)).lower()


def validate_event_identity_candidate(
    identity: str,
    *,
    story: dict[str, Any],
    opening_text: str,
) -> dict[str, Any]:
    """Apply one shared grounding/spoken/specificity contract to an identity."""
    normalized = _clean(identity).strip("，。！？:：-—| ")
    compact_identity = _compact_identity_text(normalized)
    compact_source = _compact_identity_text(_event_identity_contract(story))
    compact_opening = _compact_identity_text(opening_text)
    source_without_dates = re.sub(r"于?(?:\d{4}年)?\d{1,2}月\d{1,2}日", "", compact_source)
    source_without_actions = re.sub(r"发布|推出|亮相|宣布|官宣|当日|今日|昨日|目前", "", source_without_dates)
    grounded = bool(
        compact_identity
        and (
            compact_identity in compact_source
            or compact_identity in source_without_dates
            or compact_identity in source_without_actions
        )
    )
    opening_without_time = re.sub(r"当日|今日|昨日|目前", "", compact_opening)
    identity_without_time = re.sub(r"当日|今日|昨日|目前", "", compact_identity)
    identity_without_category = re.sub(r"(?:手机|耳机|芯片|模型|机器人|服务)$", "", compact_identity)
    identity_models = {
        token.lower() for token in re.findall(r"(?i)[a-z][a-z0-9.+-]{3,}", compact_identity)
    }
    action_split = re.split(r"公布|发布|推出|展示|亮相|官宣|完成|牵头|晒", compact_identity, maxsplit=1)
    identity_subject = action_split[0] if len(action_split) > 1 else ""
    identity_topics = {
        token
        for token in re.findall(
            r"人形机器人|机器人|百米|芯片|处理器|手机|电脑|耳机|模型|服务器|电池|火箭|游戏|运动会|赛事",
            compact_identity,
        )
    }
    semantic_spoken = (
        len(identity_subject) >= 2
        and identity_subject in compact_opening
        and bool(identity_topics)
        and any(token in compact_opening for token in identity_topics)
    )
    spoken = bool(
        compact_identity
        and (
            compact_identity in compact_opening
            or identity_without_time in opening_without_time
            or (len(identity_without_category) >= 4 and identity_without_category in compact_opening)
            or any(token in compact_opening for token in identity_models)
            or semantic_spoken
        )
    )
    time_only = bool(EVENT_IDENTITY_TIME_ONLY_PATTERN.fullmatch(compact_identity))
    generic = bool(EVENT_IDENTITY_GENERIC_PATTERN.fullmatch(compact_identity)) or bool(
        re.fullmatch(r"(?:\d{4}年)?\d{1,2}月.*(?:行业)?集中升温", compact_identity)
    )
    length_ok = 4 <= len(normalized) <= 24
    specific = length_ok and not time_only and not generic
    if not normalized:
        reason = "empty"
    elif not length_ok:
        reason = "length"
    elif time_only:
        reason = "time_only"
    elif generic:
        reason = "generic"
    elif not grounded:
        reason = "not_grounded"
    elif not spoken:
        reason = "not_spoken"
    else:
        reason = "pass"
    return {
        "valid": reason == "pass",
        "grounded": grounded,
        "spoken": spoken,
        "specific": specific,
        "reason_code": reason,
        "normalized_identity": normalized,
        "rule_version": EVENT_IDENTITY_RULE_VERSION,
    }


def _grounded_event_identity(story: dict[str, Any], opening_text: str, proposed: str) -> str:
    """Return a short identity spoken in the opening and grounded in frozen facts.

    The writer owns dialogue, not schema metadata.  In particular, a harmless
    wording difference such as ``共同开发`` versus ``共研`` must not spend the
    only model revision round.  Prefer a valid writer proposal, otherwise
    recover a product/model name that occurs verbatim in both the opening and
    the frozen title/claims.
    """
    source = _event_identity_contract(story)
    compact_source = re.sub(r"\s+", "", source).lower()
    compact_opening = re.sub(r"\s+", "", _clean(opening_text)).lower()
    compact_proposed = re.sub(r"\s+", "", _clean(proposed)).lower()
    candidates: list[str] = []
    quoted_candidates = [
        value
        for value in re.findall(r"《[^》]{2,20}》|[“\"]([^”\"]{4,24})[”\"]", _clean(opening_text))
    ]
    for value in quoted_candidates:
        candidate = value if isinstance(value, str) else ""
        if candidate:
            candidates.append(candidate)
    # re.findall with alternatives and a capture returns only the capture for
    # ordinary quotes, so collect book-title candidates separately.
    candidates.extend(re.findall(r"《[^》]{2,20}》", _clean(opening_text)))
    chinese_model_candidates = [
        match.group(1)
        for match in re.finditer(
            r"(?=([\u3400-\u9fff]{1,8}[A-Za-z][A-Za-z0-9.+-]*\d[A-Za-z0-9.+-]*))",
            _clean(opening_text),
        )
        if 4 <= len(match.group(1)) <= 24
        and re.sub(r"\s+", "", match.group(1)).lower() in compact_source
    ]
    candidates.extend(sorted(chinese_model_candidates, key=len, reverse=True))

    generic_tokens = {
        "ai", "cpu", "gpu", "nfc", "gps", "lcd", "wifi", "wi-fi", "5g", "4g",
    }
    token_pattern = re.compile(
        r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ0-9.+-]*"
        r"(?:\s+[A-Za-z0-9.+-]+){0,3}"
    )
    model_candidates: list[str] = []
    for match in token_pattern.finditer(_clean(opening_text)):
        token = match.group(0).strip()
        compact_token = re.sub(r"\s+", "", token).lower()
        if not 4 <= len(token) <= 24 or compact_token in generic_tokens or compact_token not in compact_source:
            continue
        model_candidates.append(token)
        suffix_match = re.match(r"(?:\s*)(芯片|系统|手机|机器人|模型|服务|新机)", _clean(opening_text)[match.end():])
        if suffix_match:
            extended = token + suffix_match.group(1)
            if len(extended) <= 24 and re.sub(r"\s+", "", extended).lower() in compact_source:
                model_candidates.append(extended)
    candidates.extend(sorted(model_candidates, key=lambda value: (len(re.sub(r"\s+", "", value)), len(value)), reverse=True))

    # Games, films and named projects are often most naturally identified by
    # their Chinese book-title form.  Keep the quoted title when it is spoken
    # and frozen, instead of preserving an unverified publisher prefix.
    book_title_candidates = [
        value
        for value in re.findall(r"《[^》]{2,20}》", _clean(opening_text))
        if re.sub(r"\s+", "", value).lower() in compact_source
    ]
    candidates.extend(sorted(book_title_candidates, key=len, reverse=True))

    # A valid writer proposal is usually the most natural spoken phrase. It
    # still passes the same shared contract, so time-only or broad proposals
    # cannot outrank a concrete deterministic fallback.
    candidates.append(_clean(proposed))

    # Named events are a stronger identity than an article's broad trend
    # headline. Keep the stable event name even when an ordinal precedes it.
    event_name_candidates = [
        match.group(1)
        for match in re.finditer(
            r"(?:第[一二三四五六七八九十0-9]+届)?(世界人形机器人运动会|世界机器人大会|人形机器人运动会)",
            _clean(opening_text),
        )
    ]
    candidates.extend(sorted(event_name_candidates, key=len, reverse=True))

    # Chinese brand + product names are often the most natural spoken identity
    # (for example “华为小艺”).  Prefer the short phrase actually said by the
    # hosts instead of forcing them to recite an award's database-style name.
    brand_pattern = re.compile(r"长鑫存储|华为|小米|荣耀|阿里|腾讯|百度|字节|苹果|英伟达|联想|小鹏|宇树|长鑫")
    action_pattern = re.compile(r"公布|发布|拿|获|宣布|推出|开通|关注|官微|微博|评测|综合|评分|第一")
    chinese_phrase_candidates: list[str] = []
    spoken = re.sub(r"\s+", "", _clean(opening_text))
    candidates.extend(
        match.group(0)
        for match in brand_pattern.finditer(spoken)
        if 4 <= len(match.group(0)) <= 24 and match.group(0).lower() in compact_source
    )
    for length in range(4, 9):
        for start in range(0, max(0, len(spoken) - length + 1)):
            phrase = spoken[start:start + length]
            if (
                re.fullmatch(r"[\u3400-\u9fff]+", phrase)
                and brand_pattern.search(phrase)
                and not action_pattern.search(phrase)
                and phrase.lower() in compact_source
            ):
                chinese_phrase_candidates.append(phrase)
    candidates.extend(sorted(chinese_phrase_candidates, key=len, reverse=True))

    # The writer was explicitly asked to choose a short identity that is both
    # grounded and actually spoken.  Keep that natural spoken phrase before
    # falling back to the first claim's database-style prefix.  The previous
    # order did the opposite: a valid phrase such as ``人形机器人百米赛`` was
    # overwritten by ``荣耀公布人形机器人运动会百米项目成绩`` and the dialogue
    # was then rejected for not reciting the longer schema label verbatim.
    claim_prefix_candidates: list[str] = []
    identity_topic_pattern = re.compile(
        r"(?i)人形机器人|机器人|百米|芯片|处理器|手机|电脑|耳机|模型|服务器|电池|火箭|游戏|"
        r"Mac|iPhone|iPad|OpenAI|AI|\d"
    )
    for claim in story.get("coverage_plan") or []:
        if not isinstance(claim, dict):
            continue
        prefix = re.split(r"[，。；：、“（(]", _clean(claim.get("claim")), maxsplit=1)[0]
        prefix = prefix.strip("，。！？:：-—| ")
        if 4 <= len(prefix) <= 24 and identity_topic_pattern.search(prefix):
            claim_prefix_candidates.append(prefix)
    candidates.extend(claim_prefix_candidates)
    seen: set[str] = set()
    for candidate in candidates:
        compact = _compact_identity_text(candidate)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        check = validate_event_identity_candidate(candidate, story=story, opening_text=opening_text)
        if check["valid"]:
            return check["normalized_identity"]
    return ""


def _normalize_slot_metadata(raw: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    """Keep model creativity in text while making deterministic slot metadata authoritative."""
    repaired = deepcopy(raw)
    lines = repaired.get("lines") if isinstance(repaired.get("lines"), list) else []
    slots = _script_slots(selection)
    function_by_move = {
        "story_open_with_consequence": "fact",
        "name_and_explain_previous_object": "answer",
        "plain_language_payoff": "plain_explain",
        "concrete_user_payoff": "impact",
        "followup_translation": "plain_explain",
        "decision_boundary": "constraint",
        "behavior_question": "closing",
    }
    for index, slot in enumerate(slots):
        if index >= len(lines) or not isinstance(lines[index], dict):
            continue
        move = _clean(slot.get("required_dialogue_move"))
        lines[index].update(
            {
                "turn_id": slot["turn_id"],
                "speaker_id": slot["speaker_id"],
                "kind": slot["kind"],
                "story_id": slot["story_id"],
                "information_dimension": slot["required_information_dimension"],
                "claim_ids": list(slot["required_claim_ids"]),
                "function": "hook" if slot["turn_id"] == "T001" else function_by_move.get(move, "fact"),
                "information_key": f"{slot['story_id'] or 'outro'}-{slot['required_information_dimension']}-{index + 1}",
            }
        )
    story_by_id = {story["selection_id"]: story for story in selection.get("selected_stories") or []}
    opening_lines_by_story: dict[str, list[str]] = {}
    first_story_id = next(iter(story_by_id), "")
    for line in lines:
        story_id = _clean(line.get("story_id"))
        if not story_id:
            continue
        limit = 2 if story_id == first_story_id else 1
        parts = opening_lines_by_story.setdefault(story_id, [])
        if len(parts) < limit:
            parts.append(_clean(line.get("text")))
    opening_by_story = {story_id: "".join(parts) for story_id, parts in opening_lines_by_story.items()}
    proposed_by_story = {
        _clean(item.get("story_id")): _clean(item.get("event_identity"))
        for item in repaired.get("story_identities") or []
        if isinstance(item, dict)
    }
    repaired["story_identities"] = [
        {
            "story_id": story_id,
            "event_identity": _grounded_event_identity(
                story,
                opening_by_story.get(story_id, ""),
                proposed_by_story.get(story_id, ""),
            ),
        }
        for story_id, story in story_by_id.items()
    ]
    normalized_headlines: list[dict[str, str]] = []
    for item in repaired.get("story_headlines") or []:
        if not isinstance(item, dict):
            continue
        headline = _clean(item.get("headline")).strip("，。！？:：-—| ")
        if len(headline) > 30:
            # A model may write a good two-part headline that exceeds the
            # overlay contract only because of the explanatory second clause.
            # Keep the complete first clause when it is independently useful;
            # never slice through an English product/model token.
            first_clause = re.split(r"[，,；;。]", headline, maxsplit=1)[0].strip()
            if 8 <= len(first_clause) <= 30:
                headline = first_clause
        normalized_headlines.append({
            "story_id": _clean(item.get("story_id")),
            "headline": headline,
        })
    repaired["story_headlines"] = normalized_headlines
    broadcast_headlines = [
        _clean(item.get("headline")).strip("，。！？:：-—| ")
        for item in repaired.get("story_headlines") or []
        if isinstance(item, dict) and _clean(item.get("headline"))
    ]
    if broadcast_headlines:
        # Episode metadata is schema, not dialogue creativity. Build it from
        # the already claim-bounded broadcast headlines so “齐发、首秀、涨价”
        # packaging cannot leak in from the writer and waste a revision.
        repaired["episode_title"] = "、".join(broadcast_headlines)
        repaired["episode_summary"] = "本期关注：" + "；".join(broadcast_headlines) + "。"
    return repaired


def _dialogue_sha256(raw: dict[str, Any]) -> str:
    payload = [
        {"turn_id": _clean(item.get("turn_id")), "text": str(item.get("text") or "")}
        for item in raw.get("lines") or []
        if isinstance(item, dict)
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _metadata_sha256(raw: dict[str, Any]) -> str:
    payload = {
        "story_identities": raw.get("story_identities") or [],
        "story_headlines": raw.get("story_headlines") or [],
        "line_metadata": [
            {
                key: item.get(key)
                for key in (
                    "turn_id", "speaker_id", "kind", "story_id", "information_dimension",
                    "claim_ids", "function", "information_key", "reply_to",
                )
            }
            for item in raw.get("lines") or []
            if isinstance(item, dict)
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def repair_structural_metadata(raw: dict[str, Any], selection: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Repair deterministic schema fields while proving dialogue is immutable."""
    before_dialogue = _dialogue_sha256(raw)
    before_metadata = _metadata_sha256(raw)
    before_identities = {
        _clean(item.get("story_id")): _clean(item.get("event_identity"))
        for item in raw.get("story_identities") or []
        if isinstance(item, dict)
    }
    repaired = _normalize_slot_metadata(raw, selection)
    after_dialogue = _dialogue_sha256(repaired)
    if before_dialogue != after_dialogue:
        raise DailyScriptV2ValidationError(["结构元数据修复器修改了台词正文，已拒绝该修复"])
    after_identities = {
        _clean(item.get("story_id")): _clean(item.get("event_identity"))
        for item in repaired.get("story_identities") or []
        if isinstance(item, dict)
    }
    changes = [
        {
            "path": f"story_identities[{story_id}].event_identity",
            "before": before_identities.get(story_id, ""),
            "after": after_identities.get(story_id, ""),
        }
        for story_id in sorted(set(before_identities) | set(after_identities))
        if before_identities.get(story_id, "") != after_identities.get(story_id, "")
    ]
    audit = {
        "rule_version": EVENT_IDENTITY_RULE_VERSION,
        "changed": bool(changes) or before_metadata != _metadata_sha256(repaired),
        "changed_fields": changes,
        "dialogue_sha256_before": before_dialogue,
        "dialogue_sha256_after": after_dialogue,
        "dialogue_preserved": True,
        "metadata_sha256_before": before_metadata,
        "metadata_sha256_after": _metadata_sha256(repaired),
    }
    return repaired, audit


def _structural_issue_codes(issues: list[str]) -> list[str]:
    codes: list[str] = []
    for issue in issues:
        if "event_identity不能只是日期或时间" in issue:
            codes.append("event_identity_time_only")
        elif "event_identity不能是泛化趋势描述" in issue:
            codes.append("event_identity_generic")
        elif "event_identity必须逐字来自" in issue:
            codes.append("event_identity_not_grounded")
        elif "开场未在规定句数内说出event_identity" in issue:
            codes.append("event_identity_not_spoken")
        elif "event_identity" in issue:
            codes.append("event_identity_schema")
        else:
            codes.append("structural_validation")
    return list(dict.fromkeys(codes))


def _validated_script_to_raw(script: dict[str, Any]) -> dict[str, Any]:
    """Rehydrate a validated checkpoint without paying for a full model rewrite."""
    return {
        "episode_title": _clean(script.get("episode_title") or script.get("title")),
        "episode_summary": _clean(script.get("episode_summary")),
        "story_identities": [
            {"story_id": _clean(story.get("story_id")), "event_identity": _clean(story.get("event_identity"))}
            for story in script.get("stories") or []
            if isinstance(story, dict)
        ],
        "story_headlines": [
            {"story_id": _clean(story.get("story_id")), "headline": _clean(story.get("headline"))}
            for story in script.get("stories") or []
            if isinstance(story, dict)
        ],
        "lines": [deepcopy(line) for line in script.get("lines") or [] if isinstance(line, dict)],
    }


def _review_selection_contract(selection: dict[str, Any]) -> dict[str, Any]:
    """Expose only allocated claims to the propagation reviewer.

    Raw media headlines and unused backup claims are useful for auditing but
    can make the reviewer demand facts the writer was explicitly forbidden to
    use.  The reviewer receives the same factual ceiling as the dialogue.
    """
    return {
        "target_date": selection.get("target_date"),
        "selection_summary": selection.get("selection_summary") or {},
        "selected_stories": [
            {
                "selection_id": story.get("selection_id"),
                "story_type": story.get("story_type"),
                "heat_level": story.get("heat_level"),
                "wording_policy": story.get("wording_policy") or "verified_facts",
                "coverage_plan": story.get("coverage_plan") or [],
            }
            for story in selection.get("selected_stories") or []
        ],
    }


def _issue_turn_ids(issue: Any, valid_turn_ids: set[str]) -> list[str]:
    text = _clean(issue)
    ordered: list[str] = []
    for start, end in re.findall(r"T(\d{3})\s*[-—至到]\s*T(\d{3})", text):
        left, right = int(start), int(end)
        step = 1 if right >= left else -1
        for value in range(left, right + step, step):
            turn_id = f"T{value:03d}"
            if turn_id in valid_turn_ids and turn_id not in ordered:
                ordered.append(turn_id)
    for turn_id in re.findall(r"T\d{3}", text):
        if turn_id in valid_turn_ids and turn_id not in ordered:
            ordered.append(turn_id)
    return ordered


def _repair_editorial_lines(
    script: dict[str, Any],
    review_issues: list[str],
    selection: dict[str, Any],
    *,
    provider: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Ask the configured writer for text-only replacements without rebuilding schema."""
    raw = _validated_script_to_raw(script)
    lines = raw["lines"]
    line_by_id = {_clean(line.get("turn_id")): line for line in lines}
    target_ids: list[str] = []
    valid_turn_ids = set(line_by_id)
    issue_targets = {
        _clean(issue): _issue_turn_ids(issue, valid_turn_ids)
        for issue in review_issues
    }
    for ids in issue_targets.values():
        for turn_id in ids:
            if turn_id in line_by_id and turn_id not in target_ids:
                target_ids.append(turn_id)
    if not target_ids:
        target_ids = [_clean(line.get("turn_id")) for line in lines]
    claim_map = {
        _clean(claim.get("claim_id")): _clean(claim.get("claim"))
        for story in selection.get("selected_stories") or []
        for claim in story.get("coverage_plan") or []
        if isinstance(claim, dict)
    }
    issue_map = {
        turn_id: [issue for issue in review_issues if turn_id in issue_targets.get(_clean(issue), [])]
        for turn_id in target_ids
    }
    approved_style_examples = [
        {
            "id": example.get("id"),
            "lines": example.get("lines") or [],
        }
        for example in _load_golden_examples(limit=2)
    ]
    payload = {
        "lines_to_rewrite": [
            {
                "turn_id": turn_id,
                "current_text": _clean(line_by_id[turn_id].get("text")),
                "frozen_claims": [claim_map[claim_id] for claim_id in line_by_id[turn_id].get("claim_ids") or [] if claim_id in claim_map],
                "review_issues": issue_map[turn_id],
                "previous_text": _clean(lines[index - 1].get("text")) if (index := lines.index(line_by_id[turn_id])) > 0 else "",
                "next_text": _clean(lines[index + 1].get("text")) if index + 1 < len(lines) else "",
                "must_start_with": "每日科技快讯来了" if turn_id == "T001" else "",
                "must_be_question": _clean(line_by_id[turn_id].get("kind")) == "outro",
            }
            for turn_id in target_ids
        ],
        "target_total_characters": "420—500字（这是返工目标，不得靠删除核心claim或增加来源外信息硬凑）",
        "approved_style_examples": approved_style_examples,
    }
    prompt = """你是中文抖音科技快报的局部台词编辑。只输出JSON：
{"replacements":[{"turn_id":"T001","text":"替换后的完整台词"}]}
必须且只能返回输入的turn_id，不得改其他句子或结构字段。公司人物、核心数字、日期价格、官方状态及法律安全结论只可来自frozen_claims；允许加入不改变硬事实的类比、吐槽、常识性称呼和带“未来/可能/如果”的展望。
把全部目标句先当成一段完整对话再重写，不要逐句孤立改词。approved_style_examples只用于学习已获用户认可的情绪、节奏和接话方式，严禁复制其中事实、主体、型号或数字。
T001必须保留指定开头，先抛本期最强结果、反差或用户利益，禁止在核心信息之前提空泛问题。每条两句以上的新闻至少出现一次真正回应上一位主持人具体对象或情绪的接话，并加入追问、接梗、轻吐槽或生活类比之一；只添加“接着、另一边、最后、该模型、其技术”仍算新闻轮流念。技术清单必须讲成人能听懂的感受或场景。
修正review_issues，同时自然接住previous_text并为next_text留出承接。若覆盖多句，整体压到420—500字，单句优先32—52字；T001不超过60字。结尾要围绕至少两条新闻共有的具体能力、场景或取舍，让观众能分享理由，禁止只问“更期待哪个”。"""
    last_errors: list[str] = []
    model = ""
    for repair_attempt in range(2):
        if last_errors:
            payload["previous_repair_errors"] = last_errors
            payload["repair_attempt"] = repair_attempt
        response, model = _chat_json_with_transient_retry(
            prompt,
            payload,
            temperature=0.72,
            provider=provider or _writer_provider(),
        )
        replacements = response.get("replacements") if isinstance(response.get("replacements"), list) else []
        replacement_map = {
            _clean(item.get("turn_id")): _clean(item.get("text"))
            for item in replacements
            if isinstance(item, dict)
        }
        last_errors = []
        if set(replacement_map) != set(target_ids):
            last_errors.append("局部传播返工必须且只能返回全部指定turn_id")
            continue
        for turn_id, text in replacement_map.items():
            if not 14 <= len(text) <= 500:
                last_errors.append(f"{turn_id} 局部传播返工后长度异常")
            if turn_id == "T001" and not text.startswith("每日科技快讯来了"):
                last_errors.append("T001 局部传播返工丢失栏目身份")
            if _clean(line_by_id[turn_id].get("kind")) == "outro" and not re.search(r"[？?]", text):
                last_errors.append("局部传播返工后的结尾必须保留问号")
        if last_errors:
            payload["previous_failed_replacements"] = replacements
            continue
        for turn_id, text in replacement_map.items():
            line_by_id[turn_id]["text"] = text
        return raw, model
    raise DailyScriptV2ValidationError(last_errors or ["局部传播返工失败"])


def _chat_json_with_transient_retry(
    system: str,
    payload: dict[str, Any],
    *,
    temperature: float,
    provider: str = "default",
) -> tuple[dict[str, Any], str]:
    # Long model responses can survive the proxy idle timeout via SSE yet
    # still lose a final HTTP chunk occasionally.  Use one bounded retry:
    # repeated full generations may also consume tokens at the provider even
    # when the relay never delivers the final JSON.
    for service_attempt in range(2):
        try:
            return _chat_json(system, payload, timeout_seconds=180, temperature=temperature, provider=provider)
        except TextAIError as exc:
            transient = re.search(
                r"超时|时限|HTTP 50[234]|连接|中断|premature|ChunkedEncoding|ProxyError|RemoteDisconnected|结构化文案|SSL|EOF|Max retries",
                str(exc),
                re.IGNORECASE,
            )
            if service_attempt >= 1 or not transient:
                raise
    raise TextAIError("文本模型瞬时故障重试失败")


def _cold_review_with_fallback(
    system: str,
    payload: dict[str, Any],
    *,
    preferred_provider: str,
) -> tuple[dict[str, Any], str, str, dict[str, Any] | None]:
    """Cold-review with an auditable availability-only fallback.

    A valid Doubao rejection is never sent to Luna/default for an override.
    The fallback is used only when Doubao cannot return a review after the
    existing bounded transient retry.
    """
    try:
        raw, model = _chat_json_with_transient_retry(
            system,
            payload,
            temperature=0.0,
            provider=preferred_provider,
        )
        return raw, model, preferred_provider, None
    except TextAIError as exc:
        if preferred_provider != "doubao":
            raise
        raw, model = _chat_json_with_transient_retry(
            system,
            payload,
            temperature=0.0,
            provider="default",
        )
        return raw, model, "default", {
            "from": "doubao",
            "to": "default",
            "reason": str(exc)[:300],
        }


def _headline_overlay(title: Any) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", _clean(title)).strip("，。！？:：-—|（）() ")
    # Chinese headlines do not need word spaces, but an English product name
    # such as ``GPT-5.6 Sol`` must remain readable and must never be split in
    # the middle merely to satisfy the two-line layout.
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+|\s+(?=[\u3400-\u9fff])", "", text)
    text = text[:30]
    if len(text) <= 12:
        return {"mode": "one_line", "line_1": text, "line_2": "", "placement": "top_right_beside_presenter", "style_id": "daily_news_headline_v1"}

    def display_units(value: str) -> float:
        return sum(1.0 if "\u3400" <= char <= "\u9fff" else 0.35 if char.isspace() else 0.55 for char in value)

    protected_ascii: set[int] = set()
    for match in re.finditer(r"[A-Za-z0-9%]+(?:[.\-][A-Za-z0-9%]+)*(?:\s+[A-Za-z0-9%]+(?:[.\-][A-Za-z0-9%]+)*)*", text):
        protected_ascii.update(range(match.start() + 1, match.end()))
    # Keep numeric values attached to their spoken/display units. Splitting
    # “6999元” into “6999 / 元起售” is as disruptive as splitting a model name.
    for match in re.finditer(r"\d+(?:\.\d+)?(?:元|秒|%|％|亿|万|纳米|倍|GB|TB)", text, re.IGNORECASE):
        protected_ascii.update(range(match.start() + 1, match.end()))
    semantic_markers = (
        "募资", "投入", "限时", "降价", "下调", "固态电池", "国际标准",
        "消费级", "人形机器人", "开放预订", "正式发布", "正式立项", "支持",
        "回应",
    )
    preferred = {index for index, char in enumerate(text, 1) if char in "，：；、"}
    for marker in semantic_markers:
        start = text.find(marker)
        if start >= 4:
            preferred.add(start)
        if start >= 0:
            protected_ascii.update(range(start + 1, start + len(marker)))
    for match in re.finditer(r"的(?:首项|首个|首次|新款)", text):
        preferred.add(match.end())

    candidates: list[tuple[float, int]] = []
    for index in range(4, len(text) - 3):
        if index in protected_ascii:
            continue
        top, bottom = text[:index].rstrip("，：；、 "), text[index:].lstrip("，：；、 ")
        top_units, bottom_units = display_units(top), display_units(bottom)
        if not top or not bottom or bottom_units <= top_units:
            continue
        # Prefer natural semantic boundaries, then the most balanced layout.
        score = (18.0 if index in preferred else 0.0) - abs(bottom_units - top_units) - abs(index - len(text) * 0.45) * 0.15
        candidates.append((score, index))
    if candidates:
        split = max(candidates)[1]
    else:
        # A long protected product/value sequence can eliminate every
        # preferred split. Never fall back to a blind midpoint inside that
        # sequence; choose the nearest semantically safe boundary instead.
        safe_splits: list[tuple[float, int]] = []
        for index in range(4, len(text) - 3):
            if index in protected_ascii:
                continue
            top, bottom = text[:index].rstrip("，：；、 "), text[index:].lstrip("，：；、 ")
            if not top or not bottom:
                continue
            top_units, bottom_units = display_units(top), display_units(bottom)
            bottom_bonus = 8.0 if bottom_units > top_units else 0.0
            safe_splits.append((bottom_bonus - abs(bottom_units - top_units), index))
        split = max(safe_splits, default=(0.0, max(4, (len(text) - 1) // 2)))[1]
    line_1 = text[:split].rstrip("，：；、")
    line_2 = text[split:].lstrip("，：；、")
    return {"mode": "two_line", "line_1": line_1, "line_2": line_2, "placement": "top_right_beside_presenter", "style_id": "daily_news_headline_v1"}


def _repair_overlong_lines(
    raw: dict[str, Any],
    issues: list[str],
    selection: dict[str, Any],
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    matches = [
        match
        for issue in issues
        if (match := re.fullmatch(r"(T\d{3}) 字数超过70字异常上限", issue)) is not None
    ]
    if not matches:
        return raw
    turn_ids = [match.group(1) for match in matches]
    lines = [item for item in raw.get("lines") or [] if isinstance(item, dict)]
    line_by_id = {str(item.get("turn_id") or ""): item for item in lines}
    if any(turn_id not in line_by_id for turn_id in turn_ids):
        return raw
    identity_map = {
        str(item.get("story_id") or ""): _clean(item.get("event_identity"))
        for item in raw.get("story_identities") or []
        if isinstance(item, dict)
    }
    claim_text = {
        str(claim.get("claim_id") or ""): _clean(claim.get("claim"))
        for story in selection.get("selected_stories") or []
        for claim in story.get("coverage_plan") or []
        if isinstance(claim, dict)
    }
    story_by_id = {
        _clean(story.get("selection_id")): story
        for story in selection.get("selected_stories") or []
        if isinstance(story, dict)
    }
    repair_payload = {
        "violations": issues,
        "lines_to_shorten": [
            {
                "turn_id": turn_id,
                "current_text": _clean(line_by_id[turn_id].get("text")),
                "story_id": line_by_id[turn_id].get("story_id"),
                "event_identity": identity_map.get(str(line_by_id[turn_id].get("story_id") or ""), ""),
                "wording_policy": _clean(
                    story_by_id.get(_clean(line_by_id[turn_id].get("story_id")), {}).get("wording_policy")
                ),
                "frozen_claims": [claim_text.get(str(claim_id), "") for claim_id in line_by_id[turn_id].get("claim_ids") or []],
                "must_start_with": "每日科技快讯来了" if turn_id == "T001" else "",
            }
            for turn_id in turn_ids
        ],
    }
    repair_prompt = """你是中文短视频台词精简编辑。只输出JSON：
{"replacements":[{"turn_id":"T001","text":"精简后的完整台词"}]}
只能返回输入指定的turn_id。每条text目标35—52字，绝对不得超过60字。公司人物、核心数字、日期价格、官方状态和法律安全结论只保留frozen_claims支持的内容；节目化类比、吐槽与热点称呼可以保留。T001必须以指定短语开头。不得修改其他台词，不得解释。"""
    repair_errors: list[str] = []
    for repair_round in range(3):
        if repair_errors:
            repair_payload["previous_repair_errors"] = repair_errors
            repair_payload["repair_round"] = repair_round
        repair_raw, _ = _chat_json_with_transient_retry(
            repair_prompt,
            repair_payload,
            temperature=0.0,
            provider=provider or _writer_provider(),
        )
        replacements = repair_raw.get("replacements") if isinstance(repair_raw.get("replacements"), list) else []
        replacement_map = {
            _clean(item.get("turn_id")): _clean(item.get("text"))
            for item in replacements
            if isinstance(item, dict)
        }
        repair_errors = []
        if set(replacement_map) != set(turn_ids):
            repair_errors.append("必须且只能覆盖全部指定turn_id")
            continue
        repaired = deepcopy(raw)
        for item in repaired.get("lines") or []:
            turn_id = _clean(item.get("turn_id"))
            if turn_id not in replacement_map:
                continue
            before, after = _clean(item.get("text")), replacement_map[turn_id]
            if len(after) > HARD_LINE_MAX:
                # Some providers obey the factual constraints but still
                # return two sentences. Keep the longest complete first
                # sentence/phrase inside the real 70-character guard instead
                # of discarding the entire episode. This only deletes a suffix
                # at punctuation; it never rewrites an entity or number.
                after = _trim_complete_suffix(after)
            # 35—52 is the pacing target, while 70 is the project's actual
            # structural redline.  A repair that brings an overlong sentence
            # back to 61—70 characters has resolved the hard failure and must
            # not stop the whole unattended episode merely for missing the
            # softer editorial target.
            if not 14 <= len(after) <= HARD_LINE_MAX:
                repair_errors.append(
                    f"{turn_id} 当前为{len(after)}字，目标35—52字且不得超过{HARD_LINE_MAX}字"
                )
                continue
            before_numbers = _normalized_number_tokens(before)
            after_numbers = _normalized_number_tokens(after)
            if not after_numbers <= before_numbers:
                repair_errors.append(f"{turn_id} 精简时新增或改写了数字")
                continue
            item["text"] = after
        if not repair_errors:
            return repaired
        repair_payload["previous_failed_replacements"] = replacements
    # The provider may repeatedly shorten the prose while accidentally
    # changing a comparison number.  The original sentence is already
    # fact-checked, so a punctuation-only suffix deletion is safer than
    # stopping the whole run or asking for another creative rewrite.
    fallback = deepcopy(raw)
    fallback_errors: list[str] = []
    for item in fallback.get("lines") or []:
        turn_id = _clean(item.get("turn_id"))
        if turn_id not in turn_ids:
            continue
        before = _clean(item.get("text"))
        after = _trim_complete_suffix(before)
        if not 14 <= len(after) <= HARD_LINE_MAX or after == before:
            fallback_errors.append(f"{turn_id} 无可安全截断的完整标点边界")
            continue
        item["text"] = after
    if not fallback_errors:
        return fallback
    raise DailyScriptV2ValidationError(repair_errors + fallback_errors or ["局部句长返工失败"])


def _repair_total_length(
    raw: dict[str, Any],
    issues: list[str],
    *,
    provider: str | None = None,
    target_max: int = HARD_TOTAL_MAX,
) -> dict[str, Any]:
    current_total = sum(
        len(_clean(item.get("text")))
        for item in raw.get("lines") or []
        if isinstance(item, dict)
    )
    hard_violation = any(re.fullmatch(r"总口播\d+字，不在280—600字保护区间", issue) for issue in issues)
    target_max = max(HARD_TOTAL_MIN, min(int(target_max), HARD_TOTAL_MAX))
    if not hard_violation and current_total <= target_max:
        return raw
    lines = [item for item in raw.get("lines") or [] if isinstance(item, dict)]
    if not lines:
        return raw

    # Small overruns should not pay for a full-script rewrite or risk losing a
    # frozen number. Remove only known zero-information presenter padding,
    # preserving every model name, company, number and claim-bearing phrase.
    if current_total - target_max <= 40:
        deterministic = deepcopy(raw)
        padding_phrases = (
            "对应机型成绩亮眼，",
            "这项技术门槛可不低：",
            "这次新预告",
            "这次变化",
            "值得一提的是，",
            "可以看到，",
            "不难发现，",
        )
        for phrase in padding_phrases:
            for item in deterministic.get("lines") or []:
                text = _clean(item.get("text"))
                if phrase not in text or len(text) - len(phrase) < 14:
                    continue
                item["text"] = text.replace(phrase, "", 1).lstrip("，： ")
                if sum(len(_clean(row.get("text"))) for row in deterministic.get("lines") or []) <= target_max:
                    return deterministic
    payload: dict[str, Any] = {
        "target_total_characters": min(400, target_max),
        "hard_total_range": [HARD_TOTAL_MIN, target_max],
        "lines": [
            {
                "turn_id": _clean(item.get("turn_id")),
                "kind": _clean(item.get("kind")),
                "current_text": _clean(item.get("text")),
                "must_start_with": "每日科技快讯来了" if _clean(item.get("turn_id")) == "T001" else "",
                "must_end_as_question": _clean(item.get("kind")) == "outro",
            }
            for item in lines
        ],
    }
    prompt = f"""你是中文短视频台词压缩编辑。只输出JSON：
{{"replacements":[{{"turn_id":"T001","text":"压缩后的完整台词"}}]}}
必须且只能返回输入全部turn_id，顺序不变。整稿目标{min(400, target_max)}字、允许范围{HARD_TOTAL_MIN}—{target_max}字；当允许上限低于600字时，T001不超过44字，其余新闻句控制在28—34字，outro不超过34字，总字数是硬目标而非建议。只删冗词、重复解释、次要对照数字和参数堆叠，不得新增或改变事实、数字、百分比、产品型号和公司名；允许删除次要数字，但每句必须保留其核心事实。T001保留指定开头，outro必须保留问号。不得解释。"""
    errors: list[str] = []
    for repair_round in range(3):
        if errors:
            payload["previous_repair_errors"] = errors
            payload["repair_round"] = repair_round
        repair_raw, _ = _chat_json_with_transient_retry(
            prompt,
            payload,
            temperature=0.0,
            provider=provider or _writer_provider(),
        )
        replacements = repair_raw.get("replacements") if isinstance(repair_raw.get("replacements"), list) else []
        replacement_map = {
            _clean(item.get("turn_id")): _clean(item.get("text"))
            for item in replacements
            if isinstance(item, dict)
        }
        expected_ids = {_clean(item.get("turn_id")) for item in lines}
        errors = []
        if set(replacement_map) != expected_ids:
            errors.append("必须且只能覆盖全部turn_id")
            continue
        repaired = deepcopy(raw)
        for item in repaired.get("lines") or []:
            turn_id = _clean(item.get("turn_id"))
            before, after = _clean(item.get("text")), replacement_map[turn_id]
            if not 14 <= len(after) <= HARD_LINE_MAX:
                errors.append(f"{turn_id} 当前为{len(after)}字，必须在14—70字")
            before_numbers = _normalized_number_tokens(before)
            after_numbers = _normalized_number_tokens(after)
            if not after_numbers <= before_numbers:
                errors.append(f"{turn_id} 压缩时新增或改写了数字")
            if turn_id == "T001" and not after.startswith("每日科技快讯来了"):
                errors.append("T001 丢失栏目开头")
            if _clean(item.get("kind")) == "outro" and not re.search(r"[？?]", after):
                errors.append(f"{turn_id} 结尾不再是问题")
            item["text"] = after
        total = sum(len(_clean(item.get("text"))) for item in repaired.get("lines") or [])
        if target_max < total <= target_max + 20:
            # Provider-side compressors occasionally miss the requested cap by
            # only a handful of Chinese characters.  Do not abandon an
            # otherwise valid episode for a two-character miss: remove only
            # known non-factual presenter padding and keep every number,
            # product name and claim-bearing phrase intact.
            micro_padding = (
                "值得一提的是，",
                "可以看到，",
                "不难发现，",
                "对应来看，",
                "这次",
                "本次",
                "对应",
                "全新",
            )
            for phrase in micro_padding:
                for item in repaired.get("lines") or []:
                    before = _clean(item.get("text"))
                    if phrase not in before or len(before) - len(phrase) < 14:
                        continue
                    item["text"] = before.replace(phrase, "", 1).lstrip("，： ")
                    total = sum(len(_clean(row.get("text"))) for row in repaired.get("lines") or [])
                    if total <= target_max:
                        break
                if total <= target_max:
                    break
        if not HARD_TOTAL_MIN <= total <= target_max:
            errors.append(f"压缩后总口播仍为{total}字，必须在{HARD_TOTAL_MIN}—{target_max}字")
        if not errors:
            return repaired
        payload["previous_failed_replacements"] = replacements
    raise DailyScriptV2ValidationError(errors or ["整稿长度压缩失败"])


def _payload(selection: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    # The selector has already frozen every sentence-level fact into
    # ``coverage_plan``. Full article excerpts belong to the audit artifact,
    # not the writer request: sending them again wastes tokens, invites the
    # writer to expand beyond the frozen claims, and substantially increases
    # long-stream timeouts. Keep the parameter for API compatibility and make
    # the trust boundary explicit.
    _ = research
    stories: list[dict[str, Any]] = []
    for story in selection.get("selected_stories") or []:
        stories.append(
            {
                "selection_id": story.get("selection_id"),
                "canonical_title": story.get("canonical_title"),
                "story_type": story.get("story_type"),
                "heat_level": story.get("heat_level"),
                "content_capacity": story.get("content_capacity"),
                "understanding_cost": story.get("understanding_cost"),
                "wording_policy": story.get("wording_policy") or "verified_facts",
                "coverage_plan": story.get("coverage_plan"),
            }
        )
    return {"target_date": selection.get("target_date"), "stories": stories, "input_slots": _script_slots(selection)}


def validate_script_v2(raw: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    expected = _script_slots(selection)
    lines = raw.get("lines") if isinstance(raw.get("lines"), list) else []
    if len(lines) != len(expected):
        issues.append(f"台词应为{len(expected)}句，实际{len(lines)}句")
    story_by_id = {story["selection_id"]: story for story in selection.get("selected_stories") or []}
    claim_by_id = {
        _clean(claim.get("claim_id")): _clean(claim.get("claim"))
        for story in selection.get("selected_stories") or []
        for claim in story.get("coverage_plan") or []
        if isinstance(claim, dict)
    }
    all_claim_contract = " ".join(claim_by_id.values())
    episode_metadata = " ".join(
        [_clean(raw.get("episode_title") or raw.get("title")), _clean(raw.get("episode_summary"))]
    )
    metadata_numbers = re.findall(r"\d+(?:\.\d+)?%?", episode_metadata)
    if any(number not in all_claim_contract for number in metadata_numbers):
        issues.append("节目标题或摘要包含冻结claim之外的数字")
    if (
        UNGROUNDED_EPISODE_METADATA_PATTERN.search(episode_metadata)
        and not UNGROUNDED_EPISODE_METADATA_PATTERN.search(all_claim_contract)
    ):
        issues.append("节目标题或摘要包含冻结claim之外的强结论")
    headline_rows = raw.get("story_headlines") if isinstance(raw.get("story_headlines"), list) else []
    headline_map: dict[str, str] = {}
    for row in headline_rows:
        if not isinstance(row, dict):
            continue
        story_id = _clean(row.get("story_id"))
        headline = _clean(row.get("headline")).strip("，。！？:：-—| ")
        if story_id not in story_by_id:
            issues.append(f"story_headlines包含未知新闻：{story_id or '[空]'}")
            continue
        if story_id in headline_map:
            issues.append(f"{story_id} 播出小标题重复声明")
            continue
        headline_map[story_id] = headline
    if set(headline_map) != set(story_by_id):
        missing = sorted(set(story_by_id) - set(headline_map))
        issues.append(f"story_headlines必须覆盖全部新闻，缺少：{','.join(missing)}")
    for story_id, story in story_by_id.items():
        headline = headline_map.get(story_id, "")
        claim_contract = " ".join(
            _clean(claim.get("claim"))
            for claim in story.get("coverage_plan") or []
            if isinstance(claim, dict)
        )
        if headline and not 8 <= len(headline) <= 30:
            issues.append(f"{story_id} 播出小标题必须为8—30字")
        headline_numbers = re.findall(r"\d+(?:\.\d+)?%?", headline)
        if any(number not in claim_contract for number in headline_numbers):
            issues.append(f"{story_id} 播出小标题包含冻结claim之外的数字")
        if (
            _clean(story.get("wording_policy")) not in {"hot_topic_expressive"}
            and NARROWED_HYPE_PATTERN.search(headline)
            and not NARROWED_HYPE_PATTERN.search(_clean(story.get("canonical_title")) + " " + claim_contract)
        ):
            issues.append(f"{story_id} 播出小标题使用了未冻结的纪录、领先或夺冠结论")
        if (
            UNGROUNDED_HEADLINE_HYPE_PATTERN.search(headline)
            and not UNGROUNDED_HEADLINE_HYPE_PATTERN.search(claim_contract)
        ):
            issues.append(f"{story_id} 播出小标题使用了未冻结的首发或唯一性结论")
    identity_rows = raw.get("story_identities") if isinstance(raw.get("story_identities"), list) else []
    identity_map: dict[str, str] = {}
    for row in identity_rows:
        if not isinstance(row, dict):
            continue
        story_id = _clean(row.get("story_id"))
        identity = _clean(row.get("event_identity"))
        if story_id not in story_by_id:
            issues.append(f"story_identities包含未知新闻：{story_id or '[空]'}")
            continue
        if story_id in identity_map:
            issues.append(f"{story_id} event_identity重复声明")
            continue
        identity_map[story_id] = identity
    if set(identity_map) != set(story_by_id):
        missing = sorted(set(story_by_id) - set(identity_map))
        issues.append(f"story_identities必须覆盖全部新闻，缺少：{','.join(missing)}")
    first_story_id = next(iter(story_by_id), "")
    opening_parts: dict[str, list[str]] = {story_id: [] for story_id in story_by_id}
    for index, slot in enumerate(expected):
        story_id = _clean(slot.get("story_id"))
        if not story_id or index >= len(lines) or not isinstance(lines[index], dict):
            continue
        limit = 2 if story_id == first_story_id else 1
        if len(opening_parts[story_id]) < limit:
            opening_parts[story_id].append(_clean(lines[index].get("text")))
    for story_id, story in story_by_id.items():
        identity = identity_map.get(story_id, "")
        check = validate_event_identity_candidate(
            identity,
            story=story,
            opening_text="".join(opening_parts.get(story_id) or []),
        )
        if check["reason_code"] == "length":
            issues.append(f"{story_id} event_identity必须为4—24字")
        elif check["reason_code"] == "time_only":
            issues.append(f"{story_id} event_identity不能只是日期或时间：{identity}")
        elif check["reason_code"] == "generic":
            issues.append(f"{story_id} event_identity不能是泛化趋势描述：{identity}")
        elif check["reason_code"] == "not_grounded":
            issues.append(f"{story_id} event_identity必须逐字来自冻结标题或claim")
        elif check["reason_code"] == "not_spoken":
            issues.append(f"{story_id} 开场未在规定句数内说出event_identity：{identity}")
        elif check["reason_code"] == "empty":
            issues.append(f"{story_id} event_identity不能为空")
    used_claims: dict[str, set[str]] = {story_id: set() for story_id in story_by_id}
    normalized: list[dict[str, Any]] = []
    for index, slot in enumerate(expected):
        if index >= len(lines) or not isinstance(lines[index], dict):
            continue
        line = lines[index]
        for field in ("turn_id", "speaker_id", "kind", "story_id"):
            if _clean(line.get(field)) != _clean(slot[field]):
                if field == "kind" and slot[field] == "story" and _clean(line.get(field)) == "fact":
                    continue
                issues.append(f"{slot['turn_id']} 的 {field} 与固定时间槽不一致")
        function = _clean(line.get("function"))
        information_dimension = _clean(line.get("information_dimension"))
        information_key = _clean(line.get("information_key"))
        text = _clean(line.get("text"))
        claim_ids = line.get("claim_ids") if isinstance(line.get("claim_ids"), list) else []
        claim_ids = list(dict.fromkeys(_clean(value) for value in claim_ids if _clean(value)))
        if function not in FUNCTIONS:
            issues.append(f"{slot['turn_id']} function无效")
        if information_dimension != slot["required_information_dimension"]:
            issues.append(f"{slot['turn_id']} information_dimension必须为{slot['required_information_dimension']}")
        if slot["kind"] == "story":
            if claim_ids != slot["required_claim_ids"]:
                issues.append(f"{slot['turn_id']} 必须严格使用固定claim：{','.join(slot['required_claim_ids'])}")
            duplicate = used_claims[slot["story_id"]] & set(claim_ids)
            if duplicate:
                issues.append(f"{slot['turn_id']} 重复使用claim：{','.join(sorted(duplicate))}")
            used_claims[slot["story_id"]].update(claim_ids)
            frozen_text = " ".join(claim_by_id.get(claim_id, "") for claim_id in claim_ids)
            story_contract_text = " ".join(
                [
                    _clean(story_by_id.get(slot["story_id"], {}).get("canonical_title")),
                    *[
                        _clean(item.get("claim"))
                        for item in story_by_id.get(slot["story_id"], {}).get("coverage_plan") or []
                        if isinstance(item, dict)
                    ],
                ]
            )
            story_policy = _clean(story_by_id.get(slot["story_id"], {}).get("wording_policy"))
            if (
                story_policy not in {"hot_topic_expressive"}
                and NARROWED_HYPE_PATTERN.search(text)
                and not NARROWED_HYPE_PATTERN.search(story_contract_text)
            ):
                issues.append(f"{slot['turn_id']} 使用了未冻结的纪录、领先或夺冠结论")
            if (
                UNGROUNDED_COMPARISON_PATTERN.search(text)
                and not UNGROUNDED_COMPARISON_PATTERN.search(frozen_text)
            ):
                issues.append(f"{slot['turn_id']} 使用了冻结claim之外的比较结论")
            if (
                UNGROUNDED_REMOTE_PATTERN.search(text)
                and not UNGROUNDED_REMOTE_PATTERN.search(frozen_text)
            ):
                issues.append(f"{slot['turn_id']} 使用了冻结claim之外的无遥控结论")
            if not information_key:
                issues.append(f"{slot['turn_id']} 缺少information_key")
            if not 14 <= len(text) <= HARD_LINE_MAX:
                issues.append(f"{slot['turn_id']} 字数超过70字异常上限")
        else:
            if claim_ids:
                issues.append("结尾不得新增事实claim")
            if function != "closing" or not re.search(r"[？?]", text):
                issues.append("结尾必须使用closing并提出真实问题")
        if re.search(r"好家伙|撒胡椒面|牌桌加码|值得关注|值得期待|拭目以待|科技圈炸锅", text):
            issues.append(f"{slot['turn_id']} 命中禁用广播腔或痞化表达")
        if (
            UNGROUNDED_RELEASE_STATUS_PATTERN.search(text)
            and not UNGROUNDED_RELEASE_STATUS_PATTERN.search(all_claim_contract)
        ):
            issues.append(f"{slot['turn_id']} 使用了冻结claim之外的开售状态")
        normalized.append(
            {
                **slot,
                "speaker_name": "雅雅" if slot["speaker_id"] == "yaya" else "檬檬",
                "function": function,
                "information_dimension": information_dimension,
                "information_key": information_key,
                "claim_ids": claim_ids,
                "text": text,
                "reply_to": expected[index - 1]["turn_id"] if index else "",
            }
        )
    if normalized and normalized[0]["speaker_id"] != "yaya":
        issues.append("第一句必须由雅雅开场")
    if normalized and not normalized[0]["text"].startswith("每日科技快讯来了"):
        issues.append("第一句必须包含栏目身份并直接进入钩子")
    for story_id in story_by_id:
        keys = [item["information_key"] for item in normalized if item["story_id"] == story_id]
        if len(keys) != len(set(keys)):
            issues.append(f"{story_id} information_key重复")
    counts = {
        speaker: sum(len(item["text"]) for item in normalized if item["speaker_id"] == speaker)
        for speaker in SPEAKERS
    }
    if issues:
        raise DailyScriptV2ValidationError(issues)
    total_chars = sum(len(item["text"]) for item in normalized)
    if not HARD_TOTAL_MIN <= total_chars <= HARD_TOTAL_MAX:
        raise DailyScriptV2ValidationError([f"总口播{total_chars}字，不在280—600字保护区间"])
    minimum_ratio = min(counts.values()) / max(1, total_chars)
    if minimum_ratio < 0.30:
        raise DailyScriptV2ValidationError([f"双主持较少一方仅占{minimum_ratio:.1%}，必须至少30%"])
    stories = []
    for story in selection.get("selected_stories") or []:
        story_id = story["selection_id"]
        stories.append({
            "story_id": story_id,
            "event_identity": identity_map.get(story_id, ""),
            "headline": headline_map.get(story_id, ""),
            "source_headline": _clean(story.get("canonical_title")),
            "headline_overlay": _headline_overlay(headline_map.get(story_id, "")),
            "story_type": story.get("story_type"),
            "heat_level": story.get("heat_level"),
            "source_ids": story.get("evidence_candidate_ids") or [],
            "official_image_url": _clean(story.get("official_image_url")),
            "official_image_attribution": _clean(story.get("official_image_attribution")),
        })
    return {
        "version": "2.1-production",
        "prompt_version": PROMPT_VERSION,
        "target_date": selection.get("target_date"),
        "generated_at": _now(),
        "episode_title": _clean(raw.get("episode_title")),
        "title": _clean(raw.get("episode_title")),
        "episode_summary": _clean(raw.get("episode_summary")),
        "stories": stories,
        "lines": normalized,
        "pure_scripts": {
            speaker: "\n".join(item["text"] for item in normalized if item["speaker_id"] == speaker)
            for speaker in SPEAKERS
        },
        "validation": {
            "valid": True,
            "line_count": len(normalized),
            "spoken_character_count": total_chars,
            "estimated_duration_seconds": round(total_chars / 4.4, 1),
            "selection_version": selection.get("version"),
            "speaker_character_counts": counts,
            "speaker_minimum_ratio": round(minimum_ratio, 4),
        },
    }


def generate_script_v2(
    selection: dict[str, Any],
    research: dict[str, Any],
    *,
    max_revision_rounds: int = 1,
    reuse_structural_checkpoint: bool = True,
) -> dict[str, Any]:
    selection = _prioritize_script_lead(selection)
    payload = _payload(selection, research)
    editorial_policy = _editorial_policy(selection)
    payload["editorial_policy"] = editorial_policy
    golden_examples = _load_golden_examples()
    writer_provider = _writer_provider()
    preferred_reviewer_provider = _reviewer_provider()
    issues: list[str] = []
    checkpoint_path = RUNS_ROOT / _clean(selection.get("target_date")) / "daily_script_v2_last_rejected.json"
    checkpoint = _read_json(checkpoint_path) or {}
    story_order = [_clean(story.get("selection_id")) for story in selection.get("selected_stories") or []]
    # Slot IDs are deliberately reused as S01/S02/... across combinations.
    # They therefore cannot identify the underlying events for checkpoint
    # reuse.  Bind a rejected checkpoint to the actual event (falling back to
    # its canonical title for older fixtures) so a manual combination switch
    # never pours the previous story's dialogue into the new story's slot.
    story_event_order = [
        _clean(story.get("event_id")) or _clean(story.get("canonical_title"))
        for story in selection.get("selected_stories") or []
    ]
    if (
        checkpoint.get("story_order") != story_order
        or checkpoint.get("story_event_order") != story_event_order
        or checkpoint.get("prompt_version") != PROMPT_VERSION
    ):
        checkpoint = {}
    checkpoint_editorial_script: dict[str, Any] | None = None
    checkpoint_editorial_issues: list[str] = []
    force_duration_compression = False
    pending_raw: dict[str, Any] | None = None
    pending_model = ""
    checkpoint_raw = (
        checkpoint.get("raw_script")
        if reuse_structural_checkpoint
        and checkpoint.get("status") == "structural_rejected"
        and (
            _clean(checkpoint.get("repair_status") or "untried") == "untried"
            or _clean(checkpoint.get("script_repair_rule_version")) != SCRIPT_REPAIR_RULE_VERSION
        )
        and isinstance(checkpoint.get("raw_script"), dict)
        else None
    )
    metadata_repair_audit: dict[str, Any] = {}
    if checkpoint.get("status") == "editorial_rejected" and isinstance(checkpoint.get("script"), dict):
        previous_review = checkpoint.get("editorial_review") or {}
        force_duration_compression = _review_has_issue(previous_review, "episode_duration_over_target")
        if _review_scores_meet_policy(previous_review, editorial_policy):
            checkpoint_raw = _validated_script_to_raw(checkpoint["script"])
        else:
            review_issues = previous_review.get("issues") or []
            issues = [f"传播复验：{_clean(issue)}" for issue in review_issues if _clean(issue)]
            checkpoint_editorial_script = checkpoint["script"]
            checkpoint_editorial_issues = [_clean(issue) for issue in review_issues if _clean(issue)]
    for revision in range(max_revision_rounds + 1):
        if issues:
            payload["validation_errors_to_fix"] = issues
            payload["revision_round"] = revision
        if pending_raw is not None:
            raw, model = pending_raw, pending_model
            pending_raw = None
        elif revision == 0 and checkpoint_raw is not None:
            raw, model = deepcopy(checkpoint_raw), "durable-structural-checkpoint"
        elif revision == 0 and checkpoint_editorial_script is not None:
            raw, model = _repair_editorial_lines(
                checkpoint_editorial_script,
                checkpoint_editorial_issues,
                selection,
                provider=writer_provider,
            )
            if _review_has_issue(checkpoint.get("editorial_review") or {}, "episode_duration_over_target"):
                raw = _repair_total_length(
                    raw,
                    ["episode_duration_over_target"],
                    provider=writer_provider,
                    target_max=EDITORIAL_DURATION_MAX_CHARS,
                )
        else:
            raw, model = _chat_json_with_transient_retry(
                _prompt(golden_examples), payload, temperature=0.8, provider=writer_provider
            )
        raw, metadata_repair_audit = repair_structural_metadata(raw, selection)
        raw = _normalize_dialogue_connectors(raw, selection)
        if (
            force_duration_compression
            and sum(len(_clean(item.get("text"))) for item in raw.get("lines") or [] if isinstance(item, dict)) > EDITORIAL_DURATION_MAX_CHARS
        ):
            raw = _repair_total_length(
                raw,
                ["episode_duration_over_target"],
                provider=writer_provider,
                target_max=EDITORIAL_DURATION_MAX_CHARS,
            )
            raw, metadata_repair_audit = repair_structural_metadata(raw, selection)
            raw = _normalize_dialogue_connectors(raw, selection)
        try:
            try:
                result = validate_script_v2(raw, selection)
            except DailyScriptV2ValidationError as first_validation:
                raw = _repair_overlong_lines(
                    raw,
                    first_validation.issues,
                    selection,
                    provider=writer_provider,
                )
                raw, metadata_repair_audit = repair_structural_metadata(raw, selection)
                raw = _normalize_dialogue_connectors(raw, selection)
                try:
                    result = validate_script_v2(raw, selection)
                except DailyScriptV2ValidationError as second_validation:
                    raw = _repair_total_length(
                        raw,
                        second_validation.issues,
                        provider=writer_provider,
                    )
                    raw, metadata_repair_audit = repair_structural_metadata(raw, selection)
                    raw = _normalize_dialogue_connectors(raw, selection)
                    result = validate_script_v2(raw, selection)
            dialogue_issues = _dialogue_move_issues(result)
            if dialogue_issues:
                raw, model = _repair_editorial_lines(
                    result,
                    dialogue_issues,
                    selection,
                    provider=writer_provider,
                )
                raw, metadata_repair_audit = repair_structural_metadata(raw, selection)
                raw = _normalize_dialogue_connectors(raw, selection)
                try:
                    result = validate_script_v2(raw, selection)
                except DailyScriptV2ValidationError as dialogue_validation:
                    raw = _repair_overlong_lines(
                        raw,
                        dialogue_validation.issues,
                        selection,
                        provider=writer_provider,
                    )
                    raw, metadata_repair_audit = repair_structural_metadata(raw, selection)
                    raw = _normalize_dialogue_connectors(raw, selection)
                    result = validate_script_v2(raw, selection)
                remaining_dialogue_issues = _dialogue_move_issues(result)
                if remaining_dialogue_issues:
                    raise DailyScriptV2ValidationError(remaining_dialogue_issues)
            review_raw, review_model, reviewer_provider, reviewer_fallback = _cold_review_with_fallback(
                _review_prompt(editorial_policy),
                {
                    "review_mode": "independent_cold_review",
                    "selection": _review_selection_contract(selection),
                    "script": result,
                },
                preferred_provider=preferred_reviewer_provider,
            )
            editorial_review = validate_editorial_review(review_raw, editorial_policy)
            if float((result.get("validation") or {}).get("estimated_duration_seconds") or 0) > 120.0:
                duration_message = (
                    f"确定性时长门：预计{result['validation']['estimated_duration_seconds']}秒，"
                    "超过120秒上限，必须压缩后复审"
                )
                editorial_review["passed"] = False
                editorial_review["verdict"] = "revise"
                if duration_message not in editorial_review["issues"]:
                    editorial_review["issues"].append(duration_message)
                    editorial_review["structured_issues"].append(
                        _structured_review_issue(duration_message)
                    )
            if not editorial_review["passed"]:
                if _review_has_issue(editorial_review, "episode_duration_over_target"):
                    force_duration_compression = True
                _atomic_json(
                    checkpoint_path,
                    {
                        "status": "editorial_rejected",
                        "prompt_version": PROMPT_VERSION,
                        "saved_at": _now(),
                        "revision_round": revision,
                        "story_order": story_order,
                        "story_event_order": story_event_order,
                        "script": result,
                        "editorial_review": {
                            **editorial_review,
                            "model": review_model,
                            "provider": reviewer_provider,
                            "review_mode": "independent_cold_review",
                            "provider_fallback": reviewer_fallback,
                        },
                    },
                )
                issues = [f"传播复验：{issue}" for issue in editorial_review["issues"]]
                payload["previous_rejected_script"] = result
                if revision < max_revision_rounds:
                    pending_raw, pending_model = _repair_editorial_lines(
                        result,
                        editorial_review["issues"],
                        selection,
                        provider=writer_provider,
                    )
                    if _review_has_issue(editorial_review, "episode_duration_over_target"):
                        pending_raw = _repair_total_length(
                            pending_raw,
                            ["episode_duration_over_target"],
                            provider=writer_provider,
                            target_max=EDITORIAL_DURATION_MAX_CHARS,
                        )
                continue
            result["model"] = model
            result["editorial_review"] = {
                **editorial_review,
                "model": review_model,
                "provider": reviewer_provider,
                "review_mode": "independent_cold_review",
                "provider_fallback": reviewer_fallback,
            }
            result["generation_audit"] = {
                "prompt_version": PROMPT_VERSION,
                "writer_provider": writer_provider,
                "writer_model": model,
                "preferred_reviewer_provider": preferred_reviewer_provider,
                "reviewer_provider": reviewer_provider,
                "reviewer_model": review_model,
                "review_mode": "independent_cold_review",
                "reviewer_provider_fallback": reviewer_fallback,
                "structural_metadata_repair": metadata_repair_audit,
                "golden_examples": [
                    {
                        "id": _clean(example.get("id")),
                        "sha256": _clean(example.get("sha256")),
                        "approved_at": _clean(example.get("approved_at")),
                    }
                    for example in golden_examples
                ],
            }
            return result
        except DailyScriptV2ValidationError as exc:
            _atomic_json(
                checkpoint_path,
                {
                    "status": "structural_rejected",
                    "prompt_version": PROMPT_VERSION,
                    "saved_at": _now(),
                    "revision_round": revision,
                    "story_order": story_order,
                    "story_event_order": story_event_order,
                    "issues": exc.issues,
                    "issue_codes": _structural_issue_codes(exc.issues),
                    "raw_dialogue_sha256": _dialogue_sha256(raw),
                    "metadata_sha256": _metadata_sha256(raw),
                    "repair_status": "changed" if metadata_repair_audit.get("changed") else "no_progress",
                    "repair_rule_version": EVENT_IDENTITY_RULE_VERSION,
                    "script_repair_rule_version": SCRIPT_REPAIR_RULE_VERSION,
                    "structural_metadata_repair": metadata_repair_audit,
                    "raw_script": raw,
                },
            )
            issues = exc.issues
            payload["previous_rejected_script"] = raw
    raise DailyScriptV2ValidationError(issues or ["V2正式脚本未通过校验"])


def _markdown(script: dict[str, Any]) -> str:
    lines = [f"# {script['episode_title']}", "", script["episode_summary"], "", "## 双主持顺序稿", ""]
    lines.extend(f"**{item['speaker_name']}：** {item['text']}" for item in script["lines"])
    lines.extend(["", "## 雅雅台词纯净版", "", script["pure_scripts"]["yaya"], "", "## 檬檬台词纯净版", "", script["pure_scripts"]["mengmeng"], ""])
    return "\n".join(lines)


def run_script_v2_test(target: date | str) -> dict[str, Any]:
    target_value = date.fromisoformat(target) if isinstance(target, str) else target
    run_dir = RUNS_ROOT / target_value.isoformat()
    selection = _read_json(run_dir / "topic_selection_v2.json")
    research = _read_json(run_dir / "news_research_v2.json")
    if not selection or selection.get("model") == "historical-replay-stub":
        raise DailyAutomationError("缺少真实模型生成的V2选题，请先运行 select-v2")
    if not research:
        raise DailyAutomationError("缺少V2正文证据产物")
    script = generate_script_v2(selection, research)
    json_path = run_dir / "daily_script_v2_test.json"
    markdown_path = run_dir / "daily_script_v2_test.md"
    _atomic_json(json_path, script)
    _atomic_write_text(markdown_path, _markdown(script))
    return {"script": script, "json_artifact": str(json_path), "markdown_artifact": str(markdown_path), "legacy_script_untouched": True}
