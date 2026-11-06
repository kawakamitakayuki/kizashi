"""判断ルールの出典と知識ファイルの一致（test_plan.md の KN-1）。

.claude/skills/review-lead/sources.md の版・ID・題名が、references/ の原本と一致することを確かめる。
- 冒頭の一覧：「<略称>v<版>：`references/<ファイル>`（<題名>）」の題名が原本の1行目の見出しと、版が原本の
  「バージョン：<版>」と、ファイル名の版と同じ。
- 表の「<略称>v<版> M<番号>「<題名>」」は、原本の見出しの行「### M<番号> <題名>」と同じ。
- 「C<番号>「<題名>」」は、原本の「### C<番号>」の次の太字の行「**<題名>**」と同じ。
references/ は読むだけで書き換えない（CLAUDE.md）。
"""

from __future__ import annotations

import re

import pytest

from conftest import REPO

SOURCES = REPO / ".claude" / "skills" / "review-lead" / "sources.md"
_FILE_RE = re.compile(r"^- (\S+?)v(\d+\.\d+)：`(references/[^`]+)`（(.+)）$")
# 表のセルの中の「<略称>v<版>」と「<ID>「<題名>」」（題名の中の「」を含む。題名の終わりは「、」か行の終わりの前）
_CELL_RE = re.compile(r"(ノウハウ集|事例・手法集)v\d+\.\d+|([MC]\d{2,3})「(.+?)」(?=、|$)")


def _files() -> dict[str, tuple[str, str, str]]:
    """略称 → (版, パス, 題名)。"""
    found = {}
    for line in SOURCES.read_text(encoding="utf-8").splitlines():
        match = _FILE_RE.match(line)
        if match:
            found[match.group(1)] = (match.group(2), match.group(3), match.group(4))
    return found


def _headings(path: str) -> dict[str, str]:
    """原本の M・C の ID → 題名（M は見出しの行、C は見出しの次の太字の行）。"""
    lines = (REPO / path).read_text(encoding="utf-8").splitlines()
    titles: dict[str, str] = {}
    for i, line in enumerate(lines):
        match = re.match(r"^### ([MC]\d{2,3})(?: (.+))?$", line)
        if not match:
            continue
        if match.group(1).startswith("M"):
            titles[match.group(1)] = (match.group(2) or "").strip()
        else:
            bold = next((x for x in lines[i + 1 : i + 4] if x.strip()), "")
            titles[match.group(1)] = bold.strip().removeprefix("**").removesuffix("**")
    return titles


def test_kn1_files_versions_and_titles() -> None:
    """KN-1：冒頭の一覧の各ファイルがあり、題名・版が原本の見出しと「バージョン」の行と同じ。"""
    files = _files()
    assert set(files) == {"ノウハウ集", "事例・手法集"}
    for version, path, title in files.values():
        text = (REPO / path).read_text(encoding="utf-8")
        assert text.splitlines()[0] == f"# {title}", path
        assert re.search(rf"^バージョン：{re.escape(version)}\s*$", text, re.MULTILINE), path
        assert path.endswith(f"_v{version}.md"), path


def _table_refs() -> list[tuple[str, str | None, str, str]]:
    """表の行ごとの (規, 略称, ID, 題名)。略称は、その ID の前に書かれたもの。"""
    refs = []
    for line in SOURCES.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| 規"):
            continue
        rule, body = (c.strip() for c in line.strip().strip("|").split("|", 1))
        current = None
        for match in _CELL_RE.finditer(body.strip()):
            if match.group(1):
                current = match.group(1)
            else:
                refs.append((rule, current, match.group(2), match.group(3)))
    return refs


def test_kn1_table_ids_and_titles_match_originals() -> None:
    """KN-1：表の M・C の ID と題名が、略称の原本の見出し（M）・見出しの次の太字の行（C）と同じ。"""
    files = _files()
    refs = _table_refs()
    assert len(refs) >= 20  # 表の15行に、ID 付きの出典が少なくとも20ある
    originals = {name: _headings(path) for name, (_, path, _) in files.items()}
    for rule, name, ident, title in refs:
        assert name in originals, (rule, ident)
        assert originals[name].get(ident) == title, (rule, name, ident)


@pytest.mark.parametrize("name", ["ノウハウ集", "事例・手法集"])
def test_kn1_original_has_ids(name: str) -> None:
    """KN-1：照らす原本の見出しを読めている（見出しの形が変わって0件になるのを防ぐ）。"""
    _, path, _ = _files()[name]
    titles = _headings(path)
    if name == "ノウハウ集":
        assert {"M10", "M12", "C024", "C114"} <= set(titles)
