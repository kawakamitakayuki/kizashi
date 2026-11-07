"""別セッションでの読み直しの起動（test_plan.md の RV-1。P1-a。設計書9.3、実装仕様13章）。

scripts/review-memo を一時フォルダーのリポジトリの形に写し、引数を記録するだけの合成の claude を PATH の先頭に
置いて動かす。本物の Claude Code は起動しない（通信なし）。起動したセッションで書けないことと MCP の道具が
ないことは、README の手順で利用者が確かめる（このテストの範囲の外）。
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import REPO

SCRIPT = REPO / "scripts" / "review-memo"
RUN_ID = "20261105-101500-a3f2"
LEAD_ID = "L0007"

# 設計書9.3の起動の引数。`--tools` と `--disallowedTools` は値を複数取るので、最初の指示の前に `--` を置く。
EXPECTED_OPTIONS = [
    "--permission-mode",
    "dontAsk",
    "--setting-sources",
    "project",
    "--tools",
    "Read,Grep,Glob",
    "--disallowedTools",
    "mcp__*",
    "--",
]

FAKE_CLAUDE = """#!/usr/bin/env bash
pwd > "$REVIEW_MEMO_LOG.cwd"
printf '%s\\0' "$@" > "$REVIEW_MEMO_LOG"
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """合成のリポジトリ：スクリプトと、1つの実行のメモ（同じ候補の v1・v2・v10、ほかの候補）とページ別テキスト。"""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(SCRIPT, root / "scripts" / "review-memo")
    (root / "scripts" / "review-memo").chmod(stat.S_IRWXU)
    memos = root / "data" / "runs" / RUN_ID / "memos"
    memos.mkdir(parents=True)
    for name in ("L0007.v1.md", "L0007.v2.md", "L0007.v10.md", "L0007.vx.md", "L0008.v99.md"):
        (memos / name).write_text(f"# 合成のメモ {name}\n", encoding="utf-8")
    pages = root / "data" / "runs" / RUN_ID / "evidence" / "S001" / "pages"
    pages.mkdir(parents=True)
    (pages / "1.txt").write_text("合成の資料の1ページ。\n", encoding="utf-8")
    return root


def run_script(repo: Path, tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    """リポジトリの外のフォルダーからスクリプトを動かす（スクリプトがリポジトリの直下に移ることも確かめる）。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "claude").write_text(FAKE_CLAUDE, encoding="utf-8")
    (fake_bin / "claude").chmod(stat.S_IRWXU)
    log = tmp_path / "claude-args"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    env = dict(
        os.environ, PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}", REVIEW_MEMO_LOG=str(log)
    )
    proc = subprocess.run(
        [str(repo / "scripts" / "review-memo"), *args],
        check=False,
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc, log


def read_args(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").split("\0")[:-1]


def test_rv1_launches_read_only_session_with_latest_memo(repo: Path, tmp_path: Path) -> None:
    """RV-1：設計書9.3の引数の後に `--` と最初の指示を置き、リポジトリの直下で起動する。メモは番号を数で比べた最新。"""
    proc, log = run_script(repo, tmp_path, RUN_ID, LEAD_ID)
    assert proc.returncode == 0, proc.stderr
    args = read_args(log)
    assert args[:-1] == EXPECTED_OPTIONS
    assert len(args) == len(EXPECTED_OPTIONS) + 1
    assert Path(Path(f"{log}.cwd").read_text(encoding="utf-8").strip()) == repo

    prompt = args[-1]
    assert f"data/runs/{RUN_ID}/memos/L0007.v10.md" in prompt
    for other in ("L0007.v1.md", "L0007.v2.md", "L0007.vx.md", "L0008"):
        assert other not in prompt
    assert f"data/runs/{RUN_ID}/evidence/" in prompt
    assert "<資料ID>/pages/<ページ番号>.txt" in prompt
    assert RUN_ID in prompt and LEAD_ID in prompt
    for ask in (
        "「否定」の判定を、提示された根拠が十分に支えていない箇所",
        "言い過ぎ",
        "根拠の位置が主張を支えていない箇所",
        "書き込みはしない",
    ):
        assert ask in prompt, ask
    assert "独立した検証ではない" in prompt


def test_rv1_run_without_page_texts(repo: Path, tmp_path: Path) -> None:
    """RV-1：ページ別テキストのない実行でも起動し、最初の指示にページ別テキストがないことを書く。"""
    shutil.rmtree(repo / "data" / "runs" / RUN_ID / "evidence")
    proc, log = run_script(repo, tmp_path, RUN_ID, LEAD_ID)
    assert proc.returncode == 0, proc.stderr
    prompt = read_args(log)[-1]
    assert "ページ別テキストはない" in prompt
    assert f"data/runs/{RUN_ID}/evidence/" not in prompt


@pytest.mark.parametrize(
    "args",
    [
        (),
        (RUN_ID,),
        (RUN_ID, LEAD_ID, "extra"),
        (LEAD_ID, RUN_ID),
        ("../" + RUN_ID, LEAD_ID),
        ("20261105-101500-A3F2", LEAD_ID),
        (RUN_ID, "L007"),
        (RUN_ID, "C0007"),
        (RUN_ID, "L0007/../L0008"),
        (RUN_ID, "L0007\n"),
    ],
)
def test_rv1_rejects_bad_arguments(repo: Path, tmp_path: Path, args: tuple[str, ...]) -> None:
    """RV-1：引数の数の誤りと ID の書式（3.3）に合わない引数は終了コード2で、Claude Code を起動しない。"""
    proc, log = run_script(repo, tmp_path, *args)
    assert proc.returncode == 2, proc.stderr
    assert not log.exists()
    assert "scripts/review-memo <run_id> <lead_id>" in proc.stderr


@pytest.mark.parametrize(
    ("run_id", "lead_id"),
    [("20261105-101500-zzzz", LEAD_ID), (RUN_ID, "L0009")],
)
def test_rv1_missing_run_or_memo(repo: Path, tmp_path: Path, run_id: str, lead_id: str) -> None:
    """RV-1：実行のフォルダーか候補のメモがなければ終了コード3で、Claude Code を起動しない。"""
    proc, log = run_script(repo, tmp_path, run_id, lead_id)
    assert proc.returncode == 3, proc.stderr
    assert not log.exists()
    assert "kizashi render" in proc.stderr
