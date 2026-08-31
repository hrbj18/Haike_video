"""OpenAI-compatible script drafting tool for the human review workbench."""

from __future__ import annotations

import json
import os
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class OpenAIScript(BaseTool):
    name = "openai_script"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "script_generation"
    provider = "openai"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["python:openai", "env:OPENAI_API_KEY"]
    install_instructions = "配置 OPENAI_API_KEY；可通过 OPENAI_BASE_URL 使用兼容接口。"
    agent_skills = ["openai-docs"]
    capabilities = ["draft_script", "expand_idea", "organize_script"]
    supports = {"chinese": True, "structured_json": True, "openai_compatible_base_url": True}
    best_for = ["从主题或想法生成可审核的分段脚本草案", "整理用户提供的零散脚本"]
    not_good_for = ["未经人工审核直接进入正式生产"]
    input_schema = {
        "type": "object",
        "required": ["mode", "title"],
        "properties": {
            "mode": {"type": "string", "enum": ["organize_script", "expand_idea", "from_scratch"]},
            "organize_strength": {"type": "string", "enum": ["faithful", "light_polish"]},
            "title": {"type": "string"},
            "duration_seconds": {"type": ["number", "null"]},
            "audience": {"type": "string"},
            "content_goal": {"type": "string"},
            "brief": {"type": "string"},
            "idea": {"type": "string"},
            "script_text": {"type": "string"},
            "style_direction": {"type": "string"},
            "style_reference": {"type": "string"},
            "avatar_turn_contract": {"type": "array"},
            "model": {"type": "string"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=1, network_required=True)
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["mode", "organize_strength", "title", "duration_seconds", "brief", "idea", "script_text"]
    side_effects = ["calls OpenAI-compatible text API"]
    user_visible_verification = ["人工审核脚本结构、时长、表达和画面意图"]

    def get_status(self) -> ToolStatus:
        if os.environ.get("OPENAI_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.02

    @staticmethod
    def _model_name(inputs: dict[str, Any]) -> str:
        # Keep the gateway-specific default overridable for other compatible
        # endpoints. The configured relay exposes gpt-5.6-luna.
        return str(inputs.get("model") or os.environ.get("OPENAI_SCRIPT_MODEL") or "gpt-5.6-luna")

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        cleaned = (content or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(cleaned[start:end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("脚本返回内容不是对象")
        return parsed

    @staticmethod
    def _system_prompt(mode: str, organize_strength: str = "faithful") -> str:
        common = """你是中文短视频编导。请根据输入制作一份可人工审核的脚本草案。
只输出 JSON，不要 Markdown，不要解释。JSON 必须包含：
{
  \"version\": \"1.0\",
  \"title\": string,
  \"total_duration_seconds\": number,
  \"voice_performance\": {\"performance_intent\": string, \"pacing_profile\": \"conversational\" },
  \"sections\": [
    {\"id\": string, \"turn_id\": string|null, \"speaker_id\": string|null, \"speaker_name\": string|null, \"label\": string, \"text\": string, \"start_seconds\": number, \"end_seconds\": number, \"speaker_directions\": string, \"enhancement_cues\": [{\"type\": \"overlay\"|\"broll\"|\"diagram\"|\"stat_card\"|\"code_snippet\"|\"animation\", \"description\": string, \"timestamp_seconds\": number}]}
  ]
}
共同要求：使用简体中文；有目标时长时按目标时长合理分段，没有目标时长时按内容完整度自然控制篇幅；每段文字适合口播；每段都说明画面意图；不要编造具体事实、数据或书籍内容；不要机械套用“开场/展开/重点/收束”的四段标签，应按内容语义自然命名和分段。"""
        if mode == "organize_script":
            if organize_strength == "light_polish":
                strategy = """当前 mode 是 organize_script，整理强度是 light_polish：保留用户主题、事实、观点和主要顺序；允许轻微补充衔接、修正口语病句和合并重复表达，但不得新增事实、结论、案例或改变立场；不要为了完整感另造开头和结尾。"""
            else:
                strategy = """当前 mode 是 organize_script，整理强度是 faithful：不得新增事实、观点、案例、结论或改变语气；尽量保留原句顺序和信息密度，只做断句、错别字、明显语病、重复语句和自然分段整理；输入没有开头或结尾时也不要擅自补写。"""
        elif mode == "expand_idea":
            strategy = """当前 mode 是 expand_idea：允许从一个想法扩展为完整口播稿，可以选择解释、故事、观点、清单或问答结构；补充内容必须是稳妥的通用表达，对不确定事实保持克制，不得伪造数据、人物或出处。"""
        else:
            strategy = """当前 mode 是 from_scratch：根据标题独立创作一份完整口播稿，并结合用户的可选方向；可以自由选择最适合主题的叙事结构，但不得伪造数据、人物、新闻进展或出处。"""
        return f"{common}\n本次专用策略：{strategy}"

    @staticmethod
    def _temperature(mode: str, organize_strength: str = "faithful") -> float:
        if mode == "organize_script":
            return 0.2 if organize_strength == "faithful" else 0.35
        if mode == "expand_idea":
            return 0.65
        return 0.75

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not os.environ.get("OPENAI_API_KEY"):
            return ToolResult(success=False, error="未配置 OPENAI_API_KEY")
        model = self._model_name(inputs)
        try:
            from openai import OpenAI

            base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
            client = OpenAI(base_url=base_url) if base_url else OpenAI()
            context = {
                key: inputs.get(key, "")
                for key in (
                    "mode", "title", "duration_seconds", "audience", "content_goal",
                    "brief", "idea", "script_text", "style_direction", "style_reference",
                    "avatar_turn_contract",
                )
            }
            mode = str(inputs.get("mode") or "from_scratch")
            organize_strength = str(inputs.get("organize_strength") or "faithful")
            context["organize_strength"] = organize_strength
            avatar_contract = context.get("avatar_turn_contract") or []
            request = {
                "model": model,
                "temperature": self._temperature(mode, organize_strength),
                "messages": [
                    {"role": "system", "content": self._system_prompt(mode, organize_strength)},
                    *([{"role": "system", "content": "这是双主持数字人脚本。sections 必须与 avatar_turn_contract 一一对应，数量、顺序、turn_id、speaker_id、speaker_name 均不得改变、合并、删除或新增；每个 section 只包含一个轮次。"}] if avatar_contract else []),
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
            }
            try:
                response = client.chat.completions.create(
                    **request, response_format={"type": "json_object"}
                )
            except Exception as exc:
                # Some compatible gateways implement chat completions but not
                # response_format. Retry only for that specific failure.
                message = str(exc).lower()
                if "response_format" not in message and "json_object" not in message:
                    raise
                response = client.chat.completions.create(**request)
            content = (response.choices[0].message.content or "").strip()
            script = self._parse_json_content(content)
            self._validate_script(script)
            return ToolResult(success=True, data={"provider": "openai", "model": model, "script": script})
        except (json.JSONDecodeError, ValueError) as exc:
            return ToolResult(success=False, data={"model": model}, error=f"脚本返回格式无效：{exc}")
        except Exception as exc:
            return ToolResult(success=False, data={"model": model}, error=f"脚本接口调用失败：{exc}")

    @staticmethod
    def _validate_script(script: Any) -> None:
        if not isinstance(script, dict) or not isinstance(script.get("sections"), list) or not script["sections"]:
            raise ValueError("缺少有效的 sections")
        required = {"id", "text", "start_seconds", "end_seconds"}
        for section in script["sections"]:
            if not isinstance(section, dict) or not required.issubset(section):
                raise ValueError("脚本分段缺少必要字段")
