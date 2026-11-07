"""bf1bede の実装レビューの修正のうち、探索SQL の出力の上限の回帰テスト（D-090⑩）。

test_plan.md の QU-1 の回帰。期待値は implementation_spec.md の 3.2（本文の抜粋は1件2,000字）・4.4（最大50行）と、
決定の D-090⑩（50行と truncated を保ち、続きは SQL で範囲を絞る。セルは2,000字までで、省略を明示する。
氏名などの検査は省略する前の全文に当てる）から書き写した。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kizashi import query
from kizashi.errors import KizashiError


def write_sql(root: Path, name: str, sql: str) -> str:
    (root / "inbox" / name).write_text(sql, encoding="utf-8")
    return f"inbox/{name}"


def test_qu1_long_cell_is_shortened_with_notice(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-090⑩）：6,000字のセルは2,000字に切り、省略したことと元の字数をセルと結果の両方に書く。"""
    arg = write_sql(repo, "q-1.sql", "SELECT replace(hex(zeroblob(3000)), '0', 'あ') AS v, 1 AS n")
    result = query.run_query(repo, synthetic_run, arg)
    (cell, n) = result["rows"][0]
    assert n == 1
    assert cell == "あ" * 2000 + "…（以下略。全6000字）"
    assert result["cells_shortened"] == [{"row": 1, "column": 1, "chars": 6000}]
    assert result["truncated"] is False
    saved = json.loads(
        (repo / "data" / "runs" / synthetic_run / result["saved"]).read_text("utf-8")
    )
    assert saved["rows"][0][0] == cell
    assert saved["cells_shortened"] == result["cells_shortened"]


def test_qu1_cell_at_limit_is_kept(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-090⑩）：ちょうど2,000字のセルは切らない。"""
    arg = write_sql(repo, "q-2.sql", "SELECT replace(hex(zeroblob(1000)), '0', 'い') AS v")
    result = query.run_query(repo, synthetic_run, arg)
    assert result["rows"][0][0] == "い" * 2000
    assert result["cells_shortened"] == []


def test_qu1_name_after_the_limit_is_still_found(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-090⑩）：氏名の検査は省略する前の全文に当てる。2,000字より後ろの氏名でも E_V7 で、保存しない。"""
    arg = write_sql(
        repo,
        "q-3.sql",
        "SELECT replace(hex(zeroblob(1500)), '0', 'う') || '架空' || '太郎' AS v",
    )
    with pytest.raises(KizashiError) as exc:
        query.run_query(repo, synthetic_run, arg)
    assert exc.value.code == "E_V7"
    assert "3001〜3004 文字目" in exc.value.message
    assert (repo / arg).is_file()
    assert not list((repo / "data" / "runs" / synthetic_run / "queries").glob("*q-3*"))


def test_qu1_row_limit_tells_to_narrow_with_sql(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-090⑩）：50行を超えたら50行で切って truncated を true にし、続きは SQL で範囲を絞るよう書く
    （next_page は返さない）。"""
    arg = write_sql(
        repo,
        "q-4.sql",
        "WITH RECURSIVE t(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM t WHERE i < 60) SELECT i FROM t",
    )
    result = query.run_query(repo, synthetic_run, arg)
    assert (result["row_count"], result["truncated"]) == (50, True)
    assert "next_page" not in result
    assert "範囲を絞" in result["message"]
