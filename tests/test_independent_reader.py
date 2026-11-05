"""別経路の読み込み処理のテスト（test_plan.md の IR-1。アダプタ仕様6章）。

期待値は、合成データ（tests/fixtures/make_cao_fixtures.py の表）と、テストの中で書いた小さな CSV から
手で決めた値。
"""

from __future__ import annotations

import ast
import csv
import io
from pathlib import Path

import pytest
import yaml

from conftest import DATASET_DIR, FIXTURES, REPO
from kizashi import independent_reader as ir
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

ADAPTER = load_adapter(REPO, "cao-procurement")
KYOSO = ADAPTER.file_spec(2024, "kyoso")
ZUIKEI_27 = ADAPTER.file_spec(2025, "zuikei")


def expected_headers(spec, keys: tuple[str, ...]) -> dict[str, str]:
    return {k: str(spec.take[k]["header"]) for k in keys}


def write_csv(path: Path, rows: list[list[str]], encoding: str = "cp932") -> Path:
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    path.write_bytes(buf.getvalue().encode(encoding))
    return path


# ---------------------------------------------------------------------------
# IR-1：見出しの文字列で列を特定する
# ---------------------------------------------------------------------------


def test_ir1_columns_found_by_header_strings() -> None:
    """IR-1：全角の ＮＯ・全角の括弧（方式の列）があっても、NFKC と空白の除去の後に列を特定できる。"""
    keys = (
        "no",
        "title_raw",
        "contract_date_raw",
        "counterparty_raw",
        "corp_number_raw",
        "method_raw",
        "amount_raw",
    )
    table = ir.read_by_headers(
        FIXTURES / "kyoso_2024_mini.csv", "cp932", expected_headers(KYOSO, keys)
    )
    assert table.columns == {
        "no": 0,
        "title_raw": 1,
        "contract_date_raw": 4,
        "counterparty_raw": 5,
        "corp_number_raw": 7,
        "method_raw": 8,
        "amount_raw": 10,
    }
    # 合成データの競争入札2024は12件。論理レコード番号は1から
    assert [no for no, _ in table.records] == list(range(1, 13))
    first = dict(table.records[0][1])
    assert first == {
        "no": "1",
        "title_raw": "令和６年度庁舎清掃業務",
        "contract_date_raw": "R6.4.1",
        "counterparty_raw": "株式会社テスト清掃",
        "corp_number_raw": "1.00000E+12",
        "method_raw": "一般競争",
        "amount_raw": "1,200,000",
    }
    # セルの中の改行はそのまま（元の値を変えない）
    assert table.records[2][1]["title_raw"] == "システム保守業務\n（第３期）"
    assert table.records[7][1]["amount_raw"] == "単価契約\n１件当たり　250円"


def test_ir1_header_with_newlines_in_cell() -> None:
    """IR-1：見出しのセルの中に改行がある随意契約の理由の列（令和7年度の27列）も特定できる。"""
    table = ir.read_by_headers(
        FIXTURES / "zuikei_2025_mini.csv",
        "cp932",
        expected_headers(ZUIKEI_27, ("no", "reason_raw", "amount_raw")),
    )
    assert table.columns == {"no": 0, "reason_raw": 8, "amount_raw": 10}
    assert len(table.records) == 7
    assert table.records[5][1]["amount_raw"] == "USD 10,000"


def test_ir1_header_row_is_searched_not_fixed(tmp_path: Path) -> None:
    """IR-1：見出しの行は「NO」で探す（固定の行番号を使わない）。表題が3行でも読める。"""
    path = write_csv(
        tmp_path / "x.csv",
        [
            ["表題"],
            ["表題の2行目"],
            ["", "令和７年７月11日現在"],
            ["ＮＯ", "物品役務等の名称\n及び数量", "契約金額"],
            ["", "", ""],
            ["1", "件名A", "1,000"],
            ["", "", ""],  # 空のレコードも数える
            ["3", "件名C", "２，５００"],
        ],
    )
    table = ir.read_by_headers(
        path, "cp932", {"title_raw": "物品役務等の名称及び数量", "amount_raw": "契約金額"}
    )
    assert table.columns == {"title_raw": 1, "amount_raw": 2}
    assert [(no, cells["title_raw"]) for no, cells in table.records] == [
        (1, "件名A"),
        (2, ""),
        (3, "件名C"),
    ]


