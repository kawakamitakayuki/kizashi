"""正規化のテスト（test_plan.md の NR-1〜NR-9）。

期待値は、アダプタ仕様4章の規則と、合成データ（tests/fixtures/make_cao_fixtures.py の表）から手で
数えて書いたもの。実装の出力を期待値にしていない。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from conftest import (
    DATASET_DIR,
    DROPPED_WORDS,
    NAME_WORDS,
    all_bytes_under,
    all_text_in_ledger,
    fixture_bytes,
)
from kizashi import ledger, readers
from kizashi import normalize as nz
from kizashi.adapters import cao_procurement as cao
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

PATTERNS = yaml.safe_load((DATASET_DIR / "dataset.yaml").read_text(encoding="utf-8"))["group_key"][
    "remove_patterns"
]

KYOSO_KEYS = {
    "no",
    "title_raw",
    "contract_date_raw",
    "counterparty_raw",
    "corp_number_raw",
    "method_raw",
    "estimate_raw",
    "amount_raw",
    "rate_raw",
    "note_raw",
}
ZUIKEI_KEYS = (KYOSO_KEYS - {"method_raw"}) | {"reason_raw"}

# (資料ID, 論理資料のキー, ファイル)
SOURCES = [
    ("S001", "cao-procurement|2024|kyoso|csv", "kyoso_2024_mini.csv", 2024, "kyoso"),
    ("S002", "cao-procurement|2024|zuikei|csv", "zuikei_2024_mini.csv", 2024, "zuikei"),
    ("S003", "cao-procurement|2025|kyoso|csv", "kyoso_2025_mini.csv", 2025, "kyoso"),
    ("S004", "cao-procurement|2025|zuikei|csv", "zuikei_2025_mini.csv", 2025, "zuikei"),
]


@pytest.fixture
def normalized(repo: Path) -> sqlite3.Connection:
    """合成の4ファイルを正規化の経路で台帳に書いたもの。"""
    adapter = load_adapter(repo, "cao-procurement")
    conn = ledger.create(repo, "20261105-120000-test")
    with ledger.transaction(conn):
        for sid, key, name, year, family in SOURCES:
            conn.execute(
                "INSERT INTO sources (source_id, logical_key, kind, url, status) VALUES (?, ?, 'dataset', ?, 'fetched')",
                (sid, key, f"https://www.cao.go.jp/{name}"),
            )
            spec = adapter.file_spec(year, family)
            cao.normalize_source(
                conn, adapter, source_id=sid, logical_key=key, data=fixture_bytes(name), spec=spec
            )
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def contract(conn: sqlite3.Connection, rid: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM procurement_contracts WHERE record_id = ?", (rid,)
    ).fetchone()


def cells(conn: sqlite3.Connection, rid: str) -> dict:
    return json.loads(
        conn.execute("SELECT cells_json FROM records WHERE record_id = ?", (rid,)).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# NR-1：cp932 と BOM 付き UTF-8、セル内の改行、論理レコード番号の数え方
# ---------------------------------------------------------------------------


def test_nr1_cp932_logical_numbering() -> None:
    """NR-1：cp932 の合成データで、見出しの2行の後のレコードを1から数える。セル内の改行で番号がずれない。"""
    table = readers.read_table(fixture_bytes("kyoso_2024_mini.csv"), "cp932")
    assert [no for no, _ in table.records] == list(range(1, 13))
    assert [cells[0] for _, cells in table.records] == [str(n) for n in range(1, 13)]
    no3 = dict(table.records)[3]
    assert no3[1] == "システム保守業務\n（第３期）"
    assert no3[7] == "0000000000002\n0000000000003"
    # レコードの区切りは CRLF、セル内の改行は LF（実データと同じ）。物理的な行は論理レコードより多い
    data = fixture_bytes("kyoso_2024_mini.csv")
    assert data.count(b"\r\n") == 2 + 2 + 12
    assert data.count(b"\n") > 2 + 2 + 12


def test_nr1_bom_utf8_and_empty_record() -> None:
    """NR-1：BOM 付き UTF-8 を読める。空のレコードも1件として数える。"""
    text = '表題\r\n,作成日\r\nＮＯ,件名\r\n,\r\n1,"改行のある\r\n件名"\r\n\r\n3,件名C\r\n'
    table = readers.read_table(("﻿" + text).encode("utf-8"), "utf-8-sig")
    assert table.header_strings() == ["NO", "件名"]
    assert [(no, cells) for no, cells in table.records] == [
        (1, ("1", "改行のある\r\n件名")),
        (2, ()),
        (3, ("3", "件名C")),
    ]


def test_nr1_wrong_encoding_is_decode_error() -> None:
    """NR-1：決めた文字コードで読めなければ、置き換えずに止める（Q01）。"""
    with pytest.raises(readers.DecodeError):
        readers.read_table(fixture_bytes("kyoso_2024_mini.csv"), "utf-8")


def test_nr1_r8_files_are_bom_utf8_in_dataset_yaml(repo: Path) -> None:
    """NR-1：令和8年度（2026）のファイルは BOM 付き UTF-8 として読む設定（アダプタ仕様2章。D-026）。"""
    adapter = load_adapter(repo, "cao-procurement")
    for family in ("kyoso", "zuikei"):
        spec = adapter.file_spec(2026, family)
        assert spec.encoding == "utf-8-sig"
        assert spec.partial_year is True
    assert adapter.file_spec(2026, "kyoso").expected_columns == 17
    assert adapter.file_spec(2026, "zuikei").expected_columns == 28


# ---------------------------------------------------------------------------
# NR-2：和暦の日付
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R6.4.1", "2024-04-01"),
        ("令和6年4月1日", "2024-04-01"),
        ("令和6年5月10日", "2024-05-10"),
        ("Ｒ６．４．１", "2024-04-01"),
        (" R7.3.31 ", "2025-03-31"),
        ("R8.3.31", "2026-03-31"),
        ("R6.13.1", None),
        ("R6.2.30", None),
        ("令和6年度", None),
        ("2024/4/1", None),
        ("R6.4", None),
        ("", None),
        (None, None),
    ],
)
def test_nr2_wareki(raw: str | None, expected: str | None) -> None:
    """NR-2：`R6.4.1` と `令和6年4月1日` を ISO にする。読めない日付は None。"""
    assert nz.wareki_to_iso(raw) == expected


def test_nr2_bad_date_in_ledger(normalized: sqlite3.Connection) -> None:
    """NR-2：読めない日付（S001 の NO 9 の R6.13.1）は NULL で、比較できない理由 bad_date が付く。"""
    row = contract(normalized, "S001:000009")
    assert row["contract_date"] is None
    assert json.loads(row["incomparable_reasons_json"]) == ["bad_date"]
    assert contract(normalized, "S001:000003")["contract_date"] == "2024-05-10"
    assert contract(normalized, "S002:000003")["contract_date"] == "2024-06-01"


# ---------------------------------------------------------------------------
# NR-3：金額
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,200,000", (1200000, None)),
        ("350000", (350000, None)),
        ("１，２００", (1200, None)),
        (" 650,000 ", (650000, None)),
        ("1,20,000", (None, "non_numeric")),
        ("12,345ドル", (None, "foreign_currency")),
        ("USD 10,000", (None, "foreign_currency")),
        ("1,000ユーロ", (None, "foreign_currency")),
        ("単価契約\n１件当たり　250円", (None, "unit_price_text")),
        ("単価　1時間当たり　8,000円", (None, "unit_price_text")),
        ("9ヵ月＠100,000円×3台ほか", (None, "non_numeric")),
        ("", (None, "missing")),
        ("-", (None, "missing")),
        ("－", (None, "missing")),
        ("―", (None, "missing")),
    ],
)
def test_nr3_amount(raw: str, expected: tuple) -> None:
    """NR-3：整数、外貨、単価の文字列、その他の文字列、欠け（ゼロにしない）。"""
    assert nz.parse_amount(raw) == expected


def test_nr3_amount_in_ledger(normalized: sqlite3.Connection) -> None:
    """NR-3：元の値を amount_raw に残し、整数にできた値だけを amount_yen に入れる。"""
    row = contract(normalized, "S002:000003")
    assert (row["amount_raw"], row["amount_yen"], row["amount_note"]) == (
        "12,345ドル",
        None,
        "foreign_currency",
    )
    row = contract(normalized, "S001:000001")
    assert (row["amount_raw"], row["amount_yen"], row["amount_note"]) == (
        "1,200,000",
        1200000,
        None,
    )
    row = contract(normalized, "S002:000007")
    assert (row["amount_yen"], row["amount_note"]) == (None, "non_numeric")


# ---------------------------------------------------------------------------
# NR-4：法人番号の5つの状態
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0000000000001", ("digits13", ["0000000000001"])),
        ("0000000000001 ", ("digits13", ["0000000000001"])),
        ("1.00000E+12", ("exponent", [])),
        ("7.0111E+12", ("exponent", [])),
        ("-", ("missing", [])),
        ("－", ("missing", [])),
        ("", ("missing", [])),
        ("0000000000002\n0000000000003", ("multiple", ["0000000000002", "0000000000003"])),
        ("12345", ("invalid", [])),
        ("00000000000012", ("invalid", [])),
        ("法人番号なし", ("invalid", [])),
    ],
)
def test_nr4_corp_number(raw: str, expected: tuple) -> None:
    """NR-4：digits13・exponent（復元しない）・missing・multiple・invalid。"""
    assert nz.parse_corp_number(raw) == expected


def test_nr4_counts_in_ledger(normalized: sqlite3.Connection) -> None:
    """NR-4：S001（競争入札 令和6年度）の状態ごとの件数（手で数えた値）。"""
    counts = dict(
        normalized.execute(
            "SELECT corp_number_state, COUNT(*) FROM procurement_contracts WHERE record_id LIKE 'S001:%'"
            " GROUP BY corp_number_state"
        ).fetchall()
    )
    assert counts == {"digits13": 2, "exponent": 7, "missing": 1, "multiple": 1, "invalid": 1}
    row = contract(normalized, "S001:000003")
    assert json.loads(row["corp_numbers_json"]) == ["0000000000002", "0000000000003"]


# ---------------------------------------------------------------------------
# NR-5：担当官のセルの分割。氏名がどこにも保存されない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "units", "count"),
    [
        (
            "支出負担行為担当官\n内閣府大臣官房会計担当参事官\n架空　太郎",
            ["内閣府大臣官房会計担当参事官"],
            1,
        ),
        (
            (
                "支出負担行為担当官\n内閣府大臣官房会計担当参事官\n架空　太郎\n"
                "支出負担行為担当官代理\n食品安全委員会事務局総務課長\n見本　三郎"
            ),
            ["内閣府大臣官房会計担当参事官", "食品安全委員会事務局総務課長"],
            2,
        ),
        (
            "支出負担行為担当官　内閣府大臣官房会計担当参事官　架空　太郎",
            ["内閣府大臣官房会計担当参事官"],
            1,
        ),
        (
            "分任支出負担行為担当官\n内閣府政策統括官（防災担当）付参事官\n仮名　花子",
            ["内閣府政策統括官（防災担当）付参事官"],
            1,
        ),
        ("支出負担行為担当官\n食品安全委員会事務局長\n試験次郎", ["食品安全委員会事務局長"], 1),
        (
            "支出負担行為担当官\n内閣府 大臣官房会計担当参事官\n架空　太郎",
            ["内閣府 大臣官房会計担当参事官"],
            1,
        ),
        ("支出負担行為担当官\n架空", [None], 1),
        ("内閣府大臣官房会計担当参事官\n架空　太郎", [None], 0),
    ],
)
def test_nr5_split_officers(cell: str, units: list, count: int) -> None:
    """NR-5：役職ごとに分け、最後の2語（2語なら最後の1語）を氏名として捨てる。1語以下は分けられない。"""
    assert cao.split_officers(cell) == (units, count)


def test_nr5_officer_units_in_ledger(normalized: sqlite3.Connection) -> None:
    """NR-5：2人のセル（S001 の NO 4）は所属が2つ、担当官が2人。"""
    row = contract(normalized, "S001:000004")
    assert json.loads(row["officer_units_json"]) == [
        "内閣府大臣官房会計担当参事官",
        "食品安全委員会事務局総務課長",
    ]
    assert row["officer_count"] == 2
    assert json.loads(contract(normalized, "S001:000005")["officer_units_json"]) == [
        "内閣府大臣官房会計担当参事官"
    ]


def test_nr5_no_personal_names_anywhere(repo: Path, normalized: sqlite3.Connection) -> None:
    """NR-5：氏名の語が、records.cells_json・分野の表・台帳のファイルのどこにもない（全文検索）。"""
    text = all_text_in_ledger(normalized)
    for word in NAME_WORDS:
        assert word not in text, word
    normalized.close()
    raw = all_bytes_under(repo / "data" / "runs")
    for word in NAME_WORDS:
        assert word.encode("utf-8") not in raw, word


# ---------------------------------------------------------------------------
# NR-6：単価契約
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "amount", "expected"),
    [
        ("一般競争（単価）", "3,000,000", True),
        ("一般競争（総合評価・単価）", "1,800,000", True),
        ("一般競争", "単価契約\n１件当たり　250円", True),  # 令和5年度の形（金額の欄）
        (None, "単価　1時間当たり　8,000円", True),  # 随意契約の金額の欄
        ("一般競争", "1,200,000", False),
        ("一般競争（総合評価）", "5,000,000", False),
        (None, "4,000,000", False),
    ],
)
def test_nr6_unit_price(method: str | None, amount: str, expected: bool) -> None:
    """NR-6：方式の列（競争入札）または金額の列に `単価` を含めば単価契約。"""
    assert cao.is_unit_price(method, amount) is expected


def test_nr6_unit_price_counts(normalized: sqlite3.Connection) -> None:
    """NR-6：S001 は NO 2・5・6・8 の4件、S002 は NO 4 の1件（手で数えた値）。"""
    rows = normalized.execute(
        "SELECT record_id FROM procurement_contracts WHERE unit_price = 1 ORDER BY record_id"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "S001:000002",
        "S001:000005",
        "S001:000006",
        "S001:000008",
        "S002:000004",
        "S003:000011",
        "S003:000012",
        "S004:000004",
    ]
    detail = json.loads(contract(normalized, "S001:000006")["method_detail_json"])
    assert detail == {"comprehensive_evaluation": True, "unit_price": True}
    assert contract(normalized, "S002:000001")["method_detail_json"] is None


# ---------------------------------------------------------------------------
# NR-7：理由の分類と根拠条文
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "category", "articles"),
    [
        ("会計法第29条の3第4項\n公募", "open_call", ["会計法第29条の3第4項"]),
        ("会計法第29条の3第4項\n企画競争", "planning_competition", ["会計法第29条の3第4項"]),
        ("予算決算及び会計令第99条の2\n（不調）", "failed_tender", ["予決令第99条の2"]),
        (
            "会計法第29条の3第4項\n契約の性質又は目的が競争を許さない場合",
            "nature_no_competition",
            ["会計法第29条の3第4項"],
        ),
        (
            "予算決算及び会計令99条の5\n慈善のため設立した救済施設から物件を買い入れるとき",
            "charity",
            ["予決令第99条の5"],
        ),
        (
            "会計法第29条の3第5項\n予決令第99条第2号",
            "other",
            ["予決令第99条第2号", "会計法第29条の3第5項"],
        ),
        ("会計法令第99条の2\n企画競争", "planning_competition", ["会計法令第99条の2"]),
        ("会計法第２９条の３第４項（企画競争）", "planning_competition", ["会計法第29条の3第4項"]),
        ("予決令99条の2", "other", ["予決令第99条の2"]),
        ("企画競争（公募）", "planning_competition", []),
        ("不調のため公募", "failed_tender", []),
        ("", "other", []),
    ],
)
def test_nr7_reason(reason: str, category: str, articles: list[str]) -> None:
    """NR-7：語を上から順に見て分類する。条文は予決令にそろえ、「第」をそろえて並べ替える。"""
    assert cao.reason_category(reason) == category
    assert cao.reason_articles(reason) == articles


def test_nr7_reason_in_ledger(normalized: sqlite3.Connection) -> None:
    """NR-7：随意契約の行に分類と条文が入り、競争入札の行は NULL。"""
    row = contract(normalized, "S002:000006")
    assert row["reason_category"] == "other"
    assert json.loads(row["reason_articles_json"]) == ["予決令第99条第2号", "会計法第29条の3第5項"]
    row = contract(normalized, "S001:000001")
    assert (row["reason_raw"], row["reason_category"], row["reason_articles_json"]) == (
        None,
        None,
        None,
    )
    categories = [
        r[0]
        for r in normalized.execute(
            "SELECT reason_category FROM procurement_contracts WHERE record_id LIKE 'S002:%' ORDER BY no"
        )
    ]
    assert categories == [
        "open_call",
        "planning_competition",
        "failed_tender",
        "nature_no_competition",
        "charity",
        "other",
        "planning_competition",
        "open_call",
        "nature_no_competition",
    ]


# ---------------------------------------------------------------------------
# NR-8：件名のまとめのキーと、取り除いた部分の保存
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "key", "removed"),
    [
        ("令和６年度庁舎清掃業務", "庁舎清掃業務", ["令和6年度"]),
        ("令和7年度庁舎清掃業務", "庁舎清掃業務", ["令和7年度"]),
        ("広報誌印刷業務（令和６年度）", "広報誌印刷業務", ["(令和6年度)"]),
        ("広報誌印刷業務（令和７年度）", "広報誌印刷業務", ["(令和7年度)"]),
        ("システム保守業務\n（第３期）", "システム保守業務", ["第3期", "()"]),
        ("システム保守業務（第４期）", "システム保守業務", ["第4期", "()"]),
        ("4月分　複写機保守", "複写機保守", ["4月分"]),
        ("12か月間　車両運行管理業務", "車両運行管理業務", ["12か月間"]),
        ("車両運行管理業務（第２期）", "車両運行管理業務", ["第2期", "()"]),
        ("2024年度 調査", "調査", ["2024年度"]),
        ("R6年度業務", "業務", ["R6年度"]),
        ("令和元年版白書", "白書", ["令和元年版"]),
        ("平成30年度分析", "分析", ["平成30年度"]),
        ("研修会場借上げ", "研修会場借上げ", []),
        ("消耗品の購入（単価契約）", "消耗品の購入(単価契約)", []),
    ],
)
def test_nr8_group_key(title: str, key: str, removed: list[str]) -> None:
    """NR-8：NFKC・空白の除去の後、決まった形を上から順に取り除く（括弧の形を先に）。"""
    assert nz.group_key(title, PATTERNS) == (key, removed)


def test_nr8_removed_parts_in_ledger(normalized: sqlite3.Connection) -> None:
    """NR-8：取り除いた部分を title_removed_parts に残し、元の件名は title_raw にそのまま残す。"""
    row = contract(normalized, "S001:000003")
    assert row["group_key"] == "システム保守業務"
    assert json.loads(row["title_removed_parts"]) == ["第3期", "()"]
    assert row["title_raw"] == "システム保守業務\n（第３期）"
    assert contract(normalized, "S004:000002")["group_key"] == "システム保守業務"


# ---------------------------------------------------------------------------
# NR-9：取り込まない列と officer_raw が残らない。cells_json のキーが take のキー
# ---------------------------------------------------------------------------


def test_nr9_cells_json_keys_are_take_keys(normalized: sqlite3.Connection) -> None:
    """NR-9：records.cells_json のキーは take のキー（officer_raw を除く）。値は元のセルの文字列。"""
    for sid, family in (
        ("S001", "kyoso"),
        ("S002", "zuikei"),
        ("S003", "kyoso"),
        ("S004", "zuikei"),
    ):
        expected = KYOSO_KEYS if family == "kyoso" else ZUIKEI_KEYS
        for (cells_json,) in normalized.execute(
            "SELECT cells_json FROM records WHERE source_id = ?", (sid,)
        ):
            assert set(json.loads(cells_json)) == expected
    c = cells(normalized, "S001:000003")
    assert c["title_raw"] == "システム保守業務\n（第３期）"
    assert c["corp_number_raw"] == "0000000000002\n0000000000003"
    assert c["no"] == "3"
    assert cells(normalized, "S001:000010")["counterparty_raw"] == "株式会社テスト?警備"


def test_nr9_dropped_columns_not_stored(repo: Path, normalized: sqlite3.Connection) -> None:
    """NR-9：担当官の所在地、相手方の住所、随意契約の内部用の列の値が台帳にない。"""
    text = all_text_in_ledger(normalized)
    for word in DROPPED_WORDS:
        assert word not in text, word
    assert "officer_raw" not in text


def test_nr9_bad_header_writes_nothing(repo: Path) -> None:
    """NR-9・QC-1：見出しが期待と違うファイルは、1行も書かずに E_FORMAT_CHANGED（D-027）。"""
    adapter = load_adapter(repo, "cao-procurement")
    conn = ledger.create(repo, "20261105-120001-test")
    conn.execute(
        "INSERT INTO sources (source_id, logical_key, kind, url, status) VALUES ('S001', 'k', 'dataset', 'u', 'fetched')"
    )
    with pytest.raises(KizashiError) as info:
        cao.normalize_source(
            conn,
            adapter,
            source_id="S001",
            logical_key="cao-procurement|2024|kyoso|csv",
            data=fixture_bytes("kyoso_bad_header.csv"),
            spec=adapter.file_spec(2024, "kyoso"),
        )
    assert info.value.code == "E_FORMAT_CHANGED"
    assert "10列目（amount_raw）" in info.value.message
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM procurement_contracts").fetchone()[0] == 0
