"""bf1bede の実装レビューの修正のうち、record と計算の回帰テスト（1-14・D-090⑪⑬）。

test_plan.md の CA-1（基礎集計の依頼の型）、CA-3（文書値）、IN-2（参照の検査）の回帰。期待値は
implementation_spec.md の 3.7（型の誤りは E_SCHEMA）、6.2 の lead、7.1・7.2 と、決定の D-090⑪⑬から書き写した。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from conftest import COMPARISON, add_evidence, ledger_of, make_lead, put, rec
from kizashi import calc, query, record
from kizashi.errors import KizashiError

SQLITE_MAX = 2**63 - 1
SQLITE_MIN = -(2**63)


def base_request(**overrides: object) -> dict:
    return {
        "kind": "calc",
        "op": "count",
        "table": "procurement_contracts",
        "filter": [{"col": "fiscal_year", "op": "=", "value": 2024}],
        "label": "令和6年度の件数",
        "unit": "件",
        **overrides,
    }


def record_error(repo: Path, run_id: str, data: dict) -> KizashiError:
    with pytest.raises(KizashiError) as exc:
        record.record(repo, run_id, put(repo, "x.json", data))
    return exc.value


# ---------------------------------------------------------------------------
# 1-14（CA-1）：基礎集計の依頼の型の誤りは E_SCHEMA（想定外のエラーにしない）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", [["procurement_contracts"], {"name": "procurement_contracts"}])
def test_ca1_table_must_be_a_string(repo: Path, synthetic_run: str, table: object) -> None:
    """CA-1（1-14）：table を配列やオブジェクトで書くと E_SCHEMA。"""
    error = record_error(repo, synthetic_run, base_request(table=table))
    assert error.code == "E_SCHEMA"


@pytest.mark.parametrize(
    "item",
    [
        {"col": "unit_price", "op": "=", "value": 10**30},
        {"col": "unit_price", "op": "=", "value": SQLITE_MAX + 1},
        {"col": "unit_price", "op": "=", "value": SQLITE_MIN - 1},
        {"col": "unit_price", "op": "in", "value": [0, 10**30]},
        {"col": "fiscal_year", "op": "=", "value": 10**30},
    ],
)
def test_ca1_filter_integer_out_of_sqlite_range(repo: Path, synthetic_run: str, item: dict) -> None:
    """CA-1（1-14）：条件の値の整数が SQLite の整数の範囲（−2^63〜2^63−1）の外なら E_SCHEMA。"""
    error = record_error(repo, synthetic_run, base_request(filter=[item]))
    assert error.code == "E_SCHEMA"


@pytest.mark.parametrize("value", [SQLITE_MAX, SQLITE_MIN])
def test_ca1_filter_integer_at_sqlite_bounds(repo: Path, synthetic_run: str, value: int) -> None:
    """CA-1（1-14）：範囲の端の整数は受け付ける（当たる行はないので件数は0）。"""
    result = rec(
        repo, synthetic_run, base_request(filter=[{"col": "unit_price", "op": "=", "value": value}])
    )
    assert result["value"] == "0"


# ---------------------------------------------------------------------------
# D-090⑪（CA-3）：小数の直後の句点「．」は小数点ではない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "values"),
    [
        ("単価の比は0.5．", [Decimal("0.5")]),
        ("単価の比は0.5.", [Decimal("0.5")]),
        ("比は.5．", [Decimal("0.5")]),
        ("件数は12．", [Decimal(12)]),
        ("金額は1,200．", [Decimal(1200)]),
        ("版は1.2.3．", []),  # 数値として正しくない並びは、句点を除いても取り出さない
    ],
)
def test_ca3_sentence_period_after_number(quote: str, values: list[Decimal]) -> None:
    """CA-3（D-090⑪）：数値の並びの末尾の「.」（全角の「．」を含む）は、直後が数字でないので句点とみなす。"""
    assert [n.value for n in calc.numbers_in_quote(quote)] == values


def test_ca3_doc_value_before_sentence_period(repo: Path, synthetic_run: str) -> None:
    """CA-3（D-090⑪）：「単価の比は0.5．」の引用から、文書値 0.5 を登録できる。5 は登録できない。"""
    lead = make_lead(repo, synthetic_run)
    source = add_evidence(repo, synthetic_run, ["単価の比は0.5．"])
    proposal = {
        "kind": "doc_value",
        "lead_id": lead["lead"],
        "source_id": source,
        "position": {"page": 1},
        "quote": "単価の比は0.5．",
        "value": "0.5",
        "label": "単価の比（公表元の記載）",
        "unit": "倍",
    }
    assert rec(repo, synthetic_run, proposal)["value"] == "0.5"
    error = record_error(repo, synthetic_run, {**proposal, "value": "5"})
    assert error.code == "E_DOC_VALUE_MISMATCH"


# ---------------------------------------------------------------------------
# D-090⑬（IN-2）：照合の計算を探索の候補に所属させない
# ---------------------------------------------------------------------------


def _reconcile_calc(repo: Path, run_id: str) -> str:
    conn = ledger_of(repo, run_id)
    (calc_id,) = conn.execute(
        "SELECT calc_id FROM computations WHERE kind = 'reconcile' ORDER BY calc_id LIMIT 1"
    ).fetchone()
    conn.close()
    return calc_id


def test_in2_lead_cannot_own_reconcile_calc(repo: Path, synthetic_run: str) -> None:
    """IN-2（D-090⑬）：kind: lead の calc_ids に照合の計算を入れると E_SCHEMA。照合の計算の lead_id は変えず、
    候補もできない。"""
    (repo / "inbox" / "q-001.sql").write_text(
        "SELECT fiscal_year, amount_yen FROM procurement_contracts WHERE group_key = '翻訳業務'",
        encoding="utf-8",
    )
    query.run_query(repo, synthetic_run, "inbox/q-001.sql")
    k_from = rec(repo, synthetic_run, _sum("翻訳業務", 2024), name="a.json")["id"]
    k_to = rec(repo, synthetic_run, _sum("翻訳業務", 2025), name="b.json")["id"]
    reconcile = _reconcile_calc(repo, synthetic_run)
    conn = ledger_of(repo, synthetic_run)
    leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    conn.close()
    error = record_error(
        repo,
        synthetic_run,
        {
            "kind": "lead",
            "query_file": "q-001.sql",
            "lead_key": "翻訳業務",
            "change_category": "amount_up",
            "period_from": 2024,
            "period_to": 2025,
            "calc_from": k_from,
            "calc_to": k_to,
            "calc_ids": [reconcile],
            "comparison": COMPARISON,
            "note": "探索で見つけた",
        },
    )
    assert error.code == "E_SCHEMA"
    assert reconcile in error.message
    conn = ledger_of(repo, synthetic_run)
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == leads
    (owner,) = conn.execute(
        "SELECT lead_id FROM computations WHERE calc_id = ?", (reconcile,)
    ).fetchone()
    conn.close()
    assert owner is None


def _sum(group: str, year: int) -> dict:
    return {
        "kind": "calc",
        "op": "sum",
        "table": "procurement_contracts",
        "column": "amount_yen",
        "filter": [
            {"col": "group_key", "op": "=", "value": group},
            {"col": "fiscal_year", "op": "=", "value": year},
        ],
        "label": f"{group}の契約金額の合計",
        "unit": "円",
    }
