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


# 調査のセッションで書き込みのできる Bash のコマンドと、拒否の規則のなかった書き込み先（D-071）。accept edits に
# 切り替えると、拒否の規則のないパスへのファイル操作が確認なしで動いた（mkdir inbox/sub）ため、拒否の一覧に足す
WRITE_COMMANDS = (
    "mkdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "rmdir",
    "ln",
    "tee",
    "sed",
    "chmod",
    "truncate",
    "dd",
    "install",
)
# `inbox/` の下のフォルダーへの書き込みは `Edit(/inbox/*/*)` で止める。`Edit(/inbox/*/**)` は `inbox/` 直下の
# ファイルにも当たり、許可した .json・.sql の書き込みまで拒否した（D-084。v2.1.319 で再現）
EXTRA_EDIT_DENY = ("Edit(/.git/**)", "Edit(/inbox/*/*)")


def investigate_settings() -> dict:
    import json

    return json.loads((REPO / "investigate.settings.json").read_text(encoding="utf-8"))


def test_kn3_investigate_settings_doc_matches_file() -> None:
    """KN-3：docs/claude_code_config.md の2節（investigate.settings.json）の囲みの中身が、実ファイルと同じ
    （JSON として比べる）。"""
    import json

    lines = DOC.read_text(encoding="utf-8").split("\n")
    assert json.loads(fenced_body(lines, "## 2. ")) == investigate_settings()


def test_kn3_investigate_settings_deny_write_commands() -> None:
    """KN-3（D-071）：調査用の設定の拒否の一覧に、書き込みのできる Bash のコマンドと、`.git/` と `inbox/` の下の
    フォルダーへの書き込みがある。`inbox/` 直下の .json・.sql の許可はそのまま。"""
    permissions = investigate_settings()["permissions"]
    deny = set(permissions["deny"])
    for command in WRITE_COMMANDS:
        assert f"Bash({command} *)" in deny, command
    for rule in EXTRA_EDIT_DENY:
        assert rule in deny, rule
    assert {"Edit(/inbox/*.json)", "Edit(/inbox/*.sql)"} <= set(permissions["allow"])
    # 回帰（D-084）：inbox/ 直下のファイルに当たる拒否を置かない
    assert "Edit(/inbox/*/**)" not in deny
    assert "Edit(/inbox/**)" not in deny


def test_kn3_d085_reporting_language() -> None:
    """KN-3（D-085）：CLAUDE.md に、記者への質問・途中の説明・最終報告を日本語で書く指示がある。"""
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert (
        "- 記者への質問・途中の説明・最終報告は日本語で書く。"
        "引用、コード、識別子、エラー原文は原表記を保つ。"
    ) in text


def test_kn3_d086_table_quote_guidance() -> None:
    """KN-3（D-086）：investigate の Skill に、表のページの引用で対象の行と支える部分を示す指示がある。
    引用の決まり（文末まで）は変えない。"""
    text = (REPO / ".claude" / "skills" / "investigate" / "SKILL.md").read_text(encoding="utf-8")
    assert "改行を文末とみなさない" in text
    assert "資料ID・ページ・契約件名" in text
    assert "どの部分が判定を支えるか" in text
