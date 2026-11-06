"""構成文書と実ファイルの一致（test_plan.md の KN-3。D-045②・D-046）。

docs/claude_code_config.md の5〜8節は、CLAUDE.md と Skill のファイルを「そのまま置ける形」で載せている。節の囲み
（``` か ````）の中身が、リポジトリの実ファイルの中身と同じであることを確かめる（片方だけを直して食い違わないため）。
"""

from __future__ import annotations

import pytest

from conftest import REPO

DOC = REPO / "docs" / "claude_code_config.md"
SECTIONS = {
    "## 5. ": "CLAUDE.md",
    "## 6. ": ".claude/skills/investigate/SKILL.md",
    "## 7. ": ".claude/skills/investigate/record-examples.md",
    "## 8. ": ".claude/skills/review-lead/SKILL.md",
}


def fenced_body(lines: list[str], heading: str) -> str:
    """見出しの後の最初の囲みの中身。閉じる行は、開いた行と同じ数のバッククォートだけの行。"""
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    opening = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("```"))
    fence = lines[opening][: len(lines[opening]) - len(lines[opening].lstrip("`"))]
    closing = next(i for i in range(opening + 1, len(lines)) if lines[i] == fence)
    return "\n".join(lines[opening + 1 : closing])


@pytest.mark.parametrize("heading", list(SECTIONS))
def test_kn3_config_doc_matches_files(heading: str) -> None:
    """KN-3：docs/claude_code_config.md の5〜8節の囲みの中身が、実ファイルと同じ。"""
    lines = DOC.read_text(encoding="utf-8").split("\n")
    actual = (REPO / SECTIONS[heading]).read_text(encoding="utf-8").rstrip("\n")
    assert fenced_body(lines, heading) == actual, (
        f"{SECTIONS[heading]} と docs/claude_code_config.md の {heading.strip()} 節が違う"
    )


def test_kn3_fence_is_not_used_inside() -> None:
    """KN-3：囲みの中のファイルに、囲みと同じ数のバッククォートだけの行がない（あると節の終わりを取り違える）。"""
    lines = DOC.read_text(encoding="utf-8").split("\n")
    for heading, name in SECTIONS.items():
        start = next(i for i, line in enumerate(lines) if line.startswith(heading))
        opening = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("```"))
        fence = lines[opening][: len(lines[opening]) - len(lines[opening].lstrip("`"))]
        body = (REPO / name).read_text(encoding="utf-8").split("\n")
        assert fence not in body, (name, fence)
