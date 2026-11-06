"""法人番号の補完（test_plan.md の CN-1。P1-b。設計書7.4、D-076）。

期待値は手で計算した値。検査用数字は、法人番号の指定等に関する省令（平成26年財務省令第70号）第2条の算式
（9 −（基礎番号の最下位を1桁目として、奇数桁×1・偶数桁×2 の和を9で割った余り））で求めた。
合成の13桁は架空の値で、検査用数字だけを算式に合わせた。
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import httpx
import pytest

from conftest import Site, ledger_of, write_config
from kizashi import normalize, pdftext, runner
from kizashi.adapters import cao_procurement

MATCHED = {"title_raw": "match", "amount_yen": "match"}


@pytest.mark.parametrize(
    ("number", "ok"),
    [
        (
            "8700110005901",
            True,
        ),  # 国税庁の例：偶数桁の和13×2＋奇数桁の和11＝37、37÷9 の余り1、9−1＝8
        ("1700110005901", False),
        ("8700110005902", False),  # 奇数桁の和12、偶数桁の和13：12＋26＝38、余り2、9−2＝7
        ("8000000000001", True),  # 1 → 9−1＝8
        ("7000000000002", True),  # 2 → 9−2＝7
        ("9000000000009", True),  # 9 → 余り0、9−0＝9
        ("7000000000010", True),  # 2桁目の1×2＝2 → 9−2＝7
        ("1000000000008", True),  # 8 → 9−8＝1
        ("1000000000040", True),  # 2桁目の4×2＝8 → 9−8＝1
        ("1000000000009", False),  # 9 → 9 で、1ではない
        ("2000000000007", True),  # 7 → 9−7＝2
        ("0000000000001", False),  # 検査用数字は1〜9
        ("870011000590", False),  # 12桁
        ("87001100059012", False),  # 14桁
        ("８７００１１０００５９０１", False),  # 全角は呼ぶ側で NFKC にする
    ],
)
def test_cn1_check_digit(number: str, ok: bool) -> None:
    """CN-1：検査用数字の検査（D-076）。"""
    assert normalize.corp_check_digit_ok(number) is ok


@pytest.mark.parametrize(
    ("number", "raw", "ok"),
    [
        ("8700110005901", "8.70011E+12", True),  # 870011.0005901 → 870011
        ("8700110005901", "8.7E+12", True),  # 87.0011… → 87
        ("8700110005901", "8.7E+12 ", True),  # 末尾の空白
        ("8700110005901", "8.70012E+12", False),
        ("8700110005901", "8.7E+11", False),  # 桁の数が違う
        ("8123455000000", "8.12346E+12", True),  # 812345.5 → 四捨五入で 812346
        ("8123455000000", "8.12345E+12", False),  # 切り捨ての値とは比べない
        ("9999995000000", "1.00000E+13", True),  # 繰り上がり：99999.95 → 100000
        ("9999995000000", "9.99999E+12", False),  # 999999.5 → 1000000
        ("1000000000008", "1.00000E+12", True),
        ("2000000000007", "1.00000E+12", False),
        ("1000000000008", "1000000000008", False),  # 指数表記でない
        ("1000000000008", "", False),
    ],
)
def test_cn1_exponent_significant_digits(number: str, raw: str, ok: bool) -> None:
    """CN-1：指数表記に残る有効桁と、13桁の値を、表示の桁で四捨五入して比べる。"""
    assert normalize.exponent_matches(number, raw) is ok


@pytest.mark.parametrize(
    ("cell", "fields", "expected"),
    [
        ("1000000000008", MATCHED, ({"state": "valid_from_pdf"}, "1000000000008")),
        ("１０００００００００００８", MATCHED, ({"state": "valid_from_pdf"}, "1000000000008")),
        ("1000000000008\n", MATCHED, ({"state": "valid_from_pdf"}, "1000000000008")),
        ("1000000000009", MATCHED, ({"state": "unconfirmed", "failed": ["check_digit"]}, None)),
        (
            "2000000000007",
            MATCHED,
            ({"state": "unconfirmed", "failed": ["significant_digits"]}, None),
        ),
        (
            "1000000000008",
            {"title_raw": "mismatch", "amount_yen": "match"},
            ({"state": "unconfirmed", "failed": ["title_amount"]}, None),
        ),
        (
            "1000000000008",
            {"title_raw": "match", "amount_yen": "unverifiable"},
            ({"state": "unconfirmed", "failed": ["title_amount"]}, None),
        ),
        (
            "2000000000006",  # 6 → 9−6＝3 で、2ではない。有効桁も合わない
            {"title_raw": "mismatch", "amount_yen": "match"},
            (
                {
                    "state": "unconfirmed",
                    "failed": ["check_digit", "significant_digits", "title_amount"],
                },
                None,
            ),
        ),
        ("1.00000E+12", MATCHED, ({"state": "unconfirmed", "failed": ["pdf_cell"]}, None)),
        (
            "1000000000008\n1000000000040",
            {"title_raw": "match", "amount_yen": "mismatch"},
            ({"state": "unconfirmed", "failed": ["pdf_cell", "title_amount"]}, None),
        ),
        ("", MATCHED, ({"state": "unconfirmed", "failed": ["pdf_cell"]}, None)),
    ],
)
def test_cn1_completion_conditions(cell: str, fields: dict, expected: tuple) -> None:
    """CN-1：3条件（検査用数字・有効桁・件名と金額）と、PDF の欄の13桁がちょうど1つであること。"""
    assert pdftext.corp_completion("1.00000E+12", cell, fields) == expected


# ---------------------------------------------------------------------------
# 実行を通す：令和6年度の競争入札（S001・S002）の PDF の法人番号の欄だけを替える
# ---------------------------------------------------------------------------

# NO → PDF の法人番号の欄（7列目）。CSV はどれも `1.00000E+12`（指数表記）
PDF_CORP = {
    "1": "1000000000008",  # 3条件を満たす（検査用数字1、有効桁 1.00000E+12、件名と金額が一致）
    "6": "1000000000009",  # 検査用数字が合わない
    "10": "2000000000007",  # 検査用数字は合うが、有効桁が合わない
    "11": "1000000000040",  # 検査用数字と有効桁は合うが、件名を替えるので件名が合わない
    "12": "1000000000008\n1000000000040",  # 13桁が2つ
}
# 指数表記の行（合成の CSV の kyoso_2024_mini の NO 1・5・6・8・10・11・12）
EXPONENT_ROWS = (1, 5, 6, 8, 10, 11, 12)


def squash(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


@pytest.fixture
def pdf_with_corp_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    original = pdftext.extract_cells

    def extract(pages, spec):
        table = original(pages, spec)
        if not any(
            r.no == "1" and "令和6年度庁舎清掃業務" in squash(r.cells.get(1, ""))
            for r in table.rows
        ):
            return table
        for row in table.rows:
            if row.no in PDF_CORP:
                row.cells[7] = PDF_CORP[row.no]
            if row.no == "11":
                row.cells[1] += "（変更）"
        return table

    monkeypatch.setattr(pdftext, "extract_cells", extract)


def run(repo: Path, site: Site) -> dict:
    result = runner.start_run(repo, "cao-procurement", write_config(repo), transport=site.transport)
    assert result["state"] == "completed"
    return result


def source_rows(repo: Path, run_id: str, csv_source: str) -> dict:
    conn = ledger_of(repo, run_id)
    for (res,) in conn.execute(
        "SELECT result_json FROM computations WHERE kind = 'reconcile' ORDER BY calc_id"
    ):
        result = json.loads(res)
        if result["type"] == "source" and result["pairs"][0]["csv_source"] == csv_source:
            conn.close()
            return result
    conn.close()
    raise AssertionError(csv_source)


def test_cn1_run_adopts_only_rows_meeting_all_conditions(
    repo: Path, site: Site, pdf_with_corp_numbers: None
) -> None:
    """CN-1：3条件をすべて満たす行だけ valid_from_pdf にし、13桁を入れる。ほかは exponent のまま。"""
    result = run(repo, site)
    conn = ledger_of(repo, result["run_id"])
    states = {
        rid: (state, json.loads(numbers))
        for rid, state, numbers in conn.execute(
            "SELECT record_id, corp_number_state, corp_numbers_json FROM procurement_contracts"
            " WHERE record_id LIKE 'S001:%'"
        )
    }
    assert states["S001:000001"] == ("valid_from_pdf", ["1000000000008"])
    for n in EXPONENT_ROWS[1:]:
        assert states[f"S001:{n:06d}"] == ("exponent", []), n
    assert states["S001:000002"] == ("digits13", ["0000000000001"])
    assert "1000000000008" in cao_procurement.corp_numbers(conn)
    assert "1000000000040" not in cao_procurement.corp_numbers(conn)
    conn.close()

    record = source_rows(repo, result["run_id"], "S001")
    rows = record["rows"]
    expected = {
        1: {"state": "valid_from_pdf"},
        5: {"state": "unconfirmed", "failed": ["pdf_cell"]},
        6: {"state": "unconfirmed", "failed": ["check_digit"]},
        8: {"state": "unconfirmed", "failed": ["pdf_cell"]},
        10: {"state": "unconfirmed", "failed": ["significant_digits"]},
        11: {"state": "unconfirmed", "failed": ["title_amount"]},
        12: {"state": "unconfirmed", "failed": ["pdf_cell"]},
    }
    for n, completion in expected.items():
        assert rows[f"S001:{n:06d}"]["corp_completion"] == completion, n
    for n in (2, 3, 4, 7, 9):  # 指数表記でない行には書かない
        assert "corp_completion" not in rows[f"S001:{n:06d}"], n
    # T6b の法人番号の照合は unverifiable のまま（CSV では13桁を確かめられない）
    assert rows["S001:000001"]["fields"]["corp_numbers"] == "unverifiable"
    assert rows["S001:000011"]["fields"]["title_raw"] == "mismatch"
    assert record["counts"]["corp_completion"] == {"valid_from_pdf": 1, "unconfirmed": 6}
    assert result["reconcile_source"]["S001"]["corp_completion"] == {
        "valid_from_pdf": 1,
        "unconfirmed": 6,
    }


def test_cn1_run_without_pdf_numbers_keeps_exponent(repo: Path, site: Site) -> None:
    """CN-1：合成の PDF（CSV と同じ指数表記）では採用せず、指数表記の行の数だけ未確定になる。"""
    result = run(repo, site)
    expected = {  # 資料 → 指数表記の行の数（合成の CSV から数えた）
        "S001": 7,  # 令和6年度 競争入札：NO 1・5・6・8・10・11・12
        "S003": 3,  # 令和6年度 随意契約：NO 3・4・6
        "S005": 10,  # 令和7年度 競争入札：NO 1・3・5〜12
        "S007": 0,  # 令和7年度 随意契約：13桁で入っている
    }
    for source, n in expected.items():
        assert result["reconcile_source"][source]["corp_completion"] == {
            "valid_from_pdf": 0,
            "unconfirmed": n,
        }, source
    conn = ledger_of(repo, result["run_id"])
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM procurement_contracts WHERE corp_number_state = 'valid_from_pdf'"
    ).fetchone()
    conn.close()
    assert count == 0


# ---------------------------------------------------------------------------
# 回帰（D-080）：13桁に対応し得ない指数は、べき乗を計算せずに合わない（未確定）とする
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "1.00000E+1000010",  # 指数が大きすぎる（10 の 1000005 乗を作ると decimal.Overflow）
        "1.0E+" + "1" * 5000,  # 指数の文字列が長すぎる（int にすると ValueError）
        "1." + "0" * 5000 + "E+12",  # 仮数の桁が長すぎる
        "1.00000E+11",  # 13桁の値（10の12乗以上）には対応しない指数
        "1.00000E+14",
        "1.00000E+0",
    ],
)
def test_cn1_regression_d080_exponent_out_of_range(raw: str) -> None:
    """CN-1 回帰（D-080）：対応し得ない指数表記は例外にせず False。補完は significant_digits で未確定。"""
    assert normalize.exponent_matches("1000000000008", raw) is False
    assert pdftext.corp_completion(raw, "1000000000008", MATCHED) == (
        {"state": "unconfirmed", "failed": ["significant_digits"]},
        None,
    )


def test_cn1_regression_d080_run_continues(
    repo: Path, site: Site, pdf_with_corp_numbers: None
) -> None:
    """CN-1 回帰（D-080）：CSV の NO 1 の法人番号が `1.00000E+1000010` でも実行は完了し、その行は未確定。"""
    from conftest import URL, fixture_bytes

    data = fixture_bytes("kyoso_2024_mini.csv")
    assert data.count(b"1.00000E+12") == 7  # 指数表記の行（NO 1・5・6・8・10・11・12）
    site.routes[URL["k24_csv"]] = httpx.Response(
        200,
        headers={"content-type": "text/csv"},
        content=data.replace(b"1.00000E+12", b"1.00000E+1000010", 1),
    )
    result = run(repo, site)
    rows = source_rows(repo, result["run_id"], "S001")["rows"]
    assert rows["S001:000001"]["corp_completion"] == {
        "state": "unconfirmed",
        "failed": ["significant_digits"],
    }
    conn = ledger_of(repo, result["run_id"])
    state = conn.execute(
        "SELECT corp_number_state, corp_numbers_json FROM procurement_contracts"
        " WHERE record_id = 'S001:000001'"
    ).fetchone()
    conn.close()
    assert tuple(state) == ("exponent", "[]")
    assert result["reconcile_source"]["S001"]["corp_completion"] == {
        "valid_from_pdf": 0,
        "unconfirmed": 7,
    }
