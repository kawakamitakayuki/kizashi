"""bf1bede の実装レビューの修正のうち、氏名と取り込まない列の値の検査の回帰テスト（D-090①②・1-2・1-3・1-6）。

test_plan.md の QU-1・CK-4・CF-1 の回帰。期待値は implementation_spec.md の 4.1（normalize）、4.4（query）、
9章の V7（氏名と取り込まない列の値。D-032）と、決定の D-090①②から書き写した。取り込まない列の値は合成データの
担当官の所在地（tests/fixtures/make_cao_fixtures.py の OFFICER_ADDR）と相手方の住所（CP_ADDR）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import URL, Site, calc_sum, ledger_of, put, write_config
from kizashi import check, query, record, runner
from kizashi.errors import KizashiError

CP_ADDR = "テスト県模擬市本町２－２"  # 相手方の住所のセル（取り込まない列。index 6）
OFFICER_ADDR_LINE = "テスト県架空市官庁町１－１"  # 担当官の所在地のセルの1行（index 3）


def write_sql(root: Path, name: str, sql: str) -> str:
    (root / "inbox" / name).write_text(sql, encoding="utf-8")
    return f"inbox/{name}"


def assert_not_saved(root: Path, run_id: str, arg: str, exc: KizashiError) -> None:
    """E_V7 で、値を書かず、queries/・accepted/ に保存せず、提案は inbox/ に残る（D-037・D-040②）。"""
    assert exc.code == "E_V7"
    assert "模擬市" not in exc.message and "官庁町" not in exc.message
    run = root / "data" / "runs" / run_id
    name = Path(arg).name
    assert not (run / "accepted" / name).exists()
    assert not any(name.removesuffix(".sql") in p.name for p in (run / "queries").glob("*"))
    assert (root / arg).is_file()


# ---------------------------------------------------------------------------
# D-090①（QU-1）：探索SQL の本文・結果・エラーの文に、取り込まない列の値の検査も当てる
# ---------------------------------------------------------------------------


def test_qu1_excluded_value_in_sql_body(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-090①）：コメントに相手方の住所を書いた探索SQL は実行せず E_V7。"""
    arg = write_sql(repo, "q-1.sql", f"-- {CP_ADDR}\nSELECT COUNT(*) FROM records")
    with pytest.raises(KizashiError) as exc:
        query.run_query(repo, synthetic_run, arg)
    assert_not_saved(repo, synthetic_run, arg, exc.value)
    assert "取り込まない列の値" in exc.value.message


def test_qu1_excluded_value_in_result_cell(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-090①）：文字列の結合で結果のセルに住所ができれば、保存・返却の前に E_V7（位置だけを書く）。"""
    arg = write_sql(repo, "q-2.sql", "SELECT 'テスト県模擬市' || '本町２－２' AS v")
    with pytest.raises(KizashiError) as exc:
        query.run_query(repo, synthetic_run, arg)
    assert_not_saved(repo, synthetic_run, arg, exc.value)
    assert "結果の1行目・1列目" in exc.value.message


def test_qu1_excluded_value_in_sqlite_error(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-090①）：SQLite のエラーの文に担当官の所在地ができても E_V7 にし、元の文を出さない。"""
    arg = write_sql(
        repo, "q-3.sql", "SELECT json_extract('{}', 'テスト県架空市' || '官庁町１－１')"
    )
    with pytest.raises(KizashiError) as exc:
        query.run_query(repo, synthetic_run, arg)
    assert_not_saved(repo, synthetic_run, arg, exc.value)
    assert "SQLite のエラーの文" in exc.value.message


def test_qu1_plain_query_still_runs(repo: Path, synthetic_run: str) -> None:
    """QU-1（D-090①）：住所も氏名もない探索SQL は、これまでどおり実行して保存する。"""
    arg = write_sql(repo, "q-4.sql", "SELECT COUNT(*) AS n FROM procurement_contracts")
    result = query.run_query(repo, synthetic_run, arg)
    assert result["citable"] is False


# ---------------------------------------------------------------------------
# 1-6（QU-1・CK-4）：原本の CSV がなければ、query と record は E_V7（想定外のエラーにしない）
# ---------------------------------------------------------------------------


def _remove_raw_csv(repo: Path) -> None:
    for path in (repo / "data" / "raw").glob("*.csv"):
        path.unlink()


def test_qu1_missing_raw_csv_is_e_v7(repo: Path, synthetic_run: str) -> None:
    """QU-1（1-6）：氏名の一覧を作る原本がなければ、検査を飛ばさず E_V7（終了コード1）。保存しない。"""
    _remove_raw_csv(repo)
    arg = write_sql(repo, "q-5.sql", "SELECT COUNT(*) FROM records")
    with pytest.raises(KizashiError) as exc:
        query.run_query(repo, synthetic_run, arg)
    assert_not_saved(repo, synthetic_run, arg, exc.value)
    assert exc.value.exit_code == 1


def test_ck4_record_missing_raw_csv_is_e_v7(repo: Path, synthetic_run: str) -> None:
    """CK-4（1-6）：record の時点の V7 も、原本がなければ E_V7。台帳を変えず、提案は inbox/ に残る。"""
    _remove_raw_csv(repo)
    arg = put(repo, "k.json", calc_sum("翻訳業務", 2024))
    conn = ledger_of(repo, synthetic_run)
    before = conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0]
    conn.close()
    with pytest.raises(KizashiError) as exc:
        record.record(repo, synthetic_run, arg)
    assert exc.value.code == "E_V7"
    conn = ledger_of(repo, synthetic_run)
    assert conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0] == before
    conn.close()
    assert (repo / arg).is_file()


