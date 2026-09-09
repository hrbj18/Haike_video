"""Apply the fact-checked, compressed script revision through the workbench API."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "mihoyo-ai-girlfriend-remake-1"
STATE_PATH = ROOT / "projects" / PROJECT_ID / "artifacts" / "workbench.json"


SECTIONS = [
    {
        "id": "sec_01",
        "label": "一个住进桌面的角色",
        "sentences": [
            "最懂陪伴的AI女友，为什么反而不秒回你？",
            "米哈游此前在Steam测试了BSide林离。",
            "主角林离生活在上海，主修钢琴、辅修心理学，喜欢黑胶、老电影和雨天。",
            "不少用户把她称作AI女友。",
        ],
    },
    {
        "id": "sec_02",
        "label": "打开应用就是打开日常",
        "sentences": [
            "打开应用，林离会直接出现在电脑桌面。",
            "她会弹琴、看书、发呆，房间也跟着现实时间变化。",
            "早晨天亮，傍晚落日，凌晨变成都市夜景。",
            "你可以听她弹琴，也能写信交流，或者上传MIDI文件。",
        ],
    },
    {
        "id": "sec_03",
        "label": "角色开始进入生活",
        "sentences": [
            "这款应用没有开放世界、战斗和抽卡。",
            "林离进入桌面以后，陪伴不会随着游戏窗口关闭。",
            "你写代码，她在一旁练琴；你处理文件，她低头翻乐谱。",
            "她有自己的节奏，也不会一直催你互动。",
        ],
    },
    {
        "id": "sec_04",
        "label": "等待也是产品设计",
        "sentences": [
            "这个设计最特别的地方，就是等待。",
            "很多AI伴侣都在争夺响应速度，林离会让用户等一等。",
            "秒回带来效率，等待保留距离。",
            "她更像一个有自己生活的人，关系感也来自这段无法随时控制的距离。",
        ],
    },
    {
        "id": "sec_05",
        "label": "把MIDI变成角色演奏",
        "sentences": [
            "另一个变化发生在音乐上。",
            "产品提供近一百三十首曲目，也允许上传MIDI文件。",
            "系统读取琴键、力度和时长，再把数据映射到林离的手指、手腕和身体动作。",
            "她会按照用户提供的乐谱完成一段演奏。",
        ],
    },
    {
        "id": "sec_06",
        "label": "用户看见谁在为我弹",
        "sentences": [
            "任何播放器都能播放同一首肖邦。",
            "林离会让用户感到，这首曲子由一个熟悉的角色专门演奏。",
            "用户得到的除了音乐，还有谁在为我弹。",
            "这种看得见的个性化表演，让普通内容带上了关系感。",
        ],
    },
    {
        "id": "sec_07",
        "label": "存在感与分寸感",
        "sentences": [
            "原视频记录的当时数据里，Steam有七百多条评论，好评率百分之八十三，同时在线峰值超过七千二百人。",
            "一个没有战斗和抽卡、也不追求即时回应的应用，仍然让大量用户把她留在桌面。",
            "AI陪伴的吸引力，可能就藏在存在感和分寸感里。",
        ],
    },
]


def request_json(method: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:4754/api/project/{PROJECT_ID}/workbench{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    draft = state["project"]["script_draft"]
    result = request_json("PATCH", "/script-draft/content", {
        "expected_revision": draft["revision"],
        "title": "最懂陪伴的AI女友，反而不秒回你",
        "sections": SECTIONS,
    })
    revised = result["project"]["script_draft"]
    texts = [item["text"] for item in revised["script"]["sections"]]
    print(json.dumps({
        "status": revised["status"],
        "revision": revised["revision"],
        "estimated_duration_seconds": revised["script"]["total_duration_seconds"],
        "character_count": sum(len(item) for item in texts),
        "sections": revised["script"]["sections"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
