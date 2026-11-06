"""検証の V5〜V7（test_plan.md の CK-1（V5〜V7）・CK-4。implementation_spec.md 9章、設計書5.5）。

合成データの実行の検出の候補「翻訳業務」（S1・amount_up）を、test_check.py の prepare で比較成立の案にしてから、
1か所ずつ変える。値は合成データ（tests/fixtures/make_cao_fixtures.py）から手で決めた：
- 候補の行は S001:000012（令和6年度の競争入札、650,000円）と S005:000007（令和7年度、750,000円）。
- 担当官の氏名は「架空　太郎」など、担当官の所在地は「テスト県架空市官庁町１－１」、相手方の住所は
  「テスト県模擬市本町２－２」（どれも架空）。随意契約の内部用の列には「会計第一係」「大臣官房」がある。
- 原本の CSV の取り込まない列（index 3・6）を書き換えて、改行のあるセルと `?` のある住所を作る（正規化の値は
  変わらない）。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from conftest import ledger_of, put, rec
from kizashi import check, record
from kizashi.errors import KizashiError
from test_check import (
    ROW_FROM,
    T2_EVIDENCE,
    codes,
    explanation,
    failing,
    many_rounds,  # noqa: F401  巡回数の上限はここでは確かめない（CK-3 は test_rounds.py）
    prepare,
    tamper,
)
from test_public import rewrite_raw_csv

OFFICER_ADDR = "テスト県架空市官庁町１－１"
CP_ADDR = "テスト県模擬市本町２－２"


def passes(repo: Path, run_id: str, lead_id: str) -> dict:
    result = check.run_check(repo, run_id, lead_id)
    assert result["passed"] is True
    return result


# ---------------------------------------------------------------------------
# V5 比較の条件
# ---------------------------------------------------------------------------


def test_ck1_v5_condition_missing(repo: Path, synthetic_run: str) -> None:
    """V5_CONDITION_MISSING：比較の条件の6項目のどれかが空なら不合格。そろっていれば合格。"""
    lead = prepare(repo, synthetic_run)
    passes(repo, synthetic_run, lead["lead"])
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET comparison_json = json_set(comparison_json, '$.period', '  ') WHERE lead_id = ?",
        (lead["lead"],),
    )
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V5_CONDITION_MISSING"] and "期間" in info.value.failures[0]["message"]
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET comparison_json = json_remove(comparison_json, '$.formula') WHERE lead_id = ?",
        (lead["lead"],),
    )
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert "期間・算式" in info.value.failures[0]["message"]


def test_ck1_v5_unresolved_comparison(repo: Path, synthetic_run: str) -> None:
    """V5_UNRESOLVED_COMPARISON：比較の条件が unresolved なのに、両年度を比べる主張（観測）か比較成立の案がある。
    比べる主張を取り下げた保留は合格する（各年度の値を別々に書いた主張は比較の主張でない）。"""
    lead = prepare(repo, synthetic_run)
    conclusion = {
        "kind": "conclusion",
        "lead_id": lead["lead"],
        "outcome": "change",
        "comparison_state": "unresolved",
        "explanation_status": "none",
    }
    rec(repo, synthetic_run, conclusion)
    found = failing(repo, synthetic_run, lead["lead"])
    assert "V5_UNRESOLVED_COMPARISON" in found
    hold = {**conclusion, "outcome": "hold", "hold_reason": "comparison"}
    del hold["explanation_status"]
    rec(repo, synthetic_run, hold)
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V5_UNRESOLVED_COMPARISON"]
    assert lead["observation"] in info.value.failures[0]["affected_ids"]
    rec(
        repo,
        synthetic_run,
        {"kind": "withdraw", "target": lead["observation"], "reason": "同種と確かめられない。"},
    )
    rec(
        repo,
        synthetic_run,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "observation",
            "text": f"令和7年度の契約金額の合計は{{calc:{lead['calc_to']}}}である。",
            "calc_ids": [lead["calc_to"]],
            "evidence": [],
        },
    )
    passes(repo, synthetic_run, lead["lead"])


UNRESOLVED_HEADING = "### 比較の条件を確定できない（unresolved）候補の主張"


def test_ck1_v5_unresolved_boundary_is_left_to_reporter(repo: Path, synthetic_run: str) -> None:
    """V5_UNRESOLVED_COMPARISON の境界（D-056④）：「比較の主張」は年度間の比較を述べる主張で、CLI が機械で
    見つけるのは、2つ以上の年度の計算を参照する主張と比較成立の結論の案だけ。1つの年度の値だけを参照して文で
    比べる主張（「前年度より増え」）は check を通る（限界）。そのため unresolved の候補のメモの7節に、主張の文を
    記者が確かめる項目を出す。比較の条件が確定した候補のメモには出さない。"""
    from kizashi import render

    lead = prepare(repo, synthetic_run)
    comparable = render.render_lead(repo, synthetic_run, lead["lead"])
    assert UNRESOLVED_HEADING not in (repo / comparable["memo"]).read_text(encoding="utf-8")
    rec(
        repo,
        synthetic_run,
        {"kind": "withdraw", "target": lead["observation"], "reason": "同種と確かめられない。"},
    )
    rec(
        repo,
        synthetic_run,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "observation",
            "text": f"契約金額は前年度より増え、{{calc:{lead['calc_to']}}}になった。",
            "calc_ids": [lead["calc_to"]],
            "evidence": [],
        },
    )
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "unresolved",
            "hold_reason": "comparison",
        },
    )
    passes(repo, synthetic_run, lead["lead"])  # 機械では見つけない
    held = render.render_lead(repo, synthetic_run, lead["lead"])
    text = (repo / held["memo"]).read_text(encoding="utf-8")
    checklist = text.split("## 7 記者の確認リスト", 1)[1].split("## 8 ", 1)[0]
    assert UNRESOLVED_HEADING in checklist
    assert "1つの年度の値だけを参照して文で比べる主張は見つけない" in checklist


def test_ck1_v5_adjusted_without_values(repo: Path, synthetic_run: str) -> None:
    """V5_ADJUSTED_NO_VALUES：adjusted なのに補正の方法と補正前後の値がない。"""
    lead = prepare(repo, synthetic_run)
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET comparison_state = 'adjusted', adjustment_json = NULL WHERE lead_id = ?",
        (lead["lead"],),
    )
    assert "V5_ADJUSTED_NO_VALUES" in failing(repo, synthetic_run, lead["lead"])
    adjustment = {"method": "分量で補正した。", "before": lead["calc_from"], "after": "K9999"}
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET adjustment_json = ? WHERE lead_id = ?",
        (json.dumps(adjustment), lead["lead"]),
    )
    assert "V5_ADJUSTED_NO_VALUES" in failing(repo, synthetic_run, lead["lead"])
    adjustment["after"] = lead["calc_to"]
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET adjustment_json = ? WHERE lead_id = ?",
        (json.dumps(adjustment), lead["lead"]),
    )
    assert "V5_ADJUSTED_NO_VALUES" not in check_codes(repo, synthetic_run, lead["lead"])


def check_codes(repo: Path, run_id: str, lead_id: str) -> list[str]:
    try:
        check.run_check(repo, run_id, lead_id)
    except KizashiError as exc:
        return [f["code"] for f in exc.failures or []]
    return []


def test_ck1_v5_bad_rate(repo: Path, synthetic_run: str) -> None:
    """V5_BAD_RATE：増減率の基準（令和6年度の合計）が0以下なら不合格（台帳を書き換えて作る。record は作らない）。
    基準が正なら V5_BAD_RATE は出ない。"""
    lead = prepare(repo, synthetic_run)
    assert "V5_BAD_RATE" not in check_codes(repo, synthetic_run, lead["lead"])
    tamper(
        repo,
        synthetic_run,
        "UPDATE computations SET value = '0' WHERE calc_id = ?",
        (lead["calc_from"],),
    )
    found = check_codes(repo, synthetic_run, lead["lead"])
    assert "V5_BAD_RATE" in found and "V3A_RECALC_MISMATCH" in found


def test_ck1_v5_partial_year(
    repo: Path, synthetic_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5_PARTIAL_YEAR：比較成立なのに、年度途中（dataset.yaml の partial_year）の年度と年度全体を比べている。
    保留の案なら V5_PARTIAL_YEAR にしない。"""
    lead = prepare(repo, synthetic_run)
    original = check.load_adapter

    def partial_2025(root, domain):
        adapter = original(root, domain)
        files = {
            key: dataclasses.replace(spec, partial_year=key[0] == 2025)
            for key, spec in adapter.files.items()
        }
        return dataclasses.replace(adapter, files=files)

    monkeypatch.setattr(check, "load_adapter", partial_2025)
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V5_PARTIAL_YEAR"] and "2025年度" in info.value.failures[0]["message"]
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "comparable",
            "hold_reason": "comparison",
        },
    )
    passes(repo, synthetic_run, lead["lead"])


