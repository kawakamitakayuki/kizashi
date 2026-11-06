"""受注先を扱う主張の原本照合（test_plan.md の PD-2・CK-2。アダプタ仕様7.4、D-058）。

アダプタ仕様7.4：受注先を扱う主張がある場合は、どの区分でも、区分の照合項目に受注者名（`counterparty_raw`）と
法人番号（`corp_numbers`）を加える。「受注先を扱う主張」は、メモに出る主張・判定・対応・係数の根拠の位置が受注先の
列（`counterparty_raw`・`corp_number_raw`）を引くか、メモに出る基礎集計の条件に受注先の列（`corp_number_state`）が
あるもの（D-058。機械で見分けられる範囲）。

合成データ（tests/fixtures/make_cao_fixtures.py）から手で決めた値：
- 翻訳業務の行 S001:000012（令和6年度）と S005:000007（令和7年度）の受注者名は「株式会社テスト翻訳」。法人番号は
  CSV で「1.00000E+12」（指数表記）なので、法人番号の項目は照合不能（13桁でない状態は照らさない。アダプタ仕様7.3）。
- そのため、受注先を扱う主張を足すと、T6b は「否定」でなく「未確認」になり、比較成立は V8_CHANGE_NOT_NEGATED。
  2行ともメモの7節の「原本照合ができなかった行と項目」に出る。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ledger_of, rec
from kizashi import check, render
from kizashi.errors import KizashiError
from test_check import ROW_FROM, explanation, many_rounds, prepare  # noqa: F401

ROW_TO = "S005:000007"
COUNTERPARTY_EVIDENCE = {
    "source_id": "S001",
    "position": {"row_ref": ROW_FROM, "column": "counterparty_raw"},
    "quote": "株式会社テスト翻訳",
    "role": "support",
}


def counterparty_claim(lead_id: str) -> dict:
    return {
        "kind": "claim",
        "lead_id": lead_id,
        "type": "record",
        "fiscal_year": 2024,
        "text": "令和6年度の受注者は、原資料に記された事業者である。",
        "calc_ids": [],
        "evidence": [COUNTERPARTY_EVIDENCE],
    }


def codes_of(info: pytest.ExceptionInfo) -> list[str]:
    return [f["code"] for f in info.value.failures]


def test_pd2_without_counterparty_claim_t6b_uses_category_fields(
    repo: Path, synthetic_run: str
) -> None:
    """PD-2（対照）：受注先を扱う主張がなければ、区分の照合項目（金額・契約日・件名）だけで照らし、T6b は否定。"""
    lead = prepare(repo, synthetic_run)
    result = check.run_check(repo, synthetic_run, lead["lead"])
    assert result["t6"]["T6b"] == "refute"


def test_pd2_counterparty_claim_adds_counterparty_fields(repo: Path, synthetic_run: str) -> None:
    """PD-2・CK-2（D-058）：受注者名の欄を引用する主張を足すと、受注者名と法人番号も照らす。法人番号は指数表記で
    照合不能なので T6b は未確認になり、比較成立は V8_CHANGE_NOT_NEGATED（render もメモを書かない）。保留
    （原本照合ができない）のメモでは、2行が確認リストの先頭に、受注者名と法人番号の項目と一緒に出る。"""
    lead = prepare(repo, synthetic_run)
    rec(repo, synthetic_run, counterparty_claim(lead["lead"]))
    for call in (check.run_check, render.render_lead):
        with pytest.raises(KizashiError) as info:
            call(repo, synthetic_run, lead["lead"])
        assert "V8_CHANGE_NOT_NEGATED" in codes_of(info)
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "comparable",
            "hold_reason": "source_check",
        },
    )
    result = render.render_lead(repo, synthetic_run, lead["lead"])
    assert result["t6"]["T6b"] == "unknown"
    text = (repo / result["memo"]).read_text(encoding="utf-8")
    checklist = text.split("## 7 記者の確認リスト", 1)[1].split("## 8 ", 1)[0]
    first = checklist.split("### 原本照合ができなかった行と項目（先に確かめる）", 1)[1].split(
        "### 原本で確かめる行", 1
    )[0]
    for ref in (ROW_FROM, ROW_TO):
        assert ref in first
    assert "契約の相手方の商号又は名称及び住所" in first and "法人番号" in first


def t6b_judgment(repo: Path, run_id: str, lead_id: str) -> str:
    conn = ledger_of(repo, run_id)
    (judgment,) = conn.execute(
        "SELECT judgment FROM explanations WHERE lead_id = ? AND type = 'T6b'", (lead_id,)
    ).fetchone()
    conn.close()
    return judgment


def test_ck2_counterparty_in_judgment_evidence(repo: Path, synthetic_run: str) -> None:
    """CK-2（D-058）：判定（主体の同一性 T5）の根拠が受注者名の欄を引くときも、受注先を扱うとして受注者名と
    法人番号を照らす（T6b は未確認、比較成立は V8_CHANGE_NOT_NEGATED）。"""
    lead = prepare(repo, synthetic_run)
    rec(
        repo,
        synthetic_run,
        explanation(lead["lead"], "T5", "refute", evidence=[COUNTERPARTY_EVIDENCE], searched=""),
    )
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert "V8_CHANGE_NOT_NEGATED" in codes_of(info)
    assert t6b_judgment(repo, synthetic_run, lead["lead"]) == "unknown"


def test_ck2_counterparty_in_calc_filter(repo: Path, synthetic_run: str) -> None:
    """CK-2（D-058）：メモに出る基礎集計の条件に法人番号の状態（corp_number_state）があるときも、受注先を扱う
    として受注者名と法人番号を照らす（T6b は未確認）。"""
    lead = prepare(repo, synthetic_run)
    calc_id = rec(
        repo,
        synthetic_run,
        {
            "kind": "calc",
            "lead_id": lead["lead"],
            "op": "count",
            "table": "procurement_contracts",
            "filter": [
                {"col": "fiscal_year", "op": "=", "value": 2024},
                {"col": "group_key", "op": "=", "value": "翻訳業務"},
                {"col": "corp_number_state", "op": "=", "value": "exponent"},
            ],
            "label": "令和6年度の翻訳業務の行のうち、法人番号が指数表記の行の件数",
            "unit": "件",
        },
    )["id"]
    rec(
        repo,
        synthetic_run,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "observation",
            "text": f"令和6年度の翻訳業務の行のうち、法人番号が指数表記の行は{{calc:{calc_id}}}ある。",
            "calc_ids": [calc_id],
            "evidence": [],
        },
    )
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert "V8_CHANGE_NOT_NEGATED" in codes_of(info)
    assert t6b_judgment(repo, synthetic_run, lead["lead"]) == "unknown"


def test_ck2_counterparty_in_doc_value_position(repo: Path, synthetic_run: str) -> None:
    """CK-2・PD-2（D-065）：メモに出る文書値の位置が受注先の列（法人番号）を引くときも、受注先を扱うとして受注者名と
    法人番号を照らす。S001:000002 の法人番号は13桁の「0000000000001」だが、照らすのは候補の行（翻訳業務の2行。
    法人番号は指数表記で照合不能）なので T6b は未確認、比較成立の render は V8_CHANGE_NOT_NEGATED。"""
    lead = prepare(repo, synthetic_run)
    rec(
        repo,
        synthetic_run,
        {
            "kind": "doc_value",
            "lead_id": lead["lead"],
            "source_id": "S001",
            "position": {"row_ref": "S001:000002", "column": "corp_number_raw"},
            "quote": "0000000000001",
            "value": "0000000000001",
            "label": "架空の法人番号の表記",
            "unit": "番号",
        },
    )
    for call in (render.render_lead, check.run_check):  # render は判定を残さず、check は残す
        with pytest.raises(KizashiError) as info:
            call(repo, synthetic_run, lead["lead"])
        assert "V8_CHANGE_NOT_NEGATED" in codes_of(info)
    assert t6b_judgment(repo, synthetic_run, lead["lead"]) == "unknown"
