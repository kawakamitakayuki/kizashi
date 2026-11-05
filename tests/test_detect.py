"""検出（run の detect）のテスト（test_plan.md の DT-1〜DT-5、SC-1（検出と母集団の数））。

期待値は implementation_spec.md 4.8・アダプタ仕様9章と、合成データ（tests/fixtures/make_cao_fixtures.py
の表）から手で数えた値。実装の出力を期待値にしない。

合成データの令和6・7年度の group_key（件名から年度・期・月分・か月間を除いたもの。手で求めた）：
- 両年度にある：庁舎清掃業務、荷物等の配送業務、システム保守業務、複写機保守、広報誌印刷業務、研修会場借上げ、
  消耗品の購入(単価契約)、会議録作成業務、庁舎警備業務、車両運行管理業務、翻訳業務、調査研究委託、
  統計データ作成業務、物品の購入、通訳業務、業務用車両の賃借、職員研修業務（17）
- S1 の比べた組（両年度とも合計できる行がある）：上のうち、荷物等・複写機保守・広報誌・消耗品(単価契約)・
  物品の購入・通訳業務を除く11。比較できない組は6
- S1 の候補：合計が変わった10組（研修会場借上げは同じ額）が amount_up、片方の年度だけ合計できる行がない
  荷物等の配送業務・広報誌印刷業務・物品の購入の3組が amount_incomparable
- S2：システム保守業務（令和6年度は競争入札、令和7年度は随意契約）と物品の購入（その逆）。比べた組は17
- S3：両年度に随意契約がある5組（調査研究委託、統計データ作成業務、通訳業務、業務用車両の賃借、職員研修業務）の
  うち、理由の分類が公募から企画競争に変わった調査研究委託
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

import httpx
import pytest

from conftest import URL, Site, ledger_of, write_config
from kizashi import detect, ledger, runner
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

SCREENS = ("s1_amount_change", "s2_method_change", "s3_reason_change")
COLUMNS = (
    "lead_key",
    "change_category",
    "metric",
    "period_from",
    "period_to",
    "row_refs_from",
    "row_refs_to",
    "excluded_from",
    "excluded_to",
    "incomparable_reasons",
)
AMOUNT_UP = (
    "システム保守業務",
    "会議録作成業務",
    "庁舎清掃業務",
    "庁舎警備業務",
    "業務用車両の賃借",
    "統計データ作成業務",
    "翻訳業務",
    "職員研修業務",
    "調査研究委託",
    "車両運行管理業務",
)
INCOMPARABLE = ("広報誌印刷業務", "物品の購入", "荷物等の配送業務")
EXPECTED_LEADS = {
    *(("s1_amount_change", k, "amount_up", "amount_yen_sum") for k in AMOUNT_UP),
    *(("s1_amount_change", k, "amount_incomparable", "amount_yen_sum") for k in INCOMPARABLE),
    ("s2_method_change", "システム保守業務", "kyoso_to_zuikei", "method_family"),
    ("s2_method_change", "物品の購入", "zuikei_to_kyoso", "method_family"),
    ("s3_reason_change", "調査研究委託", "reason_changed", "reason_category"),
}


def start(repo: Path, site: Site, **config: object) -> dict:
    return runner.start_run(
        repo, "cao-procurement", write_config(repo, **config), transport=site.transport
    )


def screen_sql(repo: Path, name: str, sub: str = "screens") -> Path:
    return repo / "datasets" / "cao-procurement" / sub / f"{name}.sql"


def lead_of(conn: sqlite3.Connection, screen: str, key: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM leads WHERE screen_id = ? AND lead_key = ?", (screen, key)
    ).fetchone()


def calc(conn: sqlite3.Connection, calc_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM computations WHERE calc_id = ?", (calc_id,)).fetchone()


def lead_calcs(conn: sqlite3.Connection, lead_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM computations WHERE lead_id = ? ORDER BY calc_id", (lead_id,)
    ).fetchall()


def counts_of(conn: sqlite3.Connection) -> dict:
    return json.loads(conn.execute("SELECT counts_json FROM runs").fetchone()[0])


# ---------------------------------------------------------------------------
# DT-1：検出SQLの形と候補
# ---------------------------------------------------------------------------


def test_dt1_screens_take_years_and_return_same_ten_columns(repo: Path, synthetic_run: str) -> None:
    """DT-1：3本とも :year_from・:year_to を受け取り、同じ10列を返す（年度を渡さなければ実行できない）。"""
    conn = sqlite3.connect(repo / "data" / "runs" / synthetic_run / "ledger.sqlite")
    for name in SCREENS:
        sql = screen_sql(repo, name).read_text(encoding="utf-8")
        cur = conn.execute(sql, {"year_from": 2024, "year_to": 2025})
        assert tuple(d[0] for d in cur.description) == COLUMNS, name
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute(sql, {})
    conn.close()


def test_dt1_s1_raw_output_has_excluded_rows_and_nulls(repo: Path, synthetic_run: str) -> None:
    """DT-1：S1 は単価契約・数値にならない金額の行を excluded_* に理由付きで返す。理由の配列には null が入る。"""
    conn = sqlite3.connect(repo / "data" / "runs" / synthetic_run / "ledger.sqlite")
    sql = screen_sql(repo, "s1_amount_change").read_text(encoding="utf-8")
    rows = {r[0]: r for r in conn.execute(sql, {"year_from": 2024, "year_to": 2025})}
    conn.close()
    assert set(rows) == set(AMOUNT_UP) | set(INCOMPARABLE)
    nimotsu = rows["荷物等の配送業務"]
    assert nimotsu[1] == "amount_incomparable"
    assert (json.loads(nimotsu[5]), json.loads(nimotsu[6])) == (["S001:000002"], ["S005:000002"])
    assert json.loads(nimotsu[7]) == [{"ref": "S001:000002", "reason": "unit_price"}]
    assert json.loads(nimotsu[8]) == []
    assert json.loads(nimotsu[9]) == [
        "no_summable_rows_one_side",
        "excluded_unit_price_or_non_numeric",
        None,
    ]
    buppin = rows["物品の購入"]
    assert json.loads(buppin[7]) == [{"ref": "S003:000003", "reason": "amount_not_numeric"}]
    assert json.loads(buppin[9]) == [
        "no_summable_rows_one_side",
        "excluded_unit_price_or_non_numeric",
        "duplicate_rows",
    ]
    assert json.loads(rows["庁舎清掃業務"][9]) == [None, None, None]


def test_dt1_leads_registered(repo: Path, synthetic_run: str) -> None:
    """DT-1：候補が (screen_id, lead_key, change_category, 年度) で登録され、比較できない理由から null が除かれ、
    行の理由（重複・読めない日付）が足される。同じ区分のほかの候補の数と、比較の条件の既定値が入る。"""
    conn = ledger_of(repo, synthetic_run)
    leads = conn.execute("SELECT * FROM leads ORDER BY lead_id").fetchall()
    assert [r["lead_id"] for r in leads] == [f"L{n:04d}" for n in range(1, 17)]
    assert {
        (r["screen_id"], r["lead_key"], r["change_category"], r["metric"]) for r in leads
    } == EXPECTED_LEADS
    assert all(
        (r["period_from"], r["period_to"], r["state"], r["query_file"])
        == ("2024", "2025", "candidate", None)
        for r in leads
    )
    peers = {"amount_up": 9, "amount_incomparable": 2}
    for r in leads:
        assert r["peer_count"] == peers.get(r["change_category"], 0), r["lead_key"]

    reasons = {
        (r["screen_id"], r["lead_key"]): json.loads(r["incomparable_reasons_json"]) for r in leads
    }
    s1 = "s1_amount_change"
    assert reasons[(s1, "荷物等の配送業務")] == [
        "excluded_unit_price_or_non_numeric",
        "no_summable_rows_one_side",
    ]
    assert reasons[(s1, "広報誌印刷業務")] == [
        "excluded_unit_price_or_non_numeric",
        "no_summable_rows_one_side",
    ]
    assert reasons[(s1, "物品の購入")] == [
        "duplicate_rows",
        "excluded_unit_price_or_non_numeric",
        "no_summable_rows_one_side",
    ]
    assert reasons[(s1, "会議録作成業務")] == ["bad_date"]  # S001:000009 の契約日 R6.13.1
    assert reasons[(s1, "庁舎清掃業務")] == []
    assert reasons[("s2_method_change", "物品の購入")] == ["duplicate_rows"]
    assert reasons[("s2_method_change", "システム保守業務")] == []
    assert reasons[("s3_reason_change", "調査研究委託")] == []

    defaults = load_adapter(repo, "cao-procurement").config["comparison_defaults"]
    for r in leads:
        assert json.loads(r["comparison_json"]) == defaults[r["screen_id"]]
    conn.close()


def test_dt1_s1_base_aggregates_and_derived(repo: Path, synthetic_run: str) -> None:
    """DT-1・DT-2：S1 は年度ごとの合計と件数、差額と増減率を登録する（origin='run'）。値は十進数の文字列。"""
    conn = ledger_of(repo, synthetic_run)
    lead = lead_of(conn, "s1_amount_change", "庁舎清掃業務")
    k_from, k_to = calc(conn, lead["calc_from"]), calc(conn, lead["calc_to"])
    assert (k_from["kind"], k_from["origin"], k_from["value"], k_from["unit"]) == (
        "base",
        "run",
        "1200000",
        "円",
    )
    assert (k_to["value"], json.loads(k_to["row_refs_json"]), k_to["row_count"]) == (
        "1500000",
        ["S005:000001"],
        1,
    )
    assert json.loads(k_from["row_refs_json"]) == ["S001:000001"]
    assert json.loads(lead["calc_ids_json"]) == [lead["calc_from"], lead["calc_to"]]
    for k in (k_from, k_to):
        request = json.loads(k["request_json"])
        assert (
            request["op"],
            request["column"],
            request["excluded"],
            request["excluded_count"],
        ) == (
            "sum",
            "amount_yen",
            [],
            0,
        )
        assert k["lead_id"] == lead["lead_id"]
        assert "SUM(" not in k["sql_text"].upper()
    by_kind = defaultdict(list)
    for k in lead_calcs(conn, lead["lead_id"]):
        by_kind[(k["kind"], json.loads(k["request_json"])["op"])].append(k)
    assert sorted(k["value"] for k in by_kind[("base", "count")]) == ["1", "1"]
    (diff,) = by_kind[("derived", "diff")]
    (rate,) = by_kind[("derived", "rate")]
    assert (diff["value"], diff["unit"], diff["origin"]) == ("300000", "円", "run")
    assert diff["formula"] == f"diff({lead['calc_to']}, {lead['calc_from']})"
    assert (rate["value"], rate["unit"]) == ("0.25", "比率")
    assert rate["formula"] == f"rate({lead['calc_to']}, {lead['calc_from']})"
    assert len(lead_calcs(conn, lead["lead_id"])) == 6

    expected = {  # (前年度, 翌年度, 差額, 増減率)。増減率は精度28桁（calc.py）で手計算した
        "システム保守業務": ("5000000", "5100000", "100000", "0.02"),
        "統計データ作成業務": ("4000000", "4200000", "200000", "0.05"),
        "翻訳業務": ("650000", "750000", "100000", "0.1538461538461538461538461538"),
        "会議録作成業務": ("350000", "380000", "30000", "0.08571428571428571428571428571"),
    }
    for key, (v_from, v_to, v_diff, v_rate) in expected.items():
        lead = lead_of(conn, "s1_amount_change", key)
        assert (calc(conn, lead["calc_from"])["value"], calc(conn, lead["calc_to"])["value"]) == (
            v_from,
            v_to,
        )
        derived = {
            json.loads(k["request_json"])["op"]: k["value"]
            for k in lead_calcs(conn, lead["lead_id"])
            if k["kind"] == "derived"
        }
        assert derived == {"diff": v_diff, "rate": v_rate}, key
    conn.close()


def test_dt1_excluded_rows_are_not_summed_and_recorded(repo: Path, synthetic_run: str) -> None:
    """DT-1：除いた行は基礎集計の row_refs に入らず、request_json に行・理由・件数が残る。
    amount_incomparable は件数だけを登録し、差額と増減率を作らない。"""
    conn = ledger_of(repo, synthetic_run)
    lead = lead_of(conn, "s1_amount_change", "荷物等の配送業務")
    calcs = lead_calcs(conn, lead["lead_id"])
    assert [(k["kind"], json.loads(k["request_json"])["op"]) for k in calcs] == [
        ("base", "count"),
        ("base", "count"),
    ]
    k_from, k_to = calc(conn, lead["calc_from"]), calc(conn, lead["calc_to"])
    assert (k_from["value"], json.loads(k_from["row_refs_json"]), k_from["unit"]) == ("0", [], "件")
    req = json.loads(k_from["request_json"])
    assert req["excluded"] == [{"ref": "S001:000002", "reason": "unit_price"}]
    assert (req["excluded_count"], req["excluded_by_reason"]) == (1, {"unit_price": 1})
    assert (k_to["value"], json.loads(k_to["row_refs_json"])) == ("1", ["S005:000002"])
    assert json.loads(k_to["request_json"])["excluded"] == []

    lead = lead_of(conn, "s1_amount_change", "物品の購入")
    k_from, k_to = calc(conn, lead["calc_from"]), calc(conn, lead["calc_to"])
    assert json.loads(k_from["request_json"])["excluded"] == [
        {"ref": "S003:000003", "reason": "amount_not_numeric"}
    ]
    assert (k_from["value"], k_to["value"]) == ("0", "2")
    assert json.loads(k_to["row_refs_json"]) == ["S005:000005", "S005:000006"]
    assert not [k for k in lead_calcs(conn, lead["lead_id"]) if k["kind"] == "derived"]

    lead = lead_of(conn, "s1_amount_change", "広報誌印刷業務")
    k_from = calc(conn, lead["calc_from"])
    assert json.loads(k_from["request_json"])["excluded"] == [
        {"ref": "S001:000006", "reason": "unit_price"}
    ]
    assert json.loads(k_from["row_refs_json"]) == []
    conn.close()


def test_dt1_s2_s3_count_aggregates(repo: Path, synthetic_run: str) -> None:
    """DT-1：S2・S3 は年度ごとの件数を calc_from・calc_to に入れる。派生計算は作らない。"""
    conn = ledger_of(repo, synthetic_run)
    for screen, key, refs_from, refs_to in (
        ("s2_method_change", "システム保守業務", ["S001:000003"], ["S007:000002"]),
        ("s2_method_change", "物品の購入", ["S003:000003"], ["S005:000005", "S005:000006"]),
        ("s3_reason_change", "調査研究委託", ["S003:000001"], ["S007:000001"]),
    ):
        lead = lead_of(conn, screen, key)
        k_from, k_to = calc(conn, lead["calc_from"]), calc(conn, lead["calc_to"])
        assert json.loads(k_from["request_json"])["op"] == "count"
        assert (json.loads(k_from["row_refs_json"]), k_from["value"]) == (
            refs_from,
            str(len(refs_from)),
        )
        assert (json.loads(k_to["row_refs_json"]), k_to["value"]) == (refs_to, str(len(refs_to)))
        assert [k["kind"] for k in lead_calcs(conn, lead["lead_id"])] == ["base", "base"]
    # 検出の計算の総数：amount_up 10組×6、amount_incomparable 3組×2、S2 2組×2、S3 1組×2
    n = conn.execute(
        "SELECT COUNT(*) FROM computations WHERE origin = 'run' AND kind IN ('base', 'derived')"
    ).fetchone()[0]
    assert n == 72
    ids = [r[0] for r in conn.execute("SELECT calc_id FROM computations ORDER BY calc_id")]
    assert ids == [f"K{n:04d}" for n in range(1, 81)]  # 照合 K0001〜K0008 の後に K0009〜K0080
    conn.close()


# ---------------------------------------------------------------------------
# DT-2：基準値0の増減率、Decimal、系列、年度途中（台帳を手で作る）
# ---------------------------------------------------------------------------

CRAFT_RUN = "20261106-000000-test"


def crafted_ledger(
    repo: Path, rows: list[dict], years: tuple[int, ...] = (2024, 2025)
) -> tuple[sqlite3.Connection, set[str]]:
    """取得済みの CSV（年度×方式）と、分野の表の行を手で入れた台帳。"""
    conn = ledger.create(repo, CRAFT_RUN)
    conn.row_factory = None
    sources: dict[tuple[int, str], str] = {}
    serial: dict[str, int] = defaultdict(int)
    with ledger.transaction(conn):
        for year in years:
            for family in ("kyoso", "zuikei"):
                sid = ledger.next_id(conn, "S")
                conn.execute(
                    "INSERT INTO sources (source_id, logical_key, kind, url, status, sha256)"
                    " VALUES (?, ?, 'dataset', 'https://www.cao.go.jp/test.csv', 'fetched', ?)",
                    (sid, f"cao-procurement|{year}|{family}|csv", "0" * 64),
                )
                sources[(year, family)] = sid
        for row in rows:
            sid = sources[(row["year"], row["family"])]
            serial[sid] += 1
            rid = ledger.record_id(sid, serial[sid])
            conn.execute(
                "INSERT INTO records (record_id, source_id, logical_no, cells_json, cells_sha256,"
                " status, parser_version) VALUES (?, ?, ?, '{}', ?, 'adopted', 'cao-procurement/1')",
                (rid, sid, serial[sid], "0" * 64),
            )
            conn.execute(
                "INSERT INTO procurement_contracts (record_id, record_status, fiscal_year, partial_year,"
                " method_family, no, title_raw, group_key, corp_number_state, unit_price, amount_yen,"
                " reason_category, reason_articles_json) VALUES (?, 'adopted', ?, ?, ?, ?, ?, ?,"
                " 'missing', ?, ?, ?, ?)",
                (
                    rid,
                    row["year"],
                    int(row["year"] == 2026),
                    row["family"],
                    serial[sid],
                    row["group"],
                    row["group"],
                    row.get("unit_price", 0),
                    row.get("amount"),
                    None if row["family"] == "kyoso" else "other",
                    None if row["family"] == "kyoso" else "[]",
                ),
            )
    return conn, set(sources.values())


def run_detect(repo: Path, conn: sqlite3.Connection, checked: set[str], **config) -> dict:
    adapter = load_adapter(repo, "cao-procurement")
    config = {"compare_years": [2024, 2025], "series_years": [2024, 2025], **config}
    with ledger.transaction(conn):
        outcome = detect.run_screens(conn, repo, adapter, config, checked_sources=checked)
    conn.row_factory = sqlite3.Row
    return outcome.counts


def test_dt2_zero_base_has_no_rate_and_sums_are_decimal(repo: Path) -> None:
    """DT-2：基準値が0なら増減率を作らない（差額は作る）。合計は Decimal で、2の53乗を超えても誤差がない。"""
    big = 9007199254740993  # 2**53 + 1。浮動小数点では表せない
    conn, checked = crafted_ledger(
        repo,
        [
            {"year": 2024, "family": "kyoso", "group": "テスト業務A", "amount": 0},
            {"year": 2025, "family": "kyoso", "group": "テスト業務A", "amount": 500},
            {"year": 2024, "family": "kyoso", "group": "テスト業務C", "amount": big},
            {"year": 2024, "family": "kyoso", "group": "テスト業務C", "amount": 1},
            {"year": 2025, "family": "kyoso", "group": "テスト業務C", "amount": big},
            {"year": 2025, "family": "kyoso", "group": "テスト業務C", "amount": 3},
        ],
    )
    counts = run_detect(repo, conn, checked)
    assert counts["s1_amount_change"] == {
        "state": "done",
        "compared": 2,
        "incomparable": 0,
        "leads": 2,
    }
    a = lead_of(conn, "s1_amount_change", "テスト業務A")
    assert a["change_category"] == "amount_up"
    assert (calc(conn, a["calc_from"])["value"], calc(conn, a["calc_to"])["value"]) == ("0", "500")
    derived = {
        json.loads(k["request_json"])["op"]: k["value"]
        for k in lead_calcs(conn, a["lead_id"])
        if k["kind"] == "derived"
    }
    assert derived == {"diff": "500"}

    c = lead_of(conn, "s1_amount_change", "テスト業務C")
    assert calc(conn, c["calc_from"])["value"] == "9007199254740994"
    assert calc(conn, c["calc_to"])["value"] == "9007199254740996"
    derived = {
        json.loads(k["request_json"])["op"]: k["value"]
        for k in lead_calcs(conn, c["lead_id"])
        if k["kind"] == "derived"
    }
    assert derived["diff"] == "2"
    conn.close()


def test_dt2_series_years_and_partial_year(repo: Path) -> None:
    """系列：series_years の各年度の合計を基礎集計にし、比べる2年度は calc_from・calc_to と同じ計算にする。
    年度途中（dataset.yaml の partial_year）の年度を比べると、比較できない理由に partial_year が付く。"""
    conn, checked = crafted_ledger(
        repo,
        [
            {"year": 2024, "family": "kyoso", "group": "テスト業務B", "amount": 100},
            {
                "year": 2024,
                "family": "kyoso",
                "group": "テスト業務B",
                "amount": 999,
                "unit_price": 1,
            },
            {"year": 2024, "family": "zuikei", "group": "テスト業務B", "amount": None},
            {"year": 2025, "family": "kyoso", "group": "テスト業務B", "amount": 200},
            {"year": 2026, "family": "kyoso", "group": "テスト業務B", "amount": 300},
        ],
        years=(2024, 2025, 2026),
    )
    run_detect(repo, conn, checked, compare_years=[2025, 2026], series_years=[2024, 2025, 2026])
    lead = lead_of(conn, "s1_amount_change", "テスト業務B")
    assert (lead["period_from"], lead["period_to"], lead["change_category"]) == (
        "2025",
        "2026",
        "amount_up",
    )
    assert json.loads(lead["incomparable_reasons_json"]) == ["partial_year"]
    series = json.loads(lead["calc_ids_json"])
    assert series[1:] == [lead["calc_from"], lead["calc_to"]]
    first = calc(conn, series[0])
    request = json.loads(first["request_json"])
    # 2024年度：単価契約の999円を除き、金額のない行は合計に入れず件数を残す
    assert (first["value"], first["kind"], first["origin"], first["unit"]) == (
        "100",
        "base",
        "run",
        "円",
    )
    assert request["null_rows"] == 1
    assert {"col": "unit_price", "op": "=", "value": 0} in request["filter"]
    assert {"col": "fiscal_year", "op": "=", "value": 2024} in request["filter"]
    assert "年度途中" not in first["meaning"]
    assert "年度途中" in calc(conn, lead["calc_to"])["meaning"]
    conn.close()


def test_dt2_series_year_without_sources_is_not_zero(repo: Path) -> None:
    """系列：資料のそろわない年度は計算を作らない（0にしない）。候補の note に年度と欠けた資料を書く。"""
    conn, checked = crafted_ledger(
        repo,
        [
            {"year": 2024, "family": "kyoso", "group": "テスト業務D", "amount": 100},
            {"year": 2025, "family": "kyoso", "group": "テスト業務D", "amount": 200},
        ],
    )
    run_detect(repo, conn, checked, series_years=[2023, 2024, 2025])
    lead = lead_of(conn, "s1_amount_change", "テスト業務D")
    assert json.loads(lead["calc_ids_json"]) == [lead["calc_from"], lead["calc_to"]]
    assert "2023" in lead["note"] and "kyoso:2023" in lead["note"] and "zuikei:2023" in lead["note"]
    conn.close()


# ---------------------------------------------------------------------------
# DT-3：requires の資料がそろわなければ実行しない
# ---------------------------------------------------------------------------


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """実行した検出SQL・母集団のSQLの、datasets/cao-procurement/ からのパス。"""
    seen: list[str] = []
    original = detect.execute_sql

    def spy(conn, path, params):
        seen.append(f"{path.parent.name}/{path.name}")
        return original(conn, path, params)

    monkeypatch.setattr(detect, "execute_sql", spy)
    return seen


def test_dt3_s2_not_run_without_both_families(repo: Path, executed: list[str]) -> None:
    """DT-3：令和7年度の競争入札がなければ、S1・S2 の検出SQLも母集団のSQLも実行せず、not_run と欠けた資料を
    書いて partial にする。S3（随意契約だけ）は実行する。"""
    result = start(repo, Site({URL["k25_csv"]: httpx.Response(404)}))
    assert result["state"] == "partial"
    assert executed == ["screens/s3_reason_change.sql", "populations/s3_reason_change.sql"]
    conn = ledger_of(repo, result["run_id"])
    counts = counts_of(conn)
    for name in ("s1_amount_change", "s2_method_change"):
        assert counts[name]["state"] == "not_run"
        assert counts[name]["missing"] == ["kyoso:2025"]
        assert "compared" not in counts[name] and "leads" not in counts[name]
    assert counts["s3_reason_change"] == {
        "state": "done",
        "compared": 5,
        "incomparable": 0,
        "leads": 1,
    }
    assert [r[0] for r in conn.execute("SELECT DISTINCT screen_id FROM leads")] == [
        "s3_reason_change"
    ]
    stop = json.loads(conn.execute("SELECT stop_reason FROM runs").fetchone()[0])
    assert [m["logical_key"] for m in stop["missing"]] == ["cao-procurement|2025|kyoso|csv"]
    assert stop["screens_not_run"] == [
        {"screen": "s1_amount_change", "missing": ["kyoso:2025"]},
        {"screen": "s2_method_change", "missing": ["kyoso:2025"]},
    ]
    assert result["detect"]["s1_amount_change"]["state"] == "not_run"
    conn.close()


def test_dt3_s3_not_run_without_zuikei_of_one_year(repo: Path, executed: list[str]) -> None:
    """DT-3：令和6年度の随意契約がなければ、3本とも実行しない（S3 は随意契約の片方の年度を欠く）。"""
    result = start(repo, Site({URL["z24_csv"]: httpx.Response(404)}))
    assert result["state"] == "partial"
    assert executed == []
    conn = ledger_of(repo, result["run_id"])
    counts = counts_of(conn)
    for name in SCREENS:
        assert counts[name]["state"] == "not_run"
        assert counts[name]["missing"] == ["zuikei:2024"]
        assert "compared" not in counts[name]
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    stages = json.loads(conn.execute("SELECT stages_json FROM runs").fetchone()[0])
    assert stages["detect"]["state"] == "done"
    conn.close()


# ---------------------------------------------------------------------------
# DT-4・SC-1：0件は「比べたN組の中で該当なし」の数として残す。絞り込みが検出と母集団の数に効く
# ---------------------------------------------------------------------------


def test_dt4_sc1_scope_zero_leads_keeps_compared(repo: Path, site: Site) -> None:
    """DT-4・SC-1：件名に「調査研究」を含まない行に絞ると、S3 は比べた4組の中で該当0件（done・compared 4・
    leads 0）。S1 は比べた組が1つ減る。比較の条件の母集団に、記者の指示と条件が書き足される。"""
    scope = {
        "instruction": "記者の指示：調査研究の委託を除く",
        "filters": [{"field": "title", "op": "not_contains", "value": "調査研究"}],
    }
    result = start(repo, site, scope=scope)
    assert result["state"] == "completed"
    conn = ledger_of(repo, result["run_id"])
    counts = counts_of(conn)
    assert counts["s3_reason_change"] == {
        "state": "done",
        "compared": 4,
        "incomparable": 0,
        "leads": 0,
    }
    assert counts["s1_amount_change"] == {
        "state": "done",
        "compared": 10,
        "incomparable": 6,
        "leads": 12,
    }
    assert counts["s2_method_change"] == {
        "state": "done",
        "compared": 16,
        "incomparable": 0,
        "leads": 2,
    }
    assert lead_of(conn, "s1_amount_change", "調査研究委託") is None
    defaults = load_adapter(repo, "cao-procurement").config["comparison_defaults"]
    lead = lead_of(conn, "s1_amount_change", "翻訳業務")
    comparison = json.loads(lead["comparison_json"])
    assert comparison["population"] == (
        defaults["s1_amount_change"]["population"]
        + "（記者の指示で絞り込み：記者の指示：調査研究の委託を除く。条件：title が「調査研究」を含まない）"
    )
    assert {k: v for k, v in comparison.items() if k != "population"} == {
        k: v for k, v in defaults["s1_amount_change"].items() if k != "population"
    }
    conn.close()


# ---------------------------------------------------------------------------
# DT-5：母集団の数と、screens/ だけが検出として実行されること
# ---------------------------------------------------------------------------


def test_dt5_population_counts_in_counts_json(repo: Path, synthetic_run: str) -> None:
    """DT-5：populations/*.sql の compared・incomparable が runs.counts_json に入る。"""
    conn = ledger_of(repo, synthetic_run)
    counts = counts_of(conn)
    assert counts["s1_amount_change"] == {
        "state": "done",
        "compared": 11,
        "incomparable": 6,
        "leads": 13,
    }
    assert counts["s2_method_change"] == {
        "state": "done",
        "compared": 17,
        "incomparable": 0,
        "leads": 2,
    }
    assert counts["s3_reason_change"] == {
        "state": "done",
        "compared": 5,
        "incomparable": 0,
        "leads": 1,
    }
    conn.close()


def test_dt5_only_sql_in_screens_runs_as_detection(
    repo: Path, site: Site, executed: list[str]
) -> None:
    """DT-5：populations/ に置いた SQL と、screens/ の .sql でないファイルは検出として実行されない。"""
    shutil.copy(screen_sql(repo, "s1_amount_change"), screen_sql(repo, "zz_extra", "populations"))
    (repo / "datasets" / "cao-procurement" / "screens" / "memo.txt").write_text(
        "SELECT 1;", encoding="utf-8"
    )
    result = start(repo, site)
    assert result["state"] == "completed"
    assert executed == [
        "screens/s1_amount_change.sql",
        "populations/s1_amount_change.sql",
        "screens/s2_method_change.sql",
        "populations/s2_method_change.sql",
        "screens/s3_reason_change.sql",
        "populations/s3_reason_change.sql",
    ]
    conn = ledger_of(repo, result["run_id"])
    assert "zz_extra" not in counts_of(conn)
    assert {r[0] for r in conn.execute("SELECT DISTINCT screen_id FROM leads")} == set(SCREENS)
    conn.close()


@pytest.mark.parametrize(
    "case",
    ["screen_without_yaml", "screen_without_population", "bad_columns", "excluded_not_in_refs"],
)
def test_detect_errors_fail_the_run_without_leads(repo: Path, site: Site, case: str) -> None:
    """検出SQLの誤り（dataset.yaml の screens にない、母集団のSQLがない、列が違う、excluded の行が row_refs
    にない）は E_INTERNAL で detect を failed にし、候補を1つも残さない（トランザクションを巻き戻す）。"""
    base = repo / "datasets" / "cao-procurement"
    if case == "screen_without_yaml":
        shutil.copy(screen_sql(repo, "s3_reason_change"), base / "screens" / "s9_extra.sql")
        shutil.copy(
            screen_sql(repo, "s3_reason_change", "populations"),
            base / "populations" / "s9_extra.sql",
        )
    elif case == "screen_without_population":
        screen_sql(repo, "s3_reason_change", "populations").unlink()
    elif case == "bad_columns":
        screen_sql(repo, "s3_reason_change").write_text(
            "SELECT 'x' AS lead_key, 'reason_changed' AS change_category WHERE :year_from < :year_to;",
            encoding="utf-8",
        )
    else:
        sql = screen_sql(repo, "s2_method_change")
        sql.write_text(
            sql.read_text(encoding="utf-8").replace(
                "'[]' AS excluded_from",
                '\'[{"ref": "S999:000001", "reason": "x"}]\' AS excluded_from',
            ),
            encoding="utf-8",
        )
    with pytest.raises(KizashiError) as info:
        start(repo, site)
    assert info.value.code == "E_INTERNAL"
    run_id = info.value.affected_ids[0]
    conn = ledger_of(repo, run_id)
    run = conn.execute("SELECT state, stages_json FROM runs").fetchone()
    assert run["state"] == "failed"
    assert json.loads(run["stages_json"])["detect"]["state"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM computations WHERE kind IN ('base', 'derived')"
        ).fetchone()[0]
        == 0
    )
    conn.close()
