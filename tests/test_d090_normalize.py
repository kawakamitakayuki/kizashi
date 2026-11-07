"""bf1bede の実装レビューの修正のうち、正規化の回帰テスト（D-090⑤・D-091③。parser_version cao-procurement/2）。

test_plan.md の NR-3（金額）、NR-7（理由の分類と根拠条文）、QC-3（Q13 の突合）の回帰。期待値は
adapter_cao_procurement.md の 4.1・4.4・4.7・5章（Q11・Q13）と、決定の D-090⑤（NFKC の後に空白・改行を除いて
から分類・条文を取り出す。元の値は残す）・D-091③（2^63 以上の整数の金額は amount_yen を NULL、amount_note を
out_of_range にし、別経路にも同じ上限を独立に書く。2^63−1 は取り込む）から書き写した。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import URL, Site, fixture_bytes, ledger_of, write_config
from kizashi import independent_reader, normalize, runner
from kizashi.adapters import cao_procurement

MAX = 2**63 - 1  # 9223372036854775807


# ---------------------------------------------------------------------------
# D-090⑤（NR-7）：理由の分類と根拠条文は、NFKC の後に空白・改行を除いてから見る
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("性質又は目的が競争を\n許さない場合", "nature_no_competition"),
        ("企画\n競争による", "planning_competition"),
        ("公　募を行った", "open_call"),
        ("入札が不\r\n調となった", "failed_tender"),
        ("慈 善のため", "charity"),
        ("その他の理由", "other"),
    ],
)
def test_nr7_reason_category_ignores_spaces_and_newlines(reason: str, category: str) -> None:
    """NR-7（D-090⑤）：語の途中の改行・空白（全角を含む）で分類を取りこぼさない。"""
    assert cao_procurement.reason_category(reason) == category


@pytest.mark.parametrize(
    ("reason", "articles"),
    [
        ("予算決算及び会計令第99条\n第2号による", ["予決令第99条第2号"]),
        ("会計法\n第29条の3\n第4項", ["会計法第29条の3第4項"]),
        ("予決令 第102条の4 第3号", ["予決令第102条の4第3号"]),
    ],
)
def test_nr7_reason_articles_ignore_spaces_and_newlines(reason: str, articles: list[str]) -> None:
    """NR-7（D-090⑤）：条文の途中の改行・空白で、項・号を取りこぼさない。"""
    assert cao_procurement.reason_articles(reason) == articles


# ---------------------------------------------------------------------------
# D-091③（NR-3）：2^63 以上の整数の金額は out_of_range。両方の経路で同じ境界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9223372036854775807", (MAX, None)),
        ("9,223,372,036,854,775,807", (MAX, None)),
        ("9223372036854775808", (None, "out_of_range")),
        ("9,223,372,036,854,775,808", (None, "out_of_range")),
        ("99999999999999999999", (None, "out_of_range")),
        ("単価 9223372036854775808円", (None, "unit_price_text")),  # 数値の形でなければ従来どおり
    ],
)
def test_nr3_amount_integer_range(raw: str, expected: tuple) -> None:
    """NR-3（D-091③）：2^63−1 は取り込み、2^63 は整数の範囲外（数値なので non_numeric にしない）。"""
    assert normalize.parse_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9223372036854775807", MAX),
        ("9,223,372,036,854,775,808", None),
        ("99999999999999999999", None),
    ],
)
def test_nr3_independent_reader_has_the_same_bound(raw: str, expected: int | None) -> None:
    """NR-3（D-091③）：別経路の読み込み処理にも同じ格納できる範囲を独立に書く（正規化の経路の関数を呼ばない）。"""
    assert independent_reader.parse_amount_yen(raw) == expected


def test_qc3_out_of_range_amount_run(repo: Path) -> None:
    """QC-3・NR-3（D-091③）：範囲外の金額の行があっても実行は完了する。分野の表は amount_yen が NULL・
    amount_note が out_of_range で、元の文字列を残す。Q11 に件数を出し、Q13 の件数と Decimal の合計の突合は
    合格のまま（合計から除く）。"""
    big = b"9,223,372,036,854,775,808"
    data = fixture_bytes("kyoso_2024_mini.csv")
    assert data.count(b"350,000") == 1  # NO 9 の会議録作成業務の契約金額
    site = Site(
        {
            URL["k24_csv"]: httpx.Response(
                200, headers={"content-type": "text/csv"}, content=data.replace(b"350,000", big)
            )
        }
    )
    result = runner.start_run(repo, "cao-procurement", write_config(repo), transport=site.transport)
    assert result["state"] == "completed"
    conn = ledger_of(repo, result["run_id"])
    (source_id,) = conn.execute(
        "SELECT source_id FROM sources WHERE logical_key = 'cao-procurement|2024|kyoso|csv'"
    ).fetchone()
    row = conn.execute(
        "SELECT p.amount_yen, p.amount_note, p.amount_raw, r.cells_json, r.parser_version"
        " FROM procurement_contracts p JOIN records r ON r.record_id = p.record_id"
        " WHERE p.title_raw = '会議録作成業務' AND p.fiscal_year = 2024"
    ).fetchone()
    counts = json.loads(conn.execute("SELECT counts_json FROM runs").fetchone()[0])
    conn.close()
    assert (row["amount_yen"], row["amount_note"]) == (None, "out_of_range")
    assert row["amount_raw"] == big.decode()
    assert json.loads(row["cells_json"])["amount_raw"] == big.decode()
    assert row["parser_version"] == "cao-procurement/2"
    quality = counts["quality"][source_id]
    assert quality["Q11"]["counts"]["out_of_range"] == 1
    assert "out_of_range 1" in quality["Q11"]["detail"]
    assert quality["Q13"]["state"] == "pass"