# ---------------------------------------------------------------------------
# D-090①（CF-1・CK-4）：実行設定にも取り込まない列の値の検査を当てる
# ---------------------------------------------------------------------------


def _failed_run(repo: Path) -> tuple[dict, list[dict]]:
    conn = ledger_of(repo, _only_run(repo))
    run = dict(conn.execute("SELECT * FROM runs").fetchone())
    events = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY event_id")]
    conn.close()
    return run, events


def _only_run(repo: Path) -> str:
    (path,) = list((repo / "data" / "runs").iterdir())
    return path.name


def test_cf1_excluded_value_in_run_config(repo: Path, site: Site) -> None:
    """CF-1・CK-4（D-090①）：問いに相手方の住所があれば、normalize の最初に E_V7 で failed にする。台帳に設定の
    全文を書かず、住所が残らない。"""
    config = write_config(repo, question=f"{CP_ADDR}の業者との契約は増えたか")
    with pytest.raises(KizashiError) as exc:
        runner.start_run(repo, "cao-procurement", config, transport=site.transport)
    assert exc.value.code == "E_V7"
    assert "模擬市" not in exc.value.message
    run, events = _failed_run(repo)
    assert run["state"] == "failed"
    assert "模擬市" not in run["config_json"] and "模擬市" not in (run["stop_reason"] or "")
    assert json.loads(run["config_json"])["name_check"] == "pending"
    assert not any("模擬市" in json.dumps(e, ensure_ascii=False) for e in events)


# ---------------------------------------------------------------------------
# D-090②（1-3。CF-1・CK-4）：取得済みの CSV が0本なら、氏名の検査を passed にせず E_V7
# ---------------------------------------------------------------------------


def test_ck4_no_fetched_csv_is_e_v7_and_config_not_saved(repo: Path) -> None:
    """CK-4（D-090②）：CSV を1本も取得できなければ、氏名の一覧を作れないので E_V7。設定の全文を保存せず、
    検査済み（passed）の events を残さない（「原本なし」を「氏名0件」と区別する）。"""
    site = Site({URL[k]: httpx.Response(404) for k in ("k24_csv", "z24_csv", "k25_csv", "z25_csv")})
    config = write_config(repo, question="架空太郎の担当した契約は増えたか")
    with pytest.raises(KizashiError) as exc:
        runner.start_run(repo, "cao-procurement", config, transport=site.transport)
    assert exc.value.code == "E_V7"
    run, events = _failed_run(repo)
    assert run["state"] == "failed"
    stored = json.loads(run["config_json"])
    assert stored["name_check"] == "pending"
    assert "question" not in stored
    assert "架空太郎" not in run["config_json"]
    assert json.loads(run["stop_reason"])["stage"] == "normalize"
    assert json.loads(run["stop_reason"])["code"] == "E_V7"
    passed = [
        e
        for e in events
        if e["kind"] == "check" and json.loads(e["after_json"] or "{}").get("result") == "passed"
    ]
    assert passed == []
    assert (repo / config).is_file()  # 設定ファイルは inbox/ に残る


# ---------------------------------------------------------------------------
# 1-2（CK-4）：check の時点の V7 は、CLI が書いた文字列にも氏名と取り込まない列の値を当てる
# ---------------------------------------------------------------------------


def _calc_from(repo: Path, run_id: str, lead_id: str) -> str:
    conn = ledger_of(repo, run_id)
    (calc_id,) = conn.execute(
        "SELECT calc_from FROM leads WHERE lead_id = ?", (lead_id,)
    ).fetchone()
    conn.close()
    return calc_id


def _set_meaning(repo: Path, run_id: str, calc_id: str, suffix: str) -> None:
    conn = ledger_of(repo, run_id)
    conn.execute(
        "UPDATE computations SET meaning = meaning || ? WHERE calc_id = ?", (suffix, calc_id)
    )
    conn.commit()
    conn.close()


def _check_failures(repo: Path, run_id: str, lead_id: str) -> list[dict]:
    try:
        check.run_check(repo, run_id, lead_id)
    except KizashiError as exc:
        return exc.failures or []
    return []


@pytest.mark.parametrize("suffix", ["（架空 太郎）", f"（{CP_ADDR}）"])
def test_ck4_cli_written_calc_meaning_is_checked_for_names(
    repo: Path, synthetic_run: str, suffix: str
) -> None:
    """CK-4（1-2）：run が作った計算（origin='run'）の意味に氏名・取り込まない列の値があれば
    V7_PERSONAL_NAME（位置だけを書く）。"""
    calc_id = _calc_from(repo, synthetic_run, "L0001")
    _set_meaning(repo, synthetic_run, calc_id, suffix)
    failures = _check_failures(repo, synthetic_run, "L0001")
    hits = [f for f in failures if f["code"] == "V7_PERSONAL_NAME"]
    assert any(f"{calc_id}.meaning" in f["message"] for f in hits)
    assert all("太郎" not in f["message"] and "模擬市" not in f["message"] for f in failures)


def test_ck4_cli_written_calc_meaning_is_not_checked_for_numbers(
    repo: Path, synthetic_run: str
) -> None:
    """CK-4（1-2）：CLI が書いた文字列には数値の検査を当てない（V7_RAW_NUMBER にしない。9章の V7）。"""
    calc_id = _calc_from(repo, synthetic_run, "L0001")
    _set_meaning(repo, synthetic_run, calc_id, "（参考 123件）")
    failures = _check_failures(repo, synthetic_run, "L0001")
    assert not any(
        f["code"] == "V7_RAW_NUMBER" and f"{calc_id}.meaning" in f["message"] for f in failures
    )
