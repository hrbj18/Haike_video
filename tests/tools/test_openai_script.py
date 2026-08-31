from tools.text.openai_script import OpenAIScript


def test_script_modes_have_distinct_non_template_prompts() -> None:
    faithful = OpenAIScript._system_prompt("organize_script", "faithful")
    polished = OpenAIScript._system_prompt("organize_script", "light_polish")
    expanded = OpenAIScript._system_prompt("expand_idea", "faithful")
    created = OpenAIScript._system_prompt("from_scratch", "faithful")

    assert "不得新增事实" in faithful
    assert "尽量保留原句顺序" in faithful
    assert "轻微补充衔接" in polished
    assert "允许从一个想法扩展" in expanded
    assert "根据标题独立创作" in created
    for prompt in (faithful, polished, expanded, created):
        assert "不要机械套用“开场/展开/重点/收束”" in prompt


def test_organize_mode_uses_lower_creativity_than_generation_modes() -> None:
    assert OpenAIScript._temperature("organize_script", "faithful") < OpenAIScript._temperature("organize_script", "light_polish")
    assert OpenAIScript._temperature("organize_script", "light_polish") < OpenAIScript._temperature("expand_idea", "faithful")
    assert OpenAIScript._temperature("expand_idea", "faithful") <= OpenAIScript._temperature("from_scratch", "faithful")
