"""PRTR の骨組み（test_plan.md の PR-1。P1-c。docs/adapter_prtr.md）。

datasets/prtr/ の dataset.yaml、型付きの表（tables.sql）、検出SQL 1本、合成のテスト用データを確かめる。
骨組みには正規化の処理がないので、テストの中で dataset.yaml の取り込む列だけを読み、共通の表と PRTR の表に入れる。
期待値は、合成データ（tests/fixtures/make_prtr_fixtures.py）の設計から手で決めた値。
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from conftest import DROPPED_WORDS, NAME_WORDS, REPO
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

PRTR = REPO / "datasets" / "prtr"
SCRIPT = REPO / "tests" / "fixtures" / "make_prtr_fixtures.py"
SCHEMA = REPO / "src" / "kizashi" / "schema.sql"
S1 = PRTR / "screens" / "s1_air_release_change.sql"
# 資料ID → (ファイルの種類, 排出年度)
SOURCES = {
    "S001": ("honshi", 2023),
    "S002": ("besshi", 2023),
    "S003": ("honshi", 2024),
    "S004": ("besshi", 2024),
}
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
# 取り込まない列にだけある架空の語（事業所の名称と、届出者の住所）
NAME_ONLY_WORDS = ("株式会社テスト化学", "第一工場", "第二工場", "見本三郎商店")


def config() -> dict:
    return yaml.safe_load((PRTR / "dataset.yaml").read_text(encoding="utf-8"))


def generated() -> dict[str, bytes]:
    spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.outputs()


def read_rows(kind: str, year: int) -> list[list[str]]:
    data = (PRTR / "fixtures" / f"{kind}_{year}_mini.txt").read_bytes()
    return list(csv.reader(io.StringIO(data.decode("cp932"), newline="")))


# ---------------------------------------------------------------------------
# dataset.yaml と合成のテスト用データ
# ---------------------------------------------------------------------------


def test_pr1_dataset_yaml_columns_and_personal_columns() -> None:
    """PR-1：取り込む列が列の数の中にあり、本紙の5〜10列（氏名・住所）と名称の列を取り込まない。"""
    cfg = config()
    assert cfg["domain"] == "prtr" and cfg["status"] == "skeleton"
    for kind, columns in (("honshi", 23), ("besshi", 41)):
        cs = cfg["column_sets"][kind]
        assert cs["columns"] == columns
        indexes = [v["index"] for v in cs["take"].values()]
        assert len(indexes) == len(set(indexes))
        assert all(1 <= i <= columns for i in indexes)
    honshi = cfg["column_sets"]["honshi"]
    taken = {v["index"] for v in honshi["take"].values()}
    assert honshi["personal"] == [5, 6, 7, 8, 9, 10]
    assert not taken & set(honshi["personal"])
    assert not taken & set(honshi["names"])
    assert cfg["units"] == {"1": "kg", "2": "mg-TEQ"}
    assert cfg["first_scope"] == {"pref": None, "substance_code": None, "medium": "air"}
    assert sorted(p.stem for p in (PRTR / "screens").glob("*.sql")) == sorted(cfg["screens"])
    assert set(cfg["comparison_defaults"]["s1_air_release_change"]) == {
        "population",
        "row_meaning",
        "period",
        "unit",
        "entity_mapping",
        "formula",
    }


@pytest.mark.parametrize("name", sorted(generated()))
def test_pr1_fixtures_match_generator(name: str) -> None:
    """PR-1：合成のテスト用データが生成スクリプトの出力と同じ。"""
    assert (PRTR / "fixtures" / name).read_bytes() == generated()[name]


@pytest.mark.parametrize("year", [2023, 2024])
def test_pr1_fixtures_shape(year: int) -> None:
    """PR-1：cp932・CR＋LF・見出しなし・本紙23列・別紙41列。届出物質数が別紙の行の数と合う（PQ05）。"""
    raw = (PRTR / "fixtures" / f"honshi_{year}_mini.txt").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") == 8
    honshi, besshi = read_rows("honshi", year), read_rows("besshi", year)
    assert {len(r) for r in honshi} == {23} and {len(r) for r in besshi} == {41}
    assert {r[1] for r in honshi} == {str(year)}  # 排出年度（PQ02）
    per = {r[0]: 0 for r in honshi}
    for r in besshi:
        per[r[0]] += 1
    assert all(int(r[22]) == per[r[0]] for r in honshi)
    assert len(besshi) == 9


# ---------------------------------------------------------------------------
# 型付きの表と検出SQL
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger() -> sqlite3.Connection:
    """共通の表と PRTR の表に、dataset.yaml の取り込む列だけを入れた台帳（メモリー上）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.executescript((PRTR / "tables.sql").read_text(encoding="utf-8"))
    cfg = config()
    units = cfg["units"]
    facility_of: dict[tuple[int, str], str] = {}
    for sid, (kind, year) in SOURCES.items():
        conn.execute(
            "INSERT INTO sources (source_id, logical_key, kind, url, status) VALUES (?, ?, ?, ?, ?)",
            (sid, f"prtr|{year}|{kind}|txt", "dataset", f"https://www.nite.go.jp/{sid}", "fetched"),
        )
        take = cfg["column_sets"][kind]["take"]
        for n, row in enumerate(read_rows(kind, year), start=1):
            rid = f"{sid}:{n:06d}"
            cells = {key: row[spec["index"] - 1] for key, spec in take.items()}
            conn.execute(
                "INSERT INTO records (record_id, source_id, logical_no, cells_json, cells_sha256,"
                " status, parser_version) VALUES (?, ?, ?, ?, '', 'adopted', 'prtr/0')",
                (rid, sid, n, json.dumps(cells, ensure_ascii=False)),
            )
            if kind == "honshi":
                facility_of[(year, cells["seiri_no"])] = rid
                conn.execute(
                    "INSERT INTO prtr_facilities VALUES (?, 'adopted', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rid,
                        int(cells["fiscal_year_raw"]),
                        cells["seiri_no"],
                        cells["corp_number_raw"] or None,
                        cells["pref"],
                        cells["city"],
                        cells["industry_code"],
                        cells["industry_name"],
                        int(cells["substance_count"]),
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO prtr_releases (record_id, record_status, fiscal_year, seiri_no,"
                    " facility_record_id, sheet_no, substance_code, substance_name, unit, air, water,"
                    " soil, landfill, sewer, offsite) VALUES (?, 'adopted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?)",
                    (
                        rid,
                        year,
                        cells["seiri_no"],
                        facility_of[(year, cells["seiri_no"])],
                        int(cells["sheet_no"]),
                        cells["substance_code"],
                        cells["substance_name"],
                        units[cells["unit_raw"]],
                        *(
                            cells[k]
                            for k in ("air", "water", "soil", "landfill", "sewer", "offsite")
                        ),
                    ),
                )
    return conn


def screen(conn: sqlite3.Connection, **params: object) -> list[dict]:
    cur = conn.execute(S1.read_text(encoding="utf-8"), params)
    assert tuple(d[0] for d in cur.description) == COLUMNS
    rows = [dict(zip(COLUMNS, r, strict=True)) for r in cur.fetchall()]
    for r in rows:
        for key in COLUMNS[5:]:
            r[key] = json.loads(r[key])
        r["incomparable_reasons"] = [x for x in r["incomparable_reasons"] if x is not None]
    return sorted(rows, key=lambda r: r["lead_key"])


def test_pr1_personal_and_name_columns_not_stored(ledger: sqlite3.Connection) -> None:
    """PR-1：架空の氏名（代表者・個人の届出者）、届出者の住所、事業所の名称が台帳のどこにも残らない。"""
    text = "".join(
        str(v)
        for table in ("records", "prtr_facilities", "prtr_releases")
        for row in ledger.execute(f"SELECT * FROM {table}")
        for v in row
    )
    for word in (*NAME_WORDS, *DROPPED_WORDS, *NAME_ONLY_WORDS):
        assert word not in text, word


def test_pr1_s1_candidates(ledger: sqlite3.Connection) -> None:
    """PR-1：手で決めた3件だけを候補にする（増加、mg-TEQ の減少、令和6年度の大気が空欄）。"""
    got = screen(ledger, year_from=2023, year_to=2024, pref="東京都", substance_code="999")
    assert got == [
        {
            "lead_key": "0000000000001|東京都|テスト市|2000|999|kg",
            "change_category": "air_release_up",  # 10.5 → 12.0
            "metric": "air_sum",
            "period_from": 2023,
            "period_to": 2024,
            "row_refs_from": ["S002:000001"],
            "row_refs_to": ["S004:000001"],
            "excluded_from": [],
            "excluded_to": [],
            "incomparable_reasons": [],
        },
        {
            "lead_key": "0000000000007|東京都|テスト市|2100|999|mg-TEQ",
            "change_category": "air_release_down",  # 0.5 → 0.25
            "metric": "air_sum",
            "period_from": 2023,
            "period_to": 2024,
            "row_refs_from": ["S002:000008"],
            "row_refs_to": ["S004:000008"],
            "excluded_from": [],
            "excluded_to": [],
            "incomparable_reasons": [],
        },
        {
            "lead_key": "0000000000008|東京都|テスト市|2200|999|kg",
            "change_category": "air_release_incomparable",  # 6.0 → 空欄
            "metric": "air_sum",
            "period_from": 2023,
            "period_to": 2024,
            "row_refs_from": ["S002:000009"],
            "row_refs_to": ["S004:000009"],
            "excluded_from": [],
            "excluded_to": [{"ref": "S004:000009", "reason": "air_missing"}],
            "incomparable_reasons": ["air_missing_one_side"],
        },
    ]
    # 候補にしないもの：法人番号 …2 の2事業所、法人番号が空欄、大阪府、物質甲が令和5年度だけの …6
    keys = {r["lead_key"].split("|")[0] for r in got}
    assert keys.isdisjoint({"0000000000002", "0000000000005", "0000000000006", ""})


def test_pr1_s1_unchanged_substance_is_not_a_candidate(ledger: sqlite3.Connection) -> None:
    """PR-1：値の変わらない物質乙（3.0 → 3.0）と、片方の年度にだけある物質乙は候補にしない。"""
    assert screen(ledger, year_from=2023, year_to=2024, pref="東京都", substance_code="998") == []


def test_pr1_s1_other_prefecture(ledger: sqlite3.Connection) -> None:
    """PR-1：大阪府の事業所（4.0 → 9.0）は、東京都の範囲では出ず、大阪府の範囲で出る。"""
    got = screen(ledger, year_from=2023, year_to=2024, pref="大阪府", substance_code="999")
    assert [(r["lead_key"], r["change_category"]) for r in got] == [
        ("0000000000005|大阪府|テスト市|2000|999|kg", "air_release_up")
    ]


def test_pr1_s1_substance_list_revision(ledger: sqlite3.Connection) -> None:
    """PR-1：排出年度2022と2023を比べると、対象物質の見直しをまたぐので substance_list_revision が付く。"""
    for table in ("prtr_facilities", "prtr_releases"):
        ledger.execute(f"UPDATE {table} SET fiscal_year = fiscal_year - 1")
    got = screen(ledger, year_from=2022, year_to=2023, pref="東京都", substance_code="999")
    assert [r["lead_key"].split("|")[0] for r in got] == [
        "0000000000001",
        "0000000000007",
        "0000000000008",
    ]
    assert all("substance_list_revision" in r["incomparable_reasons"] for r in got)


def test_pr1_cli_does_not_run_prtr(tmp_path: Path) -> None:
    """PR-1：骨組みは CLI の分野に登録していないので、kizashi run prtr は E_USAGE。"""
    with pytest.raises(KizashiError) as exc:
        load_adapter(REPO, "prtr")
    assert exc.value.code == "E_USAGE"
