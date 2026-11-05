"""照合の工程のテスト（implementation_spec.md 4.1 の reconcile_import・reconcile_source、7.4 の記録の形。
test_plan.md の IR-2 と、P0-3 の完了条件「合成データで照合の記録が7.4の形で残る」）。

期待値は、合成データ（tests/fixtures/make_cao_fixtures.py の表と、make_pdf_cells.py の PDF の表）から
手で決めた値。合成の PDF は CSV と同じ表で、CSV の `?` だけが PDF では `−`（相手方の2件）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from conftest import NAME_WORDS, URL, Site, all_text_in_ledger, fixture_bytes, ledger_of
from conftest import write_config as _write_config
from kizashi import ledger, reconcile, runner
from kizashi.adapters import cao_procurement, load_adapter

KYOSO_FIELDS = [
    "amount_yen",
    "contract_date",
    "corp_numbers",
    "counterparty_raw",
    "method_raw",
    "title_raw",
]
ZUIKEI_FIELDS = [
    "amount_yen",
    "contract_date",
    "corp_numbers",
    "counterparty_raw",
    "reason_raw",
    "title_raw",
]


def start(repo: Path, site: Site, **config: object) -> dict:
    return runner.start_run(
        repo, "cao-procurement", _write_config(repo, **config), transport=site.transport
    )


def computations(repo: Path, run_id: str) -> list[dict]:
    """照合の計算（kind='reconcile'）。検出（detect）の自動の基礎集計・派生計算は含めない。"""
    conn = ledger_of(repo, run_id)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM computations WHERE kind = 'reconcile' ORDER BY calc_id"
        )
    ]
    conn.close()
    for r in rows:
        r["request"] = json.loads(r["request_json"])
        r["result"] = json.loads(r["result_json"])
    return rows


def field_counts(match: int, mismatch: int = 0, unverifiable: int = 0) -> dict:
    return {"match": match, "mismatch": mismatch, "unverifiable": unverifiable}


# ---------------------------------------------------------------------------
# 完了条件：合成データで照合の記録が7.4の形で残る
# ---------------------------------------------------------------------------


@pytest.fixture
def completed(repo: Path, site: Site) -> dict:
    return start(repo, site)


def test_run_has_reconcile_stages(repo: Path, completed: dict) -> None:
    """工程 reconcile_import・reconcile_source が done になり、その後に検出（detect。P0-5）が続く。"""
    assert completed["state"] == "completed"
    assert list(completed["stages"]) == [
        "plan",
        "fetch",
        "normalize",
        "quality",
        "reconcile_import",
        "reconcile_source",
        "detect",
    ]
    assert all(s["state"] == "done" for s in completed["stages"].values())
    assert "stages_not_run" not in completed
    calcs = computations(repo, completed["run_id"])
    # 資料ごとに取り込みの照合を1つ（K0001〜K0004）、資料の組ごとに原本照合を1つ（K0005〜K0008）
    assert [(c["calc_id"], c["kind"], c["origin"], c["lead_id"]) for c in calcs] == [
        (f"K{i:04d}", "reconcile", "run", None) for i in range(1, 9)
    ]
    assert [c["result"]["type"] for c in calcs] == ["import"] * 4 + ["source"] * 4
    assert all(c["meaning"] for c in calcs)


def test_reconcile_import_all_rows_match(repo: Path, completed: dict) -> None:
    """IR-2 の対照：正しく正規化した合成データでは、取り込みの照合はすべての行で一致する。"""
    calcs = computations(repo, completed["run_id"])[:4]
    rows_per_source = {"S001": 12, "S003": 9, "S005": 12, "S007": 7}
    assert [c["result"]["source"] for c in calcs] == list(rows_per_source)
    for calc in calcs:
        sid = calc["result"]["source"]
        n = rows_per_source[sid]
        result = calc["result"]
        assert result["counts"]["rows"] == n
        assert (result["counts"]["match"], result["counts"]["mismatch"]) == (n, 0)
        assert sorted(result["rows"]) == [f"{sid}:{i:06d}" for i in range(1, n + 1)]
        assert calc["row_count"] == n
        assert json.loads(calc["row_refs_json"]) == sorted(result["rows"])
        assert all(r["row_status"] == "match" for r in result["rows"].values())
    first = calcs[0]["result"]["rows"]["S001:000001"]["fields"]
    assert first == {
        "no": "match",
        "title_raw": "match",
        "contract_date_raw": "match",
        "counterparty_raw": "match",
        "corp_number_raw": "match",
        "method_raw": "match",
        "amount_raw": "match",
        "amount_yen": "match",
    }
    assert "reason_raw" in calcs[1]["result"]["rows"]["S003:000001"]["fields"]


EXPECTED_SOURCE = {
    # csv: (pdf, 件数, ページ, 項目ごとの (match, mismatch, unverifiable))
    "S001": (
        "S002",
        12,
        3,
        {
            "amount_yen": field_counts(12),  # 数値11件と単価の記載1件（NO 8）
            "contract_date": field_counts(12),
            "title_raw": field_counts(12),
            "counterparty_raw": field_counts(11, 1),  # NO 10 の `?`
            "corp_numbers": field_counts(2, 0, 10),  # 13桁は NO 2・7 だけ
            "method_raw": field_counts(12),
        },
    ),
    "S003": (
        "S004",
        9,
        2,
        {
            "amount_yen": field_counts(9),  # 数値6件と外貨・単価・その他の文字列の3件
            "contract_date": field_counts(9),
            "title_raw": field_counts(9),
            "counterparty_raw": field_counts(9),
            "corp_numbers": field_counts(3, 0, 6),  # 13桁は NO 1・2・5
            "reason_raw": field_counts(9),
        },
    ),
    "S005": (
        "S006",
        12,
        3,
        {
            "amount_yen": field_counts(12),
            "contract_date": field_counts(12),
            "title_raw": field_counts(12),
            "counterparty_raw": field_counts(11, 1),  # NO 9 の `?`
            "corp_numbers": field_counts(2, 0, 10),  # 13桁は NO 2・4
            "method_raw": field_counts(12),
        },
    ),
    "S007": (
        "S008",
        7,
        2,
        {
            "amount_yen": field_counts(7),
            "contract_date": field_counts(7),
            "title_raw": field_counts(7),  # NO 6 の康熙部首の「⼊」も NFKC で一致
            "counterparty_raw": field_counts(7),
            "corp_numbers": field_counts(7),
            "reason_raw": field_counts(7),
        },
    ),
}


@pytest.mark.parametrize("csv_source", sorted(EXPECTED_SOURCE))
def test_reconcile_source_record_shape_and_counts(
    repo: Path, completed: dict, csv_source: str
) -> None:
    """完了条件：原本照合の記録が7.4の形（type・pairs・rows・counts）で残り、件数が手で数えた値と合う。"""
    pdf, n, pages, fields = EXPECTED_SOURCE[csv_source]
    calcs = {
        c["result"]["pairs"][0]["csv_source"]: c
        for c in computations(repo, completed["run_id"])[4:]
    }
    result = calcs[csv_source]["result"]
    assert set(result) == {"type", "pairs", "rows", "counts"}
    assert result["type"] == "source"
    assert result["pairs"] == [
        {
            "csv_source": csv_source,
            "pdf_source": pdf,
            "pages": pages,
            "skipped_pages": [],
            "split_pages": [],
        }
    ]
    assert result["counts"] == {
        "rows": n,
        "matched": n,
        "not_found": 0,
        "ambiguous": 0,
        "skipped": 0,
        "fields": fields,
    }
    assert sorted(result["rows"]) == [f"{csv_source}:{i:06d}" for i in range(1, n + 1)]
    # 1ページに5件（6件目から2ページ目、11件目から3ページ目）
    for i in range(1, n + 1):
        row = result["rows"][f"{csv_source}:{i:06d}"]
        assert row["row_status"] == "matched"
        assert row["pdf_page"] == (i - 1) // 5 + 1
    expected_fields = KYOSO_FIELDS if csv_source in ("S001", "S005") else ZUIKEI_FIELDS
    assert sorted(result["rows"][f"{csv_source}:000001"]["fields"]) == expected_fields


def test_reconcile_source_specific_rows(repo: Path, completed: dict) -> None:
    """S001 の NO 10（相手方の `?`）は相手方だけが mismatch。指数表記の法人番号は unverifiable。"""
    calcs = computations(repo, completed["run_id"])
    rows = calcs[4]["result"]["rows"]
    assert rows["S001:000010"]["fields"] == {
        "amount_yen": "match",
        "contract_date": "match",
        "title_raw": "match",
        "counterparty_raw": "mismatch",
        "corp_numbers": "unverifiable",
        "method_raw": "match",
    }
    assert rows["S001:000002"]["fields"]["corp_numbers"] == "match"
    assert rows["S001:000008"]["fields"]["amount_yen"] == "match"  # 単価の記載の完全一致
    assert (
        calcs[4]["request"]["csv_source"] == "S001" and calcs[4]["request"]["pdf_source"] == "S002"
    )


def test_run_output_and_counts_have_reconcile_summary(repo: Path, completed: dict) -> None:
    """実行の出力と runs.counts_json に、照合の計算IDと件数が入る。"""
    assert completed["reconcile_import"]["S001"] == {
        "calc_id": "K0001",
        "rows": 12,
        "match": 12,
        "mismatch": 0,
    }
    source = completed["reconcile_source"]["S003"]
    assert source["calc_id"] == "K0006" and source["pdf_source"] == "S004"
    assert (source["rows"], source["matched"], source["not_found"], source["ambiguous"]) == (
        9,
        9,
        0,
        0,
    )
    conn = ledger_of(repo, completed["run_id"])
    counts = json.loads(conn.execute("SELECT counts_json FROM runs").fetchone()[0])
    conn.close()
    assert counts["reconcile_import"] == completed["reconcile_import"]
    assert counts["reconcile_source"] == completed["reconcile_source"]


def test_reconcile_records_have_no_names(repo: Path, completed: dict) -> None:
    """照合の記録に、担当官の氏名（PDF の担当官の列の文字）が入らない。"""
    conn = ledger_of(repo, completed["run_id"])
    text = all_text_in_ledger(conn)
    conn.close()
    for word in NAME_WORDS:
        assert word not in text, word


def test_stage_events_include_reconcile(repo: Path, completed: dict) -> None:
    conn = ledger_of(repo, completed["run_id"])
    stages = [
        json.loads(e["after_json"]).get("stage")
        for e in conn.execute("SELECT * FROM events WHERE kind = 'stage' ORDER BY event_id")
    ]
    conn.close()
    assert stages == [
        "plan",
        "fetch",
        "normalize",
        "quality",
        "reconcile_import",
        "reconcile_source",
        "detect",
    ]


# ---------------------------------------------------------------------------
# PDF が読めない、または取得できていない場合
# ---------------------------------------------------------------------------


def test_unreadable_pdf_rows_are_not_found(repo: Path) -> None:
    """PDF を読めなければ、その組の全行を not_found（照合不能）とし、理由を記録する。成功にしない。"""
    site = Site({URL["k24_pdf"]: httpx.Response(200, content=b"%PDF-1.4\n% broken\n")})
    result = start(repo, site)
    assert result["state"] == "completed"
    calc = next(
        c
        for c in computations(repo, result["run_id"])
        if c["result"]["type"] == "source" and c["result"]["pairs"][0]["csv_source"] == "S001"
    )
    pair = calc["result"]["pairs"][0]
    assert pair["pdf_source"] == "S002" and "読めない" in pair["error"]
    counts = calc["result"]["counts"]
    assert (counts["rows"], counts["matched"], counts["not_found"]) == (12, 0, 12)
    assert counts["fields"]["amount_yen"] == field_counts(0, 0, 12)
    row = calc["result"]["rows"]["S001:000001"]
    assert row["row_status"] == "not_found" and row["row_reason"] == "pdf_unreadable"
    assert set(row["fields"].values()) == {"unverifiable"}
    assert "error" in result["reconcile_source"]["S001"]


def test_missing_pdf_is_not_run(repo: Path) -> None:
    """PDF を取得できなかった CSV は、原本照合を not_run と理由で記録し、照合の計算を作らない（実行は partial）。"""
    site = Site({URL["k24_pdf"]: httpx.Response(404)})
    result = start(repo, site)
    assert result["state"] == "partial"
    entry = result["reconcile_source"]["S001"]
    assert entry["state"] == "not_run" and "S002" in entry["reason"]
    pairs = [
        c["result"]["pairs"][0]["csv_source"]
        for c in computations(repo, result["run_id"])
        if c["result"]["type"] == "source"
    ]
    assert pairs == ["S003", "S005", "S007"]
    # 取り込みの照合は PDF に関係なく行う
    assert result["reconcile_import"]["S001"]["match"] == 12


# ---------------------------------------------------------------------------
# IR-2：わざと列をずらした正規化の結果を、取り込みの照合（T6a）が不一致として出す
# ---------------------------------------------------------------------------


def test_ir2_shifted_columns_are_detected(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """IR-2：正規化の経路で契約金額（10列目）と予定価格（9列目）の番号を入れ替えると、別経路の読み込み処理
    （見出しで列を特定）との比較で、金額が不一致になる。

    手で決めた期待値（競争入札2024の12件）：予定価格と契約金額の元の値はどの行でも違うので、amount_raw は
    12件とも不一致。整数にした金額は、NO 8（予定価格「-」と金額「単価契約…」がどちらも整数にならない）だけ一致し、
    11件が不一致。ほかの項目はすべて一致。
    """
    run_id = "20261105-120000-test"
    conn = ledger.create(repo, run_id)
    data = fixture_bytes("kyoso_2024_mini.csv")
    sha = hashlib.sha256(data).hexdigest()
    raw = repo / "data" / "raw"
    raw.mkdir(parents=True)
    (raw / f"{sha}.csv").write_bytes(data)
    key = "cao-procurement|2024|kyoso|csv"
    conn.execute(
        "INSERT INTO sources (source_id, logical_key, kind, url, status, sha256)"
        " VALUES ('S001', ?, 'dataset', 'https://www.cao.go.jp/x.csv', 'fetched', ?)",
        (key, sha),
    )
    adapter = load_adapter(repo, "cao-procurement")
    spec = adapter.file_spec(2024, "kyoso")
    take = {k: dict(v) for k, v in spec.take.items()}
    take["amount_raw"]["index"], take["estimate_raw"]["index"] = 9, 10
    shifted = dataclasses.replace(spec, take=take)
    # 見出しの検査（Q02）を通さずに、ずれた列で正規化する
    monkeypatch.setattr(cao_procurement, "format_problems", lambda table, spec: [])
    cao_procurement.normalize_source(
        conn, adapter, source_id="S001", logical_key=key, data=data, spec=shifted
    )
    summary = reconcile.reconcile_import(conn, repo, adapter)
    assert summary["S001"]["rows"] == 12
    assert (summary["S001"]["match"], summary["S001"]["mismatch"]) == (0, 12)
    result = json.loads(
        conn.execute("SELECT result_json FROM computations WHERE kind = 'reconcile'").fetchone()[0]
    )
    conn.close()
    fields = result["counts"]["fields"]
    assert fields["amount_raw"] == {"match": 0, "mismatch": 12}
    assert fields["amount_yen"] == {"match": 1, "mismatch": 11}
    for name in ("no", "title_raw", "contract_date_raw", "counterparty_raw", "corp_number_raw"):
        assert fields[name] == {"match": 12, "mismatch": 0}, name
    assert result["rows"]["S001:000008"]["fields"]["amount_yen"] == "match"
    assert result["rows"]["S001:000001"]["row_status"] == "mismatch"


def test_ir2_missing_ledger_row_is_mismatch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """IR-2：台帳に行がない論理レコード（正規化の経路が1行を落とした）も不一致として出す。"""
    original = cao_procurement.normalize_source

    def drop_last(conn, adapter, **kwargs):
        counts = original(conn, adapter, **kwargs)
        if kwargs["source_id"] == "S001":
            conn.execute("DELETE FROM procurement_contracts WHERE record_id = 'S001:000012'")
            conn.execute("DELETE FROM records WHERE record_id = 'S001:000012'")
        return counts

    run_id = "20261105-120001-test"
    conn = ledger.create(repo, run_id)
    data = fixture_bytes("kyoso_2024_mini.csv")
    sha = hashlib.sha256(data).hexdigest()
    (repo / "data" / "raw").mkdir(parents=True)
    (repo / "data" / "raw" / f"{sha}.csv").write_bytes(data)
    key = "cao-procurement|2024|kyoso|csv"
    conn.execute(
        "INSERT INTO sources (source_id, logical_key, kind, url, status, sha256)"
        " VALUES ('S001', ?, 'dataset', 'https://www.cao.go.jp/x.csv', 'fetched', ?)",
        (key, sha),
    )
    adapter = load_adapter(repo, "cao-procurement")
    monkeypatch.setattr(cao_procurement, "normalize_source", drop_last)
    cao_procurement.normalize_source(
        conn,
        adapter,
        source_id="S001",
        logical_key=key,
        data=data,
        spec=adapter.file_spec(2024, "kyoso"),
    )
    summary = reconcile.reconcile_import(conn, repo, adapter)
    result = json.loads(
        conn.execute("SELECT result_json FROM computations WHERE kind = 'reconcile'").fetchone()[0]
    )
    conn.close()
    assert (summary["S001"]["rows"], summary["S001"]["match"], summary["S001"]["mismatch"]) == (
        12,
        11,
        1,
    )
    assert result["rows"]["S001:000012"] == {
        "row_status": "mismatch",
        "row_reason": "missing_in_ledger",
        "fields": {},
    }