def test_ir1_header_joins_two_rows_without_filling(tmp_path: Path) -> None:
    """IR-1：見出しは NO の行と次の行を縦に連結する。上の行の空欄を左の値で埋めない。"""
    path = write_csv(
        tmp_path / "x.csv",
        [
            ["ＮＯ", "公益法人の場合", "", "備考"],
            ["", "公益法人の区分", "国所管、都道府県所管の区分", ""],
            ["1", "", "", "なし"],
        ],
    )
    table = ir.read_by_headers(
        path,
        "cp932",
        {"a": "公益法人の場合公益法人の区分", "b": "国所管、都道府県所管の区分", "c": "備考"},
    )
    assert table.columns == {"a": 1, "b": 2, "c": 3}
    with pytest.raises(KizashiError) as info:
        ir.read_by_headers(path, "cp932", {"b": "公益法人の場合国所管、都道府県所管の区分"})
    assert info.value.code == "V3B_HEADER_UNRESOLVED"


@pytest.mark.parametrize(
    ("expected", "why"),
    [
        ({"x": ""}, "見出しが空"),  # 見出しが空の列は使えない
        ({"x": "   "}, "見出しが空"),
        ({"note_raw": "備考"}, "2つ以上"),  # 一致が2つ
        ({"x": "存在しない見出し"}, "見つからない"),
    ],
)
def test_ir1_unresolved_headers(tmp_path: Path, expected: dict, why: str) -> None:
    """IR-1：空の見出し、一致が2つ以上の列、一致がない見出しは V3B_HEADER_UNRESOLVED。"""
    path = write_csv(
        tmp_path / "x.csv",
        [
            ["ＮＯ", "", "備考", "備　考"],
            ["", "", "", ""],
            ["1", "a", "b", "c"],
        ],
    )
    with pytest.raises(KizashiError) as info:
        ir.read_by_headers(path, "cp932", expected)
    assert info.value.code == "V3B_HEADER_UNRESOLVED"
    assert why in info.value.message


def test_ir1_no_header_row(tmp_path: Path) -> None:
    """IR-1：「NO」の行がなければ V3B_HEADER_UNRESOLVED。"""
    path = write_csv(tmp_path / "x.csv", [["番号", "件名"], ["1", "a"]])
    with pytest.raises(KizashiError) as info:
        ir.read_by_headers(path, "cp932", {"title_raw": "件名"})
    assert info.value.code == "V3B_HEADER_UNRESOLVED"


def test_ir1_no_key_is_a_string_in_dataset_yaml() -> None:
    """IR-1：dataset.yaml の "no" は文字列のキーとして読める（引用符がなければ YAML で false になる）。"""
    assert yaml.safe_load("no: 1") == {False: 1}
    config = yaml.safe_load((DATASET_DIR / "dataset.yaml").read_bytes())
    for name, column_set in config["column_sets"].items():
        assert "no" in column_set["take"], name
        assert column_set["take"]["no"]["header"] == "NO"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,200,000", 1200000),
        ("350000", 350000),
        (" 350,000 ", 350000),
        ("１，２００", 1200),  # NFKC
        ("0", 0),
        ("1,2000", None),
        ("12,345ドル", None),
        ("単価契約\n１件当たり　250円", None),
        ("-", None),
        ("", None),
        ("1 200", None),
    ],
)
def test_ir1_amount_rule_written_independently(raw: str, expected: int | None) -> None:
    """IR-1：金額はアダプタ仕様4.4と同じ正規表現で整数にする（正規化の経路の関数を呼ばない）。"""
    assert ir.parse_amount_yen(raw) == expected


def test_ir1_does_not_import_normalization_path() -> None:
    """IR-1：別経路の読み込み処理は、正規化の経路のモジュール（normalize・readers・adapters）を使わない。"""
    source = (REPO / "src" / "kizashi" / "independent_reader.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported |= {f"{module}.{a.name}" for a in node.names}
    for forbidden in ("kizashi.normalize", "kizashi.readers", "kizashi.adapters"):
        assert not any(name.startswith(forbidden) for name in imported), forbidden