# ---------------------------------------------------------------------------
# V6 説明の判定
# ---------------------------------------------------------------------------


def test_ck1_v6_type_missing(repo: Path, synthetic_run: str) -> None:
    """V6_TYPE_MISSING：6つの型（T6は3つ）のどれかに判定がない。"""
    lead = prepare(repo, synthetic_run)
    tamper(repo, synthetic_run, "DELETE FROM explanations WHERE type = 'T5'")
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert (
        codes(info)[0] == "V6_TYPE_MISSING"
        and "T5 主体の同一性" in info.value.failures[0]["message"]
    )


def test_ck1_v6_no_evidence_and_no_search_scope(repo: Path, synthetic_run: str) -> None:
    """V6_NO_EVIDENCE：根拠の位置がない否定。V6_NO_SEARCH_SCOPE：探した範囲がない未確認。"""
    lead = prepare(repo, synthetic_run)
    rec(repo, synthetic_run, explanation(lead["lead"], "T1", "refute", evidence=[], searched=""))
    assert failing(repo, synthetic_run, lead["lead"]) == ["V6_NO_EVIDENCE"]
    rec(repo, synthetic_run, explanation(lead["lead"], "T1", "unknown", searched=""))
    assert failing(repo, synthetic_run, lead["lead"]) == ["V6_NO_SEARCH_SCOPE"]
    rec(repo, synthetic_run, explanation(lead["lead"], "T1", "unknown"))
    passes(repo, synthetic_run, lead["lead"])


