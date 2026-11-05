"""探索用の読み取り専用 SQL（test_plan.md の QU-1。implementation_spec.md 4.4）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ledger_of
from kizashi import limits, query
from kizashi.errors import KizashiError

SIXTY = (
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 60) SELECT x FROM n"
)


def write_sql(root: Path, name: str, sql: str) -> str:
    (root / "inbox" / name).write_text(sql, encoding="utf-8")
    return f"inbox/{name}"


def test_qu1_select_is_saved_and_not_citable(repo: Path, synthetic_run: str) -> None:
    """QU-1：SELECT を実行し、結果に citable: false。SQL と結果を queries/ に残し、accepted/ に移す。"""
    arg = write_sql(
        repo,
        "q-001.sql",
        "-- 翻訳業務の年度ごとの金額\nSELECT fiscal_year, amount_yen FROM procurement_contracts"
        " WHERE group_key = '翻訳業務' ORDER BY fiscal_year;\n",
    )
    result = query.run_query(repo, synthetic_run, arg)
    assert result["citable"] is False
    assert result["columns"] == ["fiscal_year", "amount_yen"]
    assert result["rows"] == [[2024, 650000], [2025, 750000]]
    assert (result["row_count"], result["truncated"]) == (2, False)
    run_dir = repo / "data" / "runs" / synthetic_run
    assert not (repo / arg).exists()
    assert (run_dir / "accepted" / "q-001.sql").is_file()
    saved = run_dir / result["saved"]
    assert saved.suffix == ".json" and saved.with_suffix(".sql").is_file()
    assert json.loads(saved.read_text(encoding="utf-8"))["rows"] == [[2024, 650000], [2025, 750000]]
    conn = ledger_of(repo, synthetic_run)
    events = conn.execute("SELECT kind, after_json FROM events WHERE kind = 'query'").fetchall()
    assert len(events) == 1 and json.loads(events[0]["after_json"])["query_file"] == "q-001.sql"


def test_qu1_cuts_at_fifty_rows(repo: Path, synthetic_run: str) -> None:
    """QU-1：結果は最大50行。超えた分は返さず truncated を true にする。"""
    result = query.run_query(repo, synthetic_run, write_sql(repo, "q-002.sql", SIXTY))
    assert limits.QUERY_ROW_LIMIT == 50
    assert result["row_count"] == 50 and result["truncated"] is True
    assert result["rows"][-1] == [50]


def test_qu1_semicolon_in_string_is_one_statement(repo: Path, synthetic_run: str) -> None:
    """QU-1：文字列の中の ; は文の区切りでない。"""
    result = query.run_query(repo, synthetic_run, write_sql(repo, "q-003.sql", "SELECT ';' AS x"))
    assert result["rows"] == [[";"]]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO events (target_id, kind, at) VALUES ('x', 'query', 'now')",
        "UPDATE leads SET state = 'change'",
        "DELETE FROM computations",
        "CREATE TABLE t (x)",
        "DROP TABLE runs",
        "ATTACH DATABASE 'other.sqlite' AS other",
        "PRAGMA table_info(runs)",
        "PRAGMA query_only = OFF",
        "VACUUM INTO 'copy.sqlite'",
        "SELECT load_extension('x')",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE runs",
        "WITH t AS (SELECT 1) INSERT INTO events (target_id, kind, at) SELECT 'x', 'query', 'now'",
        "SELECT * FROM no_such_table",
        "SELECT 'unterminated",
        "-- コメントだけ",
        "",
    ],
)
def test_qu1_rejects_writes_and_multiple_statements(
    repo: Path, synthetic_run: str, sql: str
) -> None:
    """QU-1：書き込み、ATTACH、PRAGMA、拡張の読み込み、複数の文、読めない SQL を拒否する。

    拒否したら、SQL は inbox/ に残り、queries/ にも accepted/ にも何も書かない。台帳は変わらない。
    """
    before = ledger_of(repo, synthetic_run).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    arg = write_sql(repo, "q-bad.sql", sql)
    with pytest.raises(KizashiError) as info:
        query.run_query(repo, synthetic_run, arg)
    assert info.value.code == "E_QUERY_REJECTED"
    assert (repo / arg).exists()
    run_dir = repo / "data" / "runs" / synthetic_run
    assert not (run_dir / "queries").exists() or not any((run_dir / "queries").iterdir())
    assert not (run_dir / "accepted" / "q-bad.sql").exists()
    assert not (run_dir / "copy.sqlite").exists() and not (repo / "copy.sqlite").exists()
    after = ledger_of(repo, synthetic_run).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert after == before


def test_qu1_timeout(repo: Path, synthetic_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """QU-1：実行時間が上限を超えたら中断して E_QUERY_TIMEOUT（上限を0秒にして確かめる）。"""
    monkeypatch.setattr(limits, "QUERY_TIMEOUT_SEC", 0)
    heavy = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 10000000)"
        " SELECT COUNT(*) FROM n"
    )
    arg = write_sql(repo, "q-heavy.sql", heavy)
    with pytest.raises(KizashiError) as info:
        query.run_query(repo, synthetic_run, arg)
    assert info.value.code == "E_QUERY_TIMEOUT"
    assert (repo / arg).exists()


def test_qu1_same_name_in_accepted_is_rejected(repo: Path, synthetic_run: str) -> None:
    """QU-1：accepted/ に同じ名前の探索SQLがあれば、上書きせずに拒否する（候補の query_file が指すため）。"""
    query.run_query(repo, synthetic_run, write_sql(repo, "q-001.sql", "SELECT 1"))
    arg = write_sql(repo, "q-001.sql", "SELECT 2")
    with pytest.raises(KizashiError) as info:
        query.run_query(repo, synthetic_run, arg)
    assert info.value.code == "E_QUERY_REJECTED"
    accepted = repo / "data" / "runs" / synthetic_run / "accepted" / "q-001.sql"
    assert accepted.read_text(encoding="utf-8") == "SELECT 1"
