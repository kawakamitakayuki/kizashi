"""探索用の読み取り専用 SQL（test_plan.md の QU-1。implementation_spec.md 4.4）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ledger_of
from kizashi import check, limits, query
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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM procurement_contracts WHERE group_key = '架空　太郎'",
        "-- 架空太郎の担当の契約\nSELECT fiscal_year FROM procurement_contracts",
    ],
)
def test_qu1_personal_name_in_sql_is_e_v7(repo: Path, synthetic_run: str, sql: str) -> None:
    """QU-1（D-037）：SQL の全文（文字列・コメントを含む）に氏名があれば、実行せずに E_V7。
    accepted/ にも queries/ にも保存せず、提案は inbox/ に残す。エラーに氏名を書かない。"""
    arg = write_sql(repo, "q-009.sql", sql)
    with pytest.raises(KizashiError) as info:
        query.run_query(repo, synthetic_run, arg)
    err = info.value
    assert (err.code, err.stage) == ("E_V7", "query")
    assert "V7_PERSONAL_NAME" in err.message
    assert "架空" not in err.message and "太郎" not in err.message
    run_dir = repo / "data" / "runs" / synthetic_run
    assert (repo / arg).is_file()
    assert not (run_dir / "accepted" / "q-009.sql").exists()
    assert not (run_dir / "queries").exists()
    conn = ledger_of(repo, synthetic_run)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'query'").fetchone()[0] == 0


def assert_rejected_by_name(repo: Path, run_id: str, arg: str, err: KizashiError) -> None:
    """E_V7 で拒否し、氏名を書かず、accepted/・queries/ に保存せず、成功の events もなく、提案を inbox/ に残す。"""
    assert (err.code, err.stage) == ("E_V7", "query")
    assert "V7_PERSONAL_NAME" in err.message
    for word in ("架空", "太郎"):
        assert word not in err.message and word not in err.next_action
    run_dir = repo / "data" / "runs" / run_id
    assert (repo / arg).is_file()
    assert not (run_dir / "accepted" / Path(arg).name).exists()
    assert not (run_dir / "queries").exists()
    conn = ledger_of(repo, run_id)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'query'").fetchone()[0] == 0
    conn.close()


def test_qu1_personal_name_in_result_is_e_v7(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-040②）：SQL の本文に氏名がなくても、文字列の結合で結果のセルに氏名ができれば、保存・events・返却の
    前に E_V7 にする。エラーには行と列の番号だけを書く。"""
    sql = "SELECT 1 AS n, '架空' || '太郎' AS x UNION ALL SELECT 2, 'ふつうの値'"
    arg = write_sql(repo, "q-010.sql", sql)
    with pytest.raises(KizashiError) as info:
        query.run_query(repo, synthetic_run, arg)
    assert_rejected_by_name(repo, synthetic_run, arg, info.value)
    assert "結果の1行目・2列目" in info.value.message


def test_qu1_personal_name_in_sqlite_error_is_e_v7(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-040②）：SQLite のエラーの文に氏名ができれば（json_extract の誤ったパス）、元の文を出さずに E_V7。
    氏名のないエラーの文は、これまでどおり E_QUERY_REJECTED で返す。"""
    arg = write_sql(repo, "q-011.sql", "SELECT json_extract('{}', '架空' || '太郎');")
    with pytest.raises(KizashiError) as info:
        query.run_query(repo, synthetic_run, arg)
    assert_rejected_by_name(repo, synthetic_run, arg, info.value)
    assert "SQLite のエラーの文" in info.value.message

    arg = write_sql(repo, "q-012.sql", "SELECT json_extract('{}', 'ふつうの' || 'パス');")
    with pytest.raises(KizashiError) as info:
        query.run_query(repo, synthetic_run, arg)
    assert info.value.code == "E_QUERY_REJECTED"
    assert "JSON path" in info.value.message  # 氏名のない元の文は出す


def test_qu1_result_name_hits_cover_column_names() -> None:
    """QU-1（D-040②）：列名にも氏名の検査を当てる（列名は SQL の本文から来るので、本文の検査の後の念のため）。
    数値と BLOB の表示は検査しない。位置だけを返す。"""
    names = check.name_variants([("架空", "太郎")])  # 空白なし・半角・全角の空白の3つの形
    assert query.result_name_hits(["n", "架空 太郎"], [[1, "x"]], names) == [
        "V7_PERSONAL_NAME：列名の2列目 の 1〜5 文字目"
    ]
    assert query.result_name_hits(["n"], [[1], [2, None, "<BLOB 3 bytes>"]], names) == []
    assert query.result_name_hits(["a", "b"], [["x", "y"], ["z", "前に架空太郎"]], names) == [
        "V7_PERSONAL_NAME：結果の2行目・2列目 の 3〜6 文字目"
    ]
