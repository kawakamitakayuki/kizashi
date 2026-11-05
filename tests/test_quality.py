"""品質検査のテスト（test_plan.md の QC-1・QC-2。Q01〜Q16 の件数）。

期待値は、合成データ（tests/fixtures/make_cao_fixtures.py の表）から手で数えた値。
金額の合計は、合成データの金額を手で足した値（Decimal の計算の出力ではない）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from conftest import NAME_WORDS, URL, Site, fixture_bytes, ledger_of, write_config
from kizashi import quality, runner
from kizashi.errors import KizashiError


@pytest.fixture
def completed(repo: Path, site: Site) -> dict:
    config = write_config(repo)
    return runner.start_run(repo, "cao-procurement", config, transport=site.transport)


def quality_of(result: dict, repo: Path) -> dict:
    conn = ledger_of(repo, result["run_id"])
    counts = json.loads(conn.execute("SELECT counts_json FROM runs").fetchone()[0])
    conn.close()
    return counts["quality"]


# 資料ID：S001 競争入札2024（CSV）、S003 随意契約2024、S005 競争入札2025、S007 随意契約2025（plan の順）
EXPECTED = {
    "S001": {
        "Q05": 0,
        "Q06": 5,
        "Q07": {"digits13": 2, "exponent": 7, "missing": 1, "multiple": 1, "invalid": 1},
        "Q08": (0, 0),
        "Q09": (0, 0),
        "Q10": 4,
        "Q11": {"missing": 0, "foreign_currency": 0, "unit_price_text": 1, "non_numeric": 0},
        "Q12": ["S001:000009"],
        "Q13": (12, 12, 11, "24520000"),
        "Q15": (3, 1, 4),
    },
    "S003": {
        "Q05": 0,
        "Q06": 3,
        "Q07": {"digits13": 3, "exponent": 3, "missing": 1, "multiple": 1, "invalid": 1},
        "Q08": (0, 0),
        "Q09": (0, 0),
        "Q10": 1,
        "Q11": {"missing": 0, "foreign_currency": 1, "unit_price_text": 1, "non_numeric": 1},
        "Q12": [],
        "Q13": (9, 9, 6, "17490000"),
        "Q15": (5, 0, 5),
    },
    "S005": {
        "Q05": 0,
        "Q06": 2,
        "Q07": {"digits13": 2, "exponent": 10, "missing": 0, "multiple": 0, "invalid": 0},
        "Q08": (1, 2),
        "Q09": (1, 2),
        "Q10": 2,
        "Q11": {"missing": 0, "foreign_currency": 0, "unit_price_text": 0, "non_numeric": 0},
        "Q12": [],
        "Q13": (12, 12, 12, "22260000"),
        "Q15": (2, 0, 2),
    },
    "S007": {
        "Q05": 0,
        "Q06": 2,
        "Q07": {"digits13": 7, "exponent": 0, "missing": 0, "multiple": 0, "invalid": 0},
        "Q08": (0, 0),
        "Q09": (0, 0),
        "Q10": 1,
        "Q11": {"missing": 0, "foreign_currency": 1, "unit_price_text": 1, "non_numeric": 0},
        "Q12": [],
        "Q13": (7, 7, 5, "22750000"),
        "Q15": (3, 0, 3),
    },
}


@pytest.mark.parametrize("source_id", sorted(EXPECTED))
def test_quality_counts_match_hand_counts(repo: Path, completed: dict, source_id: str) -> None:
    """Q05〜Q15 の件数と金額の合計が、合成データから手で数えた値と合う。"""
    q = quality_of(completed, repo)[source_id]
    want = EXPECTED[source_id]
    for qid in ("Q01", "Q02", "Q03", "Q04"):
        assert q[qid]["state"] == "pass", qid
    assert q["Q05"]["count"] == want["Q05"] and q["Q05"]["state"] == "pass"
    assert q["Q06"]["distinct"] == want["Q06"]
    assert q["Q07"]["counts"] == want["Q07"]
    assert (q["Q08"]["groups"], q["Q08"]["rows"]) == want["Q08"]
    assert (q["Q09"]["groups"], q["Q09"]["rows"]) == want["Q09"]
    assert q["Q10"]["count"] == want["Q10"]
    assert q["Q11"]["counts"] == want["Q11"]
    assert q["Q12"]["row_refs"] == want["Q12"]
    q13 = q["Q13"]
    assert (
        q13["csv_records"],
        q13["ledger_records"],
        q13["amount_rows"],
        q13["amount_total"],
    ) == want["Q13"]
    assert q13["state"] == "partial" and q13["independent_reader"] == "not_run"
    assert (q["Q15"]["estimate_missing"], q["Q15"]["estimate_text"], q["Q15"]["rate_dash"]) == want[
        "Q15"
    ]
    assert q["Q14"]["missing"] == [] and q["Q14"]["duplicates"] == []
    assert q["Q16"]["partial_year"] is False


def test_quality_report_is_written_without_names(repo: Path, completed: dict) -> None:
    """品質レポートが出る。必須の検査の結果と Q13 の未実施が書かれ、氏名と所属の一覧は書かれない。"""
    report = (repo / completed["quality_report"]).read_text(encoding="utf-8")
    assert report.startswith("# 品質レポート")
    for sid in ("S001", "S003", "S005", "S007"):
        assert f"## {sid}" in report
    assert "別経路の読み込み処理との突合は未実施" in report
    assert "24,520,000円" in report
    for word in NAME_WORDS:
        assert word not in report, word
    # 所属の一覧（D-006 の確認まで書かない）
    assert "食品安全委員会事務局総務課長" not in report
    assert "所属の異なり数 5" in report


# ---------------------------------------------------------------------------
# QC-1：見出しが違うファイルで停止し、failed。必須の検査が実行できないと failed
# ---------------------------------------------------------------------------


def test_qc1_bad_header_fails_run(repo: Path) -> None:
    """QC-1：見出しを1つ変えたファイルで、実行は failed（E_FORMAT_CHANGED）。原本は data/raw に残る。"""
    bad = fixture_bytes("kyoso_bad_header.csv")
    site = Site({URL["k24_csv"]: httpx.Response(200, content=bad)})
    config = write_config(repo)
    with pytest.raises(KizashiError) as info:
        runner.start_run(repo, "cao-procurement", config, transport=site.transport)
    err = info.value
    assert err.code == "E_FORMAT_CHANGED"
    run_id = err.affected_ids[0]
    conn = ledger_of(repo, run_id)
    run = conn.execute("SELECT state, stop_reason, stages_json FROM runs").fetchone()
    assert run["state"] == "failed"
    assert json.loads(run["stop_reason"])["stage"] == "normalize"
    stages = json.loads(run["stages_json"])
    assert stages["normalize"]["state"] == "failed"
    assert "quality" not in stages
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert (
        conn.execute("SELECT status FROM sources WHERE source_id = 'S001'").fetchone()[0]
        == "fetched"
    )
    conn.close()
    assert (repo / "data" / "raw" / f"{hashlib.sha256(bad).hexdigest()}.csv").is_file()
    assert (repo / config).is_file()  # 失敗した実行の設定ファイルは inbox/ に残す


def test_qc1_required_check_not_runnable_fails(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QC-1：必須の検査が実行できない（品質検査の前に原本が消えた）と、実行は failed。"""
    original = quality._check_source

    def remove_raw_then_check(conn, root, adapter, source):
        (root / "data" / "raw" / f"{source['sha256']}.csv").unlink(missing_ok=True)
        return original(conn, root, adapter, source)

    config = write_config(repo)
    monkeypatch.setattr(quality, "_check_source", remove_raw_then_check)
    with pytest.raises(KizashiError) as info:
        runner.start_run(repo, "cao-procurement", config, transport=site.transport)
    assert info.value.code == "E_INTERNAL"
    assert "Q01" in info.value.message and "未実行" in info.value.message
    run_id = info.value.affected_ids[0]
    report = (repo / "data" / "runs" / run_id / "quality_report.md").read_text(encoding="utf-8")
    assert "実行は failed" in report


# ---------------------------------------------------------------------------
# QC-2：NO以外が同じ行の組を検出し、黙って除かない
# ---------------------------------------------------------------------------


def test_qc2_duplicate_rows_kept_with_reason(repo: Path, completed: dict) -> None:
    """QC-2：S005（競争入札2025）の NO 5・6 は同じ dup_group で、どちらも残り、duplicate_rows が付く。"""
    conn = ledger_of(repo, completed["run_id"])
    rows = conn.execute(
        "SELECT record_id, record_status, dup_group, incomparable_reasons_json FROM procurement_contracts"
        " WHERE dup_group IS NOT NULL ORDER BY record_id"
    ).fetchall()
    conn.close()
    assert [tuple(r)[:3] for r in rows] == [
        ("S005:000005", "adopted", "S005:000005"),
        ("S005:000006", "adopted", "S005:000005"),
    ]
    assert all(json.loads(r["incomparable_reasons_json"]) == ["duplicate_rows"] for r in rows)