def test_ck1_v6_self_evidence(repo: Path, synthetic_run: str) -> None:
    """V6_SELF_EVIDENCE：T1〜T5 の支持の根拠が、説明する対象のレコード（候補の行）だけ。候補でない行
    （令和6年度の別の業務の行 S001:000001）の記載を足せば、自分の行だけではない。"""
    lead = prepare(repo, synthetic_run)
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "artifact",
            "comparison_state": "comparable",
        },
    )
    rec(repo, synthetic_run, explanation(lead["lead"], "T2", "support", evidence=[T2_EVIDENCE]))
    assert failing(repo, synthetic_run, lead["lead"]) == ["V6_SELF_EVIDENCE"]
    other = {
        "source_id": "S001",
        "position": {"row_ref": "S001:000001", "column": "contract_date_raw"},
        "quote": "R6.4.1",
        "role": "background",
    }
    assert T2_EVIDENCE["position"]["row_ref"] == ROW_FROM
    rec(
        repo,
        synthetic_run,
        explanation(lead["lead"], "T2", "support", evidence=[T2_EVIDENCE, other]),
    )
    passes(repo, synthetic_run, lead["lead"])


# ---------------------------------------------------------------------------
# V7（check の時点）。台帳を書き換えて、record を通らない文字列を作る
# ---------------------------------------------------------------------------


def test_ck1_v7_raw_number_at_check(repo: Path, synthetic_run: str) -> None:
    """V7_RAW_NUMBER（check の時点）：主張の本文の生の数値は不合格。{calc:ID} と年度は対象外。"""
    lead = prepare(repo, synthetic_run)
    tamper(
        repo,
        synthetic_run,
        "UPDATE claims SET text = text || '差は1,200万円。' WHERE claim_id = ?",
        (lead["observation"],),
    )
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V7_RAW_NUMBER"] and "1,200" in info.value.failures[0]["message"]


def test_ck4_names_and_excluded_values_at_check(repo: Path, synthetic_run: str) -> None:
    """CK-4（check の時点）：仮説の氏名、比較の条件（数値の検査を当てない欄）の担当官の所在地は不合格。語は
    返さない。比較の条件の「1件」は数値として不合格にしない。"""
    lead = prepare(repo, synthetic_run)
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET hypothesis = '架空 太郎が担当した業務の分量が増えた。' WHERE lead_id = ?",
        (lead["lead"],),
    )
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET comparison_json = json_set(comparison_json, '$.population', ?)"
        " WHERE lead_id = ?",
        (f"1件の契約のうち{OFFICER_ADDR}の部局のもの", lead["lead"]),
    )
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    messages = [f["message"] for f in info.value.failures]
    assert codes(info) == ["V7_PERSONAL_NAME", "V7_PERSONAL_NAME"]
    assert any(m.startswith("V7_PERSONAL_NAME：hypothesis の 1〜5 文字目") for m in messages)
    assert any("comparison.population" in m and "取り込まない列の値" in m for m in messages)
    text = json.dumps(info.value.to_dict(), ensure_ascii=False)
    assert "架空 太郎" not in text and "架空太郎" not in text and "官庁町" not in text


def test_ck4_strong_word_is_a_warning_at_check(repo: Path, synthetic_run: str) -> None:
    """CK-4（check の時点）：強い語は警告で、不合格にしない。照合済みの引用の中は対象外。"""
    lead = prepare(repo, synthetic_run)
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET hypothesis = '分量が増えたが、不正確な見積もりの可能性もある。談合ではない。'"
        " WHERE lead_id = ?",
        (lead["lead"],),
    )
    result = passes(repo, synthetic_run, lead["lead"])
    assert [(w["code"], "談合" in w["message"]) for w in result["warnings"]] == [
        ("W7_STRONG_WORD", True)
    ]


