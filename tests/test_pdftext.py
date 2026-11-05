"""原本照合（PDF）のテスト（test_plan.md の PD-1・PD-2。アダプタ仕様7.1〜7.4。D-002）。

PDF のライブラリは動かさない。合成の罫線と文字の座標（tests/fixtures/pdf_cells/traps_*.json。
生成は tests/fixtures/make_pdf_cells.py）を pdftext に渡す。期待値は、その表から手で決めた値。
"""

from __future__ import annotations

import importlib.util
import re
import unicodedata
from pathlib import Path

import pytest

from conftest import REPO, pdf_pages
from kizashi import pdftext, reconcile
from kizashi.adapters import load_adapter

ADAPTER = load_adapter(REPO, "cao-procurement")
PDF_FIELDS = ADAPTER.config["reconcile"]["pdf_fields"]
ZSPEC = pdftext.table_spec(ADAPTER.file_spec(2024, "zuikei"), PDF_FIELDS)
KSPEC = pdftext.table_spec(ADAPTER.file_spec(2024, "kyoso"), PDF_FIELDS)


def _generator():
    path = Path(__file__).resolve().parent / "fixtures" / "make_pdf_cells.py"
    spec = importlib.util.spec_from_file_location("make_pdf_cells", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


LONG_REASON = _generator().LONG_REASON


def squash(text: str) -> str:
    """NFKC にし、空白と改行を除く（テストの中で独立に書く）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


@pytest.fixture(scope="module")
def ztable() -> pdftext.PdfTable:
    return pdftext.extract_cells(pdf_pages("traps_zuikei"), ZSPEC)


@pytest.fixture(scope="module")
def ktable() -> pdftext.PdfTable:
    return pdftext.extract_cells(pdf_pages("traps_kyoso"), KSPEC)


def row_of(table: pdftext.PdfTable, no: str) -> pdftext.PdfRow:
    rows = [r for r in table.rows if r.no == no]
    assert len(rows) == 1, no
    return rows[0]


# ---------------------------------------------------------------------------
# PD-1：セルの切り出しと行の対応（7.1・7.2）
# ---------------------------------------------------------------------------


def test_pd1_table_spec_from_dataset_yaml() -> None:
    """PD-1：照合に使う列は take の index（件名1、日付4、相手方5、法人番号7、方式・理由8、金額10）。"""
    assert ZSPEC.columns == 17 and KSPEC.columns == 16
    assert ZSPEC.no_column == 0 and ZSPEC.date_column == 4
    assert ZSPEC.field_columns == {
        "amount_yen": 10,
        "contract_date": 4,
        "title_raw": 1,
        "counterparty_raw": 5,
        "corp_numbers": 7,
        "reason_raw": 8,
    }
    assert KSPEC.field_columns["method_raw"] == 8 and "reason_raw" not in KSPEC.field_columns
    assert set(ZSPEC.keep) == {1, 4, 5, 7, 8, 10}


def test_pd1_pages_rows_and_skipped_page(ztable: pdftext.PdfTable) -> None:
    """PD-1：列の数が違うページ（4ページ目の15列）は使わない。見出しの2行目（NO が空）は分かれた行にしない。
    3ページ目は見出しの直後に前のページから続く行があるので、分かれた行のあるページ。"""
    assert ztable.page_count == 5
    assert ztable.skipped_pages == [{"page": 4, "columns": 15}]
    assert ztable.split_pages == [3]
    assert [(r.page, r.no) for r in ztable.rows] == [
        (1, "1"),
        (1, "2"),
        (1, "3"),
        (1, "4"),
        (1, "5"),
        (2, "6"),
        (2, "7"),
        (3, "8"),
        (5, "10"),
        (5, "10"),
        (5, "11"),
        (5, "11"),
    ]
    # 分かれた行の影響：3ページ目のすべての件と、前のページ（2ページ目）の最後の件
    assert [(r.page, r.no) for r in ztable.rows if r.split_affected] == [(2, "7"), (3, "8")]


def test_pd1_only_no_and_reconcile_columns_are_kept(ztable: pdftext.PdfTable) -> None:
    """PD-1：NO の列と照合に使う列以外の文字は捨てる。担当官の列に縦に並んだ架空の氏名も残らない。"""
    for row in ztable.rows:
        assert set(row.cells) <= {0, 1, 4, 5, 7, 8, 10}, row.no
    text = "\n".join(c for r in ztable.rows for c in r.cells.values())
    for word in ("架", "太", "郎", "仮", "花", "子", "参事官", "官庁町", "模擬市", "備考の記載"):
        assert word not in text, word
    assert "10,000,000" not in text  # 予定価格の列（9列目）
    assert "1,000,000" not in text.replace("11,000,000", "")


def test_pd1_inner_cell_line_does_not_split_row(ztable: pdftext.PdfTable) -> None:
    """PD-1：備考の列の中だけにある横線で行を分けない。複数行のセルは連結する。"""
    row = row_of(ztable, "1")
    assert squash(row.cells[1]) == "調査研究委託(第2期)"
    assert squash(row.cells[8]) == squash(LONG_REASON)
    assert row.cells[4] == "R6.4.1"
    assert row.cells[10] == "9,800,000"


def test_pd1_kangxi_radical_stays_on_its_line(ztable: pdftext.PdfTable) -> None:
    """PD-1：上端が1.51pt ずれた康熙部首の「⼊」も同じ行にまとまり、並びが変わらない。NFKC で「入」。"""
    row = row_of(ztable, "2")
    assert row.cells[1] == "物品の購⼊"
    assert squash(row.cells[1]) == "物品の購入"


def test_pd1_cell_text_rules() -> None:
    """PD-1：文字の上下の中心で行をまとめ、x の順に並べる。文字の大きさの0.8倍を超える間と、行の区切りに空白。"""
    c = pdftext.Char
    chars = [
        c("D", 0.0, 112.0, 5.0, 122.0, 10.0),  # 2行目
        c("B", 5.0, 100.0, 10.0, 110.0, 10.0),
        c("A", 0.0, 100.0, 5.0, 110.0, 10.0),
        c("C", 18.1, 101.6, 23.1, 111.6, 10.0),  # 上端が1.6pt 下、前の文字との間が8.1pt
    ]
    assert pdftext.cell_text(chars) == "AB C D"
    assert pdftext.cell_text([]) == ""


@pytest.mark.parametrize(
    ("no", "date", "status", "page", "reason"),
    [
        ("1", "R6.4.1", "matched", 1, None),
        ("５", "R6.6.3", "matched", 1, None),  # CSV の NO は NFKC・空白除去で比べる
        ("6", "R6.8.1", "matched", 2, None),
        ("7", "R6.9.1", "ambiguous", None, "split_row"),  # 次のページの最初の行が続き
        ("8", "R6.10.1", "ambiguous", None, "split_row"),  # 分かれた行のあるページの件
        ("9", "R6.12.1", "not_found", None, None),  # 列の数が違うページの件
        ("10", "R6.10.2", "matched", 5, None),  # 同じ NO が2行。契約日で絞る
        ("11", "R6.11.1", "ambiguous", None, None),  # 契約日でも絞れない
        ("12", "R7.1.1", "not_found", None, None),  # PDF にない
    ],
)
def test_pd1_locate_row(
    ztable: pdftext.PdfTable, no: str, date: str, status: str, page: int | None, reason: str | None
) -> None:
    """PD-1：NO のセルで行を対応させる。2つ以上なら契約日で絞る。金額は行を探すのに使わない。"""
    found = pdftext.locate_row(ztable, no, date)
    assert (found.status, found.reason) == (status, reason)
    if status == "matched":
        assert found.row is not None and found.row.page == page
    else:
        assert found.row is None


def test_pd1_locate_same_no_picks_row_by_date(ztable: pdftext.PdfTable) -> None:
    """PD-1：NO 10 の2行のうち、契約日が R6.10.2 の行（金額 310,000）を選ぶ。候補のページも残す。"""
    found = pdftext.locate_row(ztable, "10", "R6.10.2")
    assert found.row is not None and found.row.cells[10] == "310,000"
    ambiguous = pdftext.locate_row(ztable, "11", "R6.11.1")
    assert ambiguous.pages == [5]


# ---------------------------------------------------------------------------
# PD-2：項目の照合（7.3）
# ---------------------------------------------------------------------------


def values(**overrides: object) -> dict:
    base: dict = {
        "title_raw": "",
        "counterparty_raw": "",
        "method_raw": None,
        "reason_raw": None,
        "contract_date_raw": "",
        "amount_raw": "",
        "amount_yen": None,
        "amount_note": "missing",
        "corp_number_state": "missing",
        "corp_numbers": [],
    }
    base.update(overrides)
    return base


def amount(raw: str, yen: int) -> dict:
    return values(amount_raw=raw, amount_yen=yen, amount_note=None)


def check(table: pdftext.PdfTable, no: str, field: str, csv_values: dict) -> str:
    row = row_of(table, no)
    column = (ZSPEC if table.columns == 17 else KSPEC).field_columns[field]
    return pdftext.match_field(field, csv_values, row.cells.get(column, ""))


@pytest.mark.parametrize(
    ("no", "csv_values", "expected"),
    [
        ("1", amount("9,800,000", 9800000), "match"),
        ("1", amount("19,800,000", 19800000), "mismatch"),  # 先頭に1桁足した変異
        ("1", amount("9,800,001", 9800001), "mismatch"),  # 末尾の1桁を変えた変異
        ("2", amount("1,000,000", 1000000), "mismatch"),  # PDF は 11,000,000（部分一致にしない）
        ("2", amount("11,000,000", 11000000), "match"),
        ("6", amount("500,000", 500000), "unverifiable"),  # PDF のセルに数値がない
        # 数値でない金額は、セルとの完全一致
        (
            "3",
            values(amount_raw="単価　1時間当たり　8,000円", amount_note="unit_price_text"),
            "match",
        ),
        (
            "3",
            values(amount_raw="単価　1時間当たり　9,000円", amount_note="unit_price_text"),
            "mismatch",
        ),
        ("4", values(amount_raw="12,345ドル", amount_note="foreign_currency"), "match"),
        ("4", values(amount_raw="12,346ドル", amount_note="foreign_currency"), "mismatch"),
        ("4", values(amount_raw="-", amount_note="missing"), "unverifiable"),
    ],
)
def test_pd2_amount(ztable: pdftext.PdfTable, no: str, csv_values: dict, expected: str) -> None:
    """PD-2：金額は数値の並びの等しさ（部分一致にしない）。予定価格の列の数値を使わない。"""
    assert check(ztable, no, "amount_yen", csv_values) == expected


def test_pd2_estimate_column_is_not_used_for_amount(ktable: pdftext.PdfTable) -> None:
    """PD-2：競争入札の NO 1 は予定価格 1,100,000・契約金額 1,000,000。予定価格の値は一致にならない。"""
    assert check(ktable, "1", "amount_yen", amount("1,000,000", 1000000)) == "match"
    assert check(ktable, "1", "amount_yen", amount("1,100,000", 1100000)) == "mismatch"
    assert check(ktable, "1", "amount_yen", amount("11,000,000", 11000000)) == "mismatch"


@pytest.mark.parametrize(
    ("field", "csv_values", "expected"),
    [
        ("title_raw", values(title_raw="調査研究委託\n（第２期）"), "match"),  # 複数行のセル
        ("title_raw", values(title_raw="調査研究委託（第３期）"), "mismatch"),
        ("title_raw", values(title_raw=""), "unverifiable"),
        ("reason_raw", values(reason_raw=LONG_REASON), "match"),
        ("reason_raw", values(reason_raw=LONG_REASON.replace("短期間", "長期間")), "mismatch"),
        ("counterparty_raw", values(counterparty_raw="株式会社テスト総研"), "match"),
        ("counterparty_raw", values(counterparty_raw="株式会社テスト総合研究所"), "mismatch"),
        ("contract_date", values(contract_date_raw="R6.4.1"), "match"),
        ("contract_date", values(contract_date_raw="R6.4.2"), "mismatch"),
        ("contract_date", values(contract_date_raw="令和6年4月1日"), "mismatch"),
        (
            "corp_numbers",
            values(corp_number_state="digits13", corp_numbers=["0000000000005"]),
            "match",
        ),
        (
            "corp_numbers",
            values(corp_number_state="digits13", corp_numbers=["0000000000006"]),
            "mismatch",
        ),
        ("corp_numbers", values(corp_number_state="exponent"), "unverifiable"),
        (
            "corp_numbers",
            values(corp_number_state="multiple", corp_numbers=["0000000000005", "0000000000006"]),
            "unverifiable",
        ),
    ],
)
def test_pd2_text_date_and_corp_number(
    ztable: pdftext.PdfTable, field: str, csv_values: dict, expected: str
) -> None:
    """PD-2：件名・相手方・理由は完全一致（150字以上の理由の1字の変異も mismatch）。契約日は境界付きの一致。
    法人番号は CSV が13桁のときだけ数値の並びの等しさで照らす。"""
    assert len(squash(LONG_REASON)) >= 150
    assert check(ztable, "1", field, csv_values) == expected


def test_pd2_question_mark_in_csv_is_mismatch(ztable: pdftext.PdfTable) -> None:
    """PD-2：CSV の `?` と PDF の文字（−）の差は mismatch（実際の差として記録する）。"""
    assert (
        check(ztable, "5", "counterparty_raw", values(counterparty_raw="（公財）テスト?研究所"))
        == "mismatch"
    )
    assert (
        check(ztable, "5", "counterparty_raw", values(counterparty_raw="（公財）テスト−研究所"))
        == "match"
    )


def test_pd2_kyoso_method_and_date_boundary(ktable: pdftext.PdfTable) -> None:
    """PD-2：方式の単価の有無の変異は mismatch。契約日 R6.4.1 は、セルの R6.4.10 に一致しない（境界付き）。"""
    assert check(ktable, "1", "method_raw", values(method_raw="一般競争（単価）")) == "match"
    assert check(ktable, "1", "method_raw", values(method_raw="一般競争")) == "mismatch"
    assert check(ktable, "2", "contract_date", values(contract_date_raw="R6.4.1")) == "mismatch"
    assert check(ktable, "2", "contract_date", values(contract_date_raw="R6.4.10")) == "match"
    digits = values(corp_number_state="digits13", corp_numbers=["0000000000001"])
    assert check(ktable, "1", "corp_numbers", digits) == "match"
    for variant in ("10000000000001", "0000000000002"):
        bad = values(corp_number_state="digits13", corp_numbers=[variant])
        assert check(ktable, "1", "corp_numbers", bad) == "mismatch"


@pytest.mark.parametrize(
    ("field", "csv_values", "cell", "expected"),
    [
        ("title_raw", values(title_raw="件名"), "", "unverifiable"),  # セルが空
        ("amount_yen", amount("1,000", 1000), "", "unverifiable"),
        (
            "amount_yen",
            amount("1,000", 1000),
            "1,000.5",
            "unverifiable",
        ),  # 前後が . の並びは数値にしない
        ("amount_yen", amount("1,000", 1000), "1,000 円", "match"),
        (
            "amount_yen",
            values(amount_raw="単価", amount_note="unit_price_text"),
            "",
            "unverifiable",
        ),
        ("contract_date", values(contract_date_raw="R6.4.1"), "令和6年4月1日", "unverifiable"),
        ("contract_date", values(contract_date_raw="R6.4.1"), "R6.4.1.2", "unverifiable"),
        ("contract_date", values(contract_date_raw=""), "R6.4.1", "unverifiable"),
    ],
)
def test_pd2_edge_cells(field: str, csv_values: dict, cell: str, expected: str) -> None:
    """PD-2：どちらかが空なら unverifiable。数値・日付の形がセルになければ unverifiable（迷えば照合不能）。"""
    assert pdftext.match_field(field, csv_values, cell) == expected


# ---------------------------------------------------------------------------
# PD-2：変化の区分ごとの照合項目（7.4）だけで判定する
# ---------------------------------------------------------------------------

ROWS = {
    # 競争入札の行：相手方が不一致、ほかは一致
    "S001:000001": {
        "row_status": "matched",
        "pdf_page": 1,
        "fields": {
            "amount_yen": "match",
            "contract_date": "match",
            "title_raw": "match",
            "counterparty_raw": "mismatch",
            "corp_numbers": "unverifiable",
            "method_raw": "match",
        },
    },
    # 随意契約の行：理由が不一致、ほかは一致
    "S003:000001": {
        "row_status": "matched",
        "pdf_page": 1,
        "fields": {
            "amount_yen": "match",
            "contract_date": "match",
            "title_raw": "match",
            "counterparty_raw": "match",
            "corp_numbers": "match",
            "reason_raw": "mismatch",
        },
    },
    # 随意契約の行：理由が照合不能、ほかは一致
    "S003:000002": {
        "row_status": "matched",
        "pdf_page": 1,
        "fields": {
            "amount_yen": "match",
            "contract_date": "match",
            "title_raw": "match",
            "counterparty_raw": "match",
            "corp_numbers": "match",
            "reason_raw": "unverifiable",
        },
    },
    "S001:000003": {
        "row_status": "ambiguous",
        "row_reason": "split_row",
        "fields": {
            "amount_yen": "unverifiable",
            "contract_date": "unverifiable",
            "title_raw": "unverifiable",
            "counterparty_raw": "unverifiable",
            "corp_numbers": "unverifiable",
            "method_raw": "unverifiable",
        },
    },
}


def test_pd2_fields_by_category_from_dataset_yaml() -> None:
    """PD-2：照合項目は dataset.yaml の fields_by_category（受注先の主張があれば fields_counterparty を加える）。"""
    assert reconcile.source_fields(ADAPTER, "amount_up") == [
        "amount_yen",
        "contract_date",
        "title_raw",
    ]
    assert reconcile.source_fields(ADAPTER, "reason_changed", counterparty=True) == [
        "title_raw",
        "reason_raw",
        "counterparty_raw",
        "corp_numbers",
    ]
    with pytest.raises(ValueError):
        reconcile.source_fields(ADAPTER, "unknown_category")


@pytest.mark.parametrize(
    ("row_refs", "category", "counterparty", "expected"),
    [
        (["S001:000001"], "amount_up", False, "refute"),  # 相手方は金額の区分の照合項目でない
        (["S001:000001"], "amount_up", True, "support"),  # 受注先の主張があれば見る
        (["S001:000001", "S003:000001"], "amount_down", False, "refute"),  # 理由は見ない
        (["S003:000001"], "reason_changed", False, "support"),
        (["S001:000001", "S003:000001"], "kyoso_to_zuikei", False, "support"),
        (["S003:000002"], "amount_up", False, "refute"),  # 理由の照合不能は金額の区分に効かない
        (["S003:000002"], "reason_changed", False, "unknown"),
        (["S001:000001", "S001:000003"], "amount_up", False, "unknown"),  # ambiguous の行
        (["S001:000009"], "amount_up", False, "unknown"),  # 照合の記録にない行
        (["S001:000003", "S003:000001"], "reason_changed", False, "support"),  # 不一致が優先
    ],
)
def test_pd2_t6b_judgment_uses_category_fields_only(
    row_refs: list[str], category: str, counterparty: bool, expected: str
) -> None:
    """PD-2：T6b は区分の照合項目だけで判定する。どれかが mismatch なら支持、すべて matched・match なら否定、
    それ以外（unverifiable・not_found・ambiguous）は未確認。"""
    fields = reconcile.source_fields(ADAPTER, category, counterparty=counterparty)
    assert reconcile.judge_t6b(ROWS, row_refs, fields) == expected