# ---------------------------------------------------------------------------
# CK-4（record の時点）：取り込まない列の値（D-032）
# ---------------------------------------------------------------------------


def conclusion_with_population(lead_id: str, population: str) -> dict:
    """比較の条件の母集団を変える結論の案（比較の条件は数値の検査を当てず、氏名の検査を当てる欄。D-035）。"""
    return {
        "kind": "conclusion",
        "lead_id": lead_id,
        "outcome": "change",
        "comparison_state": "comparable",
        "explanation_status": "none",
        "comparison": {"population": population},
    }


def v7_error(repo: Path, run_id: str, data: dict) -> KizashiError:
    with pytest.raises(KizashiError) as info:
        record.record(repo, run_id, put(repo, "v7.json", data))
    assert info.value.code == "E_V7"
    return info.value


@pytest.mark.parametrize(
    ("index", "old", "new", "written", "flagged"),
    [
        # 担当官の所在地（index 3）：改行のあるセルは、分けた各行を照らす
        (
            3,
            OFFICER_ADDR,
            f'"{OFFICER_ADDR}\r\nテスト合同庁舎別館"',
            "テスト合同庁舎別館の部局",
            True,
        ),
        # 相手方の住所（index 6）：セル全体と、改行と連続する空白を空白1つにした形を照らし、改行では分けない
        (6, CP_ADDR, f'"{CP_ADDR}\r\nテストビル３階"', f"{CP_ADDR} テストビル３階の受注者", True),
        (6, CP_ADDR, f'"{CP_ADDR}\r\nテストビル３階"', "テストビル３階の受注者", False),
        # `?` を含む住所は、`?` の位置を別の文字で書いた形も照らす（D-022）
        (6, CP_ADDR, "テスト県?野市緑町３－３", "テスト県大野市緑町３－３の受注者", True),
    ],
)
def test_ck4_excluded_values_at_record(
    repo: Path,
    synthetic_run: str,
    index: int,
    old: str,
    new: str,
    written: str,
    flagged: bool,
) -> None:
    """CK-4（D-032）：record の時点で、提案の文字列（比較の条件を含む）に取り込まない列の値があれば E_V7。
    台帳にも accepted/ にも残さない。見つけた語は返さない。"""
    lead = prepare(repo, synthetic_run, conclusion=False)
    old_cell = f",{old},"
    rewrite_raw_csv(repo, synthetic_run, "S005", old_cell, f",{new},")
    data = conclusion_with_population(lead["lead"], written)
    if not flagged:
        assert record.record(repo, synthetic_run, put(repo, "ok.json", data))["id"] == lead["lead"]
        return
    error = v7_error(repo, synthetic_run, data)
    assert "comparison.population" in error.message and "取り込まない列の値" in error.message
    assert written.split("の")[0] not in json.dumps(error.to_dict(), ensure_ascii=False)
    assert (repo / "inbox" / "v7.json").exists()
    conn = ledger_of(repo, synthetic_run)
    assert (
        conn.execute(
            "SELECT proposed_outcome FROM leads WHERE lead_id = ?", (lead["lead"],)
        ).fetchone()[0]
        is None
    )
    conn.close()


def test_ck4_other_dropped_columns_are_not_checked(repo: Path, synthetic_run: str) -> None:
    """CK-4（D-032）：取り込まない列のうち、担当官の所在地と相手方の住所のほかの列の値（随意契約の内部用の列の
    部局名「会計第一係」「大臣官房」）は照らさない。"""
    lead = prepare(repo, synthetic_run, conclusion=False)
    data = conclusion_with_population(lead["lead"], "大臣官房会計第一係が扱う契約")
    record.record(repo, synthetic_run, put(repo, "ok.json", data))
    passes(repo, synthetic_run, lead["lead"])


def test_ck4_excluded_value_in_claim_text_at_record(repo: Path, synthetic_run: str) -> None:
    """CK-4：主張の本文に担当官の所在地（改行のないセル全体）を書くと E_V7（数値の検査の不合格と一緒に返る）。"""
    lead = prepare(repo, synthetic_run, conclusion=False)
    error = v7_error(
        repo,
        synthetic_run,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "observation",
            "text": f"担当の部局は{OFFICER_ADDR}にある。",
            "calc_ids": [],
            "evidence": [],
        },
    )
    assert "V7_PERSONAL_NAME：text の 7〜19 文字目（取り込まない列の値）" in error.message
