"""提案の登録（test_plan.md の IN-2、CP-1（登録まで）、CA の record 経由。implementation_spec.md 4.5・6章）。

合成データの実行（S001〜S009）に、探索SQL・計算・候補を record で足して確かめる。期待値は合成の CSV
（tests/fixtures/make_cao_fixtures.py）から手で決めた（翻訳業務：令和6年度 650,000円、令和7年度 750,000円）。
実行には検出（detect）の候補 L0001〜L0016 と計算 K0009〜K0080 があるので、record で足す計算は K0081 から、
探索の候補は L0017 から（conftest.py の synthetic_run）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import COMPARISON, add_evidence, calc_sum, ledger_of, make_lead, put, rec
from kizashi import record
from kizashi.errors import KizashiError

PLAYBOOK = (
    "# テスト用の合成の知識ファイル（実在のノウハウ集ではない）\n\n### C900 テストの手順\n"
    "**応用手順**：①年度ごとに保存 ②契約金額を税込か確認 ③再照合\n"
)


def fails(root: Path, run_id: str, data: dict, code: str, name: str = "bad.json") -> KizashiError:
    arg = put(root, name, data)
    with pytest.raises(KizashiError) as info:
        record.record(root, run_id, arg)
    assert info.value.code == code, info.value.message
    assert (root / arg).exists()  # 失敗したら提案は inbox/ に残る
    return info.value


@pytest.fixture
def run(repo: Path, synthetic_run: str) -> str:
    ref = repo / "references"
    ref.mkdir()
    (ref / "investigative_reporting_playbook_v13.0.md").write_text(PLAYBOOK, encoding="utf-8")
    return synthetic_run


@pytest.fixture
def lead(repo: Path, run: str) -> dict:
    return make_lead(repo, run)


# ---------------------------------------------------------------------------
# IN-2：管理項目の自己申告、存在しない参照、fiscal_year のない「原資料の記載」。失敗なら inbox/ に残る
# ---------------------------------------------------------------------------


def test_in2_success_moves_to_accepted(repo: Path, run: str) -> None:
    """IN-2：成功したら accepted/<連番>-<kind>.json に移り、events に record が残る。値は手計算と同じ。"""
    result = rec(repo, run, calc_sum("翻訳業務", 2024))
    assert (result["kind"], result["id"], result["value"]) == ("calc", "K0081", "650000")
    assert not (repo / "inbox" / "p.json").exists()
    accepted = repo / "data" / "runs" / run / "accepted" / "0002-calc.json"
    assert accepted.is_file() and result["accepted"] == f"data/runs/{run}/accepted/0002-calc.json"
    conn = ledger_of(repo, run)
    ev = conn.execute("SELECT * FROM events WHERE kind = 'record'").fetchall()
    assert [e["target_id"] for e in ev] == ["K0081"]
    assert json.loads(ev[0]["after_json"])["kind"] == "calc"


@pytest.mark.parametrize(
    "extra",
    [
        {"value": "650000"},
        {"state": "active"},
        {"checked": True},
        {"created_at": "2026-11-05T10:00:00+09:00"},
        {"row_refs": ["S001:000012"]},
        {"origin": "run"},
    ],
)
def test_in2_self_declared_management_fields(repo: Path, run: str, extra: dict) -> None:
    """IN-2：管理項目（値、状態、checked、日時、row_refs、origin など）が提案にあれば E_SELF_DECLARED。"""
    fails(repo, run, calc_sum("翻訳業務", 2024, **extra), "E_SELF_DECLARED")


def test_in2_self_declared_in_other_kinds(repo: Path, run: str, lead: dict) -> None:
    """IN-2：主張の state、候補の巡回数・メモの番号、結論の state も E_SELF_DECLARED。"""
    claim = {
        "kind": "claim",
        "lead_id": lead["lead"],
        "type": "observation",
        "text": "観察。",
        "calc_ids": [],
        "evidence": [],
        "state": "checked",
    }
    fails(repo, run, claim, "E_SELF_DECLARED")
    concl = {
        "kind": "conclusion",
        "lead_id": lead["lead"],
        "outcome": "hold",
        "comparison_state": "unresolved",
        "hold_reason": "comparison",
        "rounds": 0,
    }
    fails(repo, run, concl, "E_SELF_DECLARED")
    hyp = {
        "kind": "hypothesis",
        "lead_id": lead["lead"],
        "text": "仮説。",
        "withdraw_if": "条件。",
        "memo_no": 1,
    }
    fails(repo, run, hyp, "E_SELF_DECLARED")


def test_in2_missing_references(repo: Path, run: str, lead: dict) -> None:
    """IN-2：存在しない候補・計算・レコード・資料・主張への参照は E_REF_NOT_FOUND。"""
    base = {
        "kind": "claim",
        "type": "observation",
        "text": "観察。",
        "calc_ids": [],
        "evidence": [],
    }
    fails(repo, run, {**base, "lead_id": "L0099"}, "E_REF_NOT_FOUND")
    fails(repo, run, {**base, "lead_id": lead["lead"], "calc_ids": ["K0999"]}, "E_REF_NOT_FOUND")
    fails(
        repo,
        run,
        {**base, "lead_id": lead["lead"], "text": "{calc:K0999}に増えた。"},
        "E_REF_NOT_FOUND",
    )
    match = {
        "kind": "match",
        "lead_id": lead["lead"],
        "left": ["S001:000099"],
        "right": ["S005:000007"],
        "relation": "1:1",
        "reason": "同じ業務。",
        "evidence": [],
        "status": "pending",
    }
    fails(repo, run, match, "E_REF_NOT_FOUND")
    fails(repo, run, {"kind": "withdraw", "target": "C0099", "reason": "誤り。"}, "E_REF_NOT_FOUND")
    calc = calc_sum("翻訳業務", 2024, lead_id="L0099")
    fails(repo, run, calc, "E_REF_NOT_FOUND")


def test_in2_record_claim_needs_fiscal_year(repo: Path, run: str, lead: dict) -> None:
    """IN-2：type: record の主張で fiscal_year がなければ E_SCHEMA。あれば登録できる（draft）。"""
    ev = [
        {
            "source_id": "S005",
            "position": {"row_ref": "S005:000007", "column": "method_raw"},
            "quote": "一般競争",
            "role": "support",
        }
    ]
    claim = {
        "kind": "claim",
        "lead_id": lead["lead"],
        "type": "record",
        "text": "令和7年度の公表資料では、一般競争入札として記載されている。",
        "calc_ids": [],
        "evidence": ev,
    }
    fails(repo, run, claim, "E_SCHEMA")
    result = rec(repo, run, {**claim, "fiscal_year": 2025})
    assert result["id"] == "C0001"
    row = ledger_of(repo, run).execute("SELECT * FROM claims").fetchone()
    assert (row["state"], row["type"], row["fiscal_year"]) == ("draft", "record", 2025)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({"kind": "memo"}, "E_UNKNOWN_KIND"),
        ({"kind": "run_config", "domain": "cao-procurement"}, "E_UNKNOWN_KIND"),
        ({**calc_sum("翻訳業務", 2024), "colour": "red"}, "E_SCHEMA"),
        ({k: v for k, v in calc_sum("翻訳業務", 2024).items() if k != "label"}, "E_SCHEMA"),
        ({**calc_sum("翻訳業務", 2024), "label": ""}, "E_SCHEMA"),
        ({**calc_sum("翻訳業務", 2024), "lead_id": "L1"}, "E_SCHEMA"),
    ],
)
def test_in2_unknown_kind_and_shape(repo: Path, run: str, data: dict, code: str) -> None:
    """IN-2：知らない kind（record は run_config を受け付けない）、知らない項目、必須の欠け、空の文字列、ID の書式。"""
    fails(repo, run, data, code)


def test_record_v7_numbers_names_and_strong_words(repo: Path, run: str, lead: dict) -> None:
    """4.5：Claude の書く文字列に V7 の数値と氏名の検査をする（不合格は E_V7）。強い語は警告。"""
    base = {
        "kind": "claim",
        "lead_id": lead["lead"],
        "type": "observation",
        "calc_ids": [lead["to"]],
        "evidence": [],
    }
    err = fails(repo, run, {**base, "text": "合計は750,000円に増えた。"}, "E_V7")
    assert "V7_RAW_NUMBER" in err.message and "750,000" in err.message
    err = fails(repo, run, {**base, "text": "担当官は架空　太郎である。"}, "E_V7")
    assert "V7_PERSONAL_NAME" in err.message
    assert "架空" not in err.message and "太郎" not in err.message  # 氏名をエラーに書き出さない
    result = rec(
        repo, run, {**base, "text": f"合計は{{calc:{lead['to']}}}になり、疑惑の声がある。"}
    )
    [warning] = result["warnings"]
    assert set(warning) == {"code", "message", "affected_ids"}  # 3.2 の警告の形
    assert (warning["code"], warning["affected_ids"]) == ("W7_STRONG_WORD", [result["id"]])
    assert "疑惑" in warning["message"] and "text" in warning["message"]


# ---------------------------------------------------------------------------
# 種類ごとの登録
# ---------------------------------------------------------------------------


def test_lead_from_exploration(repo: Path, run: str, lead: dict) -> None:
    """6.2 lead：screen_id='explore' で登録し、計算の lead_id を埋め、比べた組の数（探索）を1増やす。"""
    conn = ledger_of(repo, run)
    row = conn.execute("SELECT * FROM leads WHERE screen_id = 'explore'").fetchone()
    assert (row["lead_id"], row["screen_id"], row["query_file"], row["state"]) == (
        "L0017",
        "explore",
        "q-001.sql",
        "candidate",
    )
    assert (row["calc_from"], row["calc_to"], row["period_from"], row["period_to"]) == (
        lead["from"],
        lead["to"],
        "2024",
        "2025",
    )
    assert json.loads(row["comparison_json"]) == COMPARISON
    owners = dict(conn.execute("SELECT calc_id, lead_id FROM computations WHERE origin = 'record'"))
    assert owners == {lead["from"]: "L0017", lead["to"]: "L0017"}
    counts = json.loads(conn.execute("SELECT counts_json FROM runs").fetchone()[0])
    assert counts["explore"] == {"compared": 1}


def test_lead_requires_base_calcs_with_year(repo: Path, run: str, lead: dict) -> None:
    """6.2 lead：calc_from・calc_to が基礎集計でない、条件に fiscal_year がない、探索SQLがない、同じ候補がある。"""
    k_from = rec(repo, run, calc_sum("研修会場借上げ", 2024))["id"]
    k_to = rec(repo, run, calc_sum("研修会場借上げ", 2025))["id"]
    k_any = rec(
        repo,
        run,
        {
            **calc_sum("研修会場借上げ", 2024),
            "filter": [{"col": "group_key", "op": "=", "value": "研修会場借上げ"}],
        },
    )["id"]
    d = rec(
        repo,
        run,
        {"kind": "derive", "op": "diff", "args": [k_to, k_from], "label": "差額", "unit": "円"},
    )["id"]
    proposal = {
        "kind": "lead",
        "query_file": "q-001.sql",
        "lead_key": "研修会場借上げ",
        "change_category": "amount_up",
        "period_from": 2024,
        "period_to": 2025,
        "calc_from": k_from,
        "calc_to": k_to,
        "calc_ids": [],
        "comparison": COMPARISON,
        "note": "探索",
    }
    fails(repo, run, {**proposal, "calc_from": d}, "E_SCHEMA")
    fails(repo, run, {**proposal, "calc_from": k_any}, "E_SCHEMA")
    fails(repo, run, {**proposal, "calc_from": k_to, "calc_to": k_from}, "E_SCHEMA")
    fails(repo, run, {**proposal, "query_file": "q-999.sql"}, "E_REF_NOT_FOUND")
    fails(repo, run, {**proposal, "change_category": "something_new"}, "E_SCHEMA")
    fails(repo, run, {**proposal, "calc_from": lead["from"]}, "E_SCHEMA")  # 別の候補の計算
    assert rec(repo, run, proposal)["id"] == "L0018"
    # 同じ (screen_id, lead_key, change_category, period_from, period_to) の候補は作らない
    k_from2 = rec(repo, run, calc_sum("研修会場借上げ", 2024))["id"]
    k_to2 = rec(repo, run, calc_sum("研修会場借上げ", 2025))["id"]
    fails(repo, run, {**proposal, "calc_from": k_from2, "calc_to": k_to2}, "E_SCHEMA")


def test_hypothesis_sets_investigating_and_limits_leads(repo: Path, run: str, lead: dict) -> None:
    """6.2 hypothesis：最初の登録で investigating にする。調べる候補が3件を超えると E_LIMIT_LEADS。"""
    hyp = {
        "kind": "hypothesis",
        "text": "業務の範囲が広がった可能性がある。",
        "withdraw_if": "仕様書で範囲が同じと分かった場合。",
    }
    rec(repo, run, {**hyp, "lead_id": lead["lead"]})
    rec(repo, run, {**hyp, "lead_id": lead["lead"], "result": "kept"})  # 結果を書く（上書き）
    others = [
        make_lead(repo, run, g, f"q-{i}.sql")["lead"]
        for i, g in enumerate(("研修会場借上げ", "庁舎警備業務", "統計データ作成業務"), start=2)
    ]
    rec(repo, run, {**hyp, "lead_id": others[0]})
    rec(repo, run, {**hyp, "lead_id": others[1]})
    fails(repo, run, {**hyp, "lead_id": others[2]}, "E_LIMIT_LEADS")
    conn = ledger_of(repo, run)
    states = dict(conn.execute("SELECT lead_id, state FROM leads WHERE screen_id = 'explore'"))
    assert states == {
        "L0017": "investigating",
        "L0018": "investigating",
        "L0019": "investigating",
        "L0020": "candidate",
    }
    row = conn.execute(
        "SELECT hypothesis_result, withdraw_if FROM leads WHERE lead_id = 'L0017'"
    ).fetchone()
    assert row["hypothesis_result"] == "kept"


def test_explanation_rejects_t6a_t6b(repo: Path, run: str, lead: dict) -> None:
    """6.2 explanation：T6a・T6b は CLI が照合の計算から登録するので、提案では受け付けない。"""
    for t in ("T6a", "T6b"):
        fails(
            repo,
            run,
            {
                "kind": "explanation",
                "lead_id": lead["lead"],
                "type": t,
                "judgment": "refute",
                "evidence": [],
                "searched": "照合の結果",
            },
            "E_SELF_DECLARED",
        )


def test_explanation_support_needs_scope(repo: Path, run: str, lead: dict) -> None:
    """6.2 explanation：支持の T1・T3・T4 は scope と、recalc_calc_id か no_quant_reason が要る。上書きできる。"""
    base = {
        "kind": "explanation",
        "lead_id": lead["lead"],
        "type": "T3",
        "judgment": "support",
        "evidence": [],
        "searched": "令和6・7年度の仕様書",
    }
    fails(repo, run, base, "E_SCHEMA")
    fails(repo, run, {**base, "scope": "part"}, "E_SCHEMA")
    rec(repo, run, {**base, "scope": "part", "no_quant_reason": "数量の記載がない。"})
    rec(repo, run, {**base, "judgment": "unknown"})
    conn = ledger_of(repo, run)
    row = conn.execute("SELECT * FROM explanations").fetchone()
    assert (row["type"], row["judgment"], row["context"]) == ("T3", "unknown", "main_session")
    before = [
        json.loads(e[0])
        for e in conn.execute(
            "SELECT before_json FROM events WHERE kind = 'record' AND before_json IS NOT NULL"
        )
    ]
    assert before[-1]["judgment"] == "support"  # 前の値は events に残る


def test_match_question_conclusion_withdraw(repo: Path, run: str, lead: dict) -> None:
    """6.2：match（対応表）、question（questions_json に足す）、conclusion（結論の案）、withdraw。"""
    m = rec(
        repo,
        run,
        {
            "kind": "match",
            "lead_id": lead["lead"],
            "left": ["S001:000012"],
            "right": ["S005:000007"],
            "relation": "1:1",
            "reason": "同じ翻訳業務。",
            "evidence": [],
            "status": "confirmed",
        },
    )
    assert m["id"] == "M001"
    q = {
        "kind": "question",
        "lead_id": lead["lead"],
        "to": "所管部署",
        "question": "翻訳の量は変わったか。",
        "documents": "令和6・7年度の仕様書",
        "decision_impact": "量の変化で説明できれば説明の状況を改める。",
        "purposes": ["reason_breakdown"],  # 質問の目的（D-041⑩）
    }
    rec(repo, run, q)
    rec(repo, run, {**q, "question": "単価は変わったか。", "foia": "契約書"})
    fails(
        repo,
        run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "change",
            "comparison_state": "comparable",
        },
        "E_SCHEMA",
    )  # 説明の状況がない
    fails(
        repo,
        run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "unresolved",
        },
        "E_SCHEMA",
    )  # 保留の理由がない
    rec(
        repo,
        run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "unresolved",
            "hold_reason": "comparison",
            "reopen_condition": "仕様書が公表された場合。",
            "comparison": {"unit": "円（単価契約を含む）"},
        },
    )
    conn = ledger_of(repo, run)
    row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead["lead"],)).fetchone()
    assert [x["question"] for x in json.loads(row["questions_json"])] == [
        "翻訳の量は変わったか。",
        "単価は変わったか。",
    ]
    assert (row["proposed_outcome"], row["hold_reason"], row["state"]) == (
        "hold",
        "comparison",
        "candidate",
    )
    assert json.loads(row["comparison_json"])["unit"] == "円（単価契約を含む）"
    assert json.loads(row["comparison_json"])["row_meaning"] == "1件の契約"

    c = rec(
        repo,
        run,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "observation",
            "text": f"令和7年度は{{calc:{lead['to']}}}である。",
            "calc_ids": [lead["to"]],
            "evidence": [],
        },
    )["id"]
    rec(repo, run, {"kind": "withdraw", "target": lead["to"], "reason": "条件を誤った。"})
    assert (
        conn.execute("SELECT state FROM claims WHERE claim_id = ?", (c,)).fetchone()[0] == "stale"
    )
    assert (
        conn.execute("SELECT state FROM computations WHERE calc_id = ?", (lead["to"],)).fetchone()[
            0
        ]
        == "withdrawn"
    )
    rec(repo, run, {"kind": "withdraw", "target": "M001", "reason": "対応を誤った。"})
    assert conn.execute("SELECT status FROM procurement_matches").fetchone()[0] == "withdrawn"
    fails(repo, run, {"kind": "withdraw", "target": "K0001", "reason": "照合の計算。"}, "E_SCHEMA")
    fails(repo, run, {"kind": "withdraw", "target": "M001", "reason": "二度目。"}, "E_SCHEMA")


def test_doc_value_and_derive_by_record(repo: Path, run: str, lead: dict) -> None:
    """CA-2・CA-3（record 経由）：文書値は引用を照合し、数値の並びと値を照らす。派生計算は計算IDだけ。"""
    sid = add_evidence(repo, run, ["配送予定数量は年間1,200件とする。\n"])
    dv = {
        "kind": "doc_value",
        "lead_id": lead["lead"],
        "source_id": sid,
        "position": {"page": 1},
        "quote": "配送予定数量は年間1,200件とする。",
        "label": "仕様書の年間配送数量",
        "unit": "件",
    }
    fails(repo, run, {**dv, "value": "200"}, "E_DOC_VALUE_MISMATCH")
    fails(
        repo,
        run,
        {**dv, "quote": "配送予定数量は年間1,300件とする。", "value": "1300"},
        "E_QUOTE_NOT_FOUND",
    )
    k = rec(repo, run, {**dv, "value": "1200"})
    assert k["value"] == "1200"
    d = rec(
        repo,
        run,
        {
            "kind": "derive",
            "lead_id": lead["lead"],
            "op": "rate",
            "args": [lead["to"], lead["from"]],
            "label": "前年度からの増減率",
            "unit": "比率",
        },
    )
    assert d["value"] == "0.1538461538461538461538461538"
    fails(
        repo,
        run,
        {"kind": "derive", "op": "diff", "args": [lead["to"], 650000], "label": "差", "unit": "円"},
        "E_BAD_EXPR",
    )


# ---------------------------------------------------------------------------
# CP-1（登録まで）：係数の提案
# ---------------------------------------------------------------------------


def coefficient(lead_id: str, **over) -> dict:
    return {
        "kind": "coefficient",
        "lead_id": lead_id,
        "key": "tax_basis",
        "value": "1.1",
        "meaning": "税抜きの額を税込みに換算する係数",
        "why_needed": "令和6年度の公表は税抜き、令和7年度は税込みと注記されており、そのままでは比べられない",
        "apply_to": "令和6年度の合計に掛けて、令和7年度の合計と比べる",
        "basis": [{"file": "playbook", "locator": "C900", "quote": "②契約金額を税込か確認"}],
        "status": "proposed",
        **over,
    }


def test_cp1_coefficient_is_recorded_not_used(repo: Path, run: str, lead: dict) -> None:
    """CP-1：coefficient を登録できる。同じ key は上書きし、前の値は events に残る。計算は作らない。"""
    conn = ledger_of(repo, run)
    n_calcs = conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0]
    rec(repo, run, coefficient(lead["lead"]))
    fails(
        repo, run, coefficient(lead["lead"], status="accepted"), "E_SCHEMA"
    )  # 記者の判断の要約がない
    rec(
        repo,
        run,
        coefficient(
            lead["lead"],
            status="declined",
            reporter_note="記者は税込みの注記を確かめ、不要と判断した。",
        ),
    )
    coefs = json.loads(
        conn.execute(
            "SELECT coefficients_json FROM leads WHERE lead_id = ?", (lead["lead"],)
        ).fetchone()[0]
    )
    assert [(c["key"], c["status"], c["value"]) for c in coefs] == [
        ("tax_basis", "declined", "1.1")
    ]
    befores = [
        json.loads(r[0])
        for r in conn.execute(
            "SELECT before_json FROM events WHERE kind = 'record' AND before_json IS NOT NULL"
        )
    ]
    assert befores[-1]["status"] == "proposed"
    assert conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0] == n_calcs
    # 係数は計算の引数にできない（計算ID以外の引数）
    fails(
        repo,
        run,
        {
            "kind": "derive",
            "op": "product",
            "args": [lead["from"], "tax_basis"],
            "label": "換算",
            "unit": "円",
        },
        "E_BAD_EXPR",
    )


def test_cp1_knowledge_quote_must_exist(repo: Path, run: str, lead: dict) -> None:
    """CP-1：知識ファイルにない引用の手がかりは E_QUOTE_NOT_FOUND。手がかりなしは空の配列。"""
    bad = coefficient(
        lead["lead"], basis=[{"file": "playbook", "locator": "C900", "quote": "税抜きで比べる"}]
    )
    fails(repo, run, bad, "E_QUOTE_NOT_FOUND")
    rec(
        repo,
        run,
        coefficient(lead["lead"], basis=[], why_needed="手がかりはないが、税込みかが分からない"),
    )


def test_cp1_why_needed_raw_number_is_e_v7(repo: Path, run: str, lead: dict) -> None:
    """CP-1：why_needed・apply_to・reporter_note に生の数値があれば E_V7（係数の値は value にだけ書く）。"""
    fails(repo, run, coefficient(lead["lead"], why_needed="税率10%の差がある"), "E_V7")
    fails(repo, run, coefficient(lead["lead"], apply_to="1.1を掛ける"), "E_V7")
    fails(repo, run, coefficient(lead["lead"], value="1.1倍"), "E_SCHEMA")


# ---------------------------------------------------------------------------
# V7 の氏名の検査を、数値の検査を当てない欄にも当てる（D-035。CK-4 の record の時点）
# ---------------------------------------------------------------------------

NAME = "架空　太郎"  # 合成の担当官の氏名（make_cao_fixtures.py）


def _lead_proposal(repo: Path, run: str, **over) -> dict:
    k_from = rec(repo, run, calc_sum("翻訳業務", 2024), name="kf.json")["id"]
    k_to = rec(repo, run, calc_sum("翻訳業務", 2025), name="kt.json")["id"]
    return {
        "kind": "lead",
        "query_file": "q-001.sql",
        "lead_key": "翻訳業務（再）",
        "change_category": "amount_up",
        "period_from": 2024,
        "period_to": 2025,
        "calc_from": k_from,
        "calc_to": k_to,
        "calc_ids": [],
        "comparison": COMPARISON,
        "note": "探索で見つけた",
        **over,
    }


@pytest.mark.parametrize("key", ["population", "row_meaning", "entity_mapping", "formula"])
def test_ck4_name_in_comparison_is_e_v7(repo: Path, run: str, lead: dict, key: str) -> None:
    """D-035：比較の条件に氏名があれば E_V7（V7_PERSONAL_NAME）。数値の検査は当てない（「1件の契約」は通る）。"""
    comparison = {**COMPARISON, key: f"{NAME}が担当した契約"}
    err = fails(repo, run, _lead_proposal(repo, run, comparison=comparison), "E_V7")
    assert "V7_PERSONAL_NAME" in err.message and f"comparison.{key}" in err.message
    assert "架空" not in err.message and "太郎" not in err.message
    explore = "SELECT COUNT(*) FROM leads WHERE screen_id = 'explore'"
    assert ledger_of(repo, run).execute(explore).fetchone()[0] == 1


def test_ck4_name_in_lead_key_unit_and_coefficient_key_is_e_v7(
    repo: Path, run: str, lead: dict
) -> None:
    """D-035：候補の lead_key、計算・文書値の unit、係数の key に氏名があれば E_V7。数字は書いてよい。"""
    err = fails(repo, run, _lead_proposal(repo, run, lead_key=f"{NAME}の業務"), "E_V7")
    assert "lead_key" in err.message
    err = fails(repo, run, calc_sum("翻訳業務", 2024, unit=NAME), "E_V7")
    assert "unit" in err.message
    err = fails(repo, run, coefficient(lead["lead"], key=NAME), "E_V7")
    assert "key" in err.message
    # 数値の検査はこれまでどおり当てない
    assert (
        rec(repo, run, calc_sum("翻訳業務", 2024, unit="1件あたりの円"))["unit"] == "1件あたりの円"
    )
    rec(repo, run, _lead_proposal(repo, run, lead_key="翻訳業務2"))


def test_ck4_name_anywhere_in_the_proposal_is_e_v7(repo: Path, run: str, lead: dict) -> None:
    """D-036：氏名の検査は提案の中のすべての文字列（入れ子を含む）に当てる。結論での比較の条件の変更、
    根拠の引用、集計の条件の値、係数の手がかりの引用でも E_V7。"""
    conclusion = {
        "kind": "conclusion",
        "lead_id": lead["lead"],
        "outcome": "hold",
        "comparison_state": "unresolved",
        "hold_reason": "comparison",
        "reopen_condition": "同種業務の対応を確かめたら再開する",
    }
    err = fails(repo, run, {**conclusion, "comparison": {"population": f"{NAME}の契約"}}, "E_V7")
    assert "comparison.population" in err.message and "架空" not in err.message
    row = (
        ledger_of(repo, run)
        .execute("SELECT comparison_json FROM leads WHERE lead_id = ?", (lead["lead"],))
        .fetchone()[0]
    )
    assert "架空" not in row

    sid = add_evidence(repo, run, [f"契約担当官は{NAME}とする。\n"])
    claim = {
        "kind": "claim",
        "lead_id": lead["lead"],
        "type": "observation",
        "text": "公告に契約担当官の記載がある。",
        "calc_ids": [],
        "evidence": [
            {
                "source_id": sid,
                "position": {"page": 1},
                "quote": f"契約担当官は{NAME}とする。",
                "role": "background",
            }
        ],
    }
    err = fails(repo, run, claim, "E_V7")
    assert "evidence[0].quote" in err.message

    request = calc_sum("翻訳業務", 2024)
    request["filter"].append({"col": "group_key", "op": "!=", "value": NAME})
    err = fails(repo, run, request, "E_V7")
    assert "filter[2].value" in err.message

    playbook = repo / "references" / "investigative_reporting_playbook_v13.0.md"
    playbook.write_text(PLAYBOOK + f"担当の{NAME}が確かめた。\n", encoding="utf-8")
    basis = [{"file": "playbook", "locator": "C900", "quote": f"担当の{NAME}が確かめた。"}]
    err = fails(repo, run, coefficient(lead["lead"], basis=basis), "E_V7")
    assert "basis[0].quote" in err.message
    assert ledger_of(repo, run).execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_strong_words_in_comparison_warn(repo: Path, run: str, lead: dict) -> None:
    """D-036：比較の条件（候補の登録と、結論での変更）の強い語は警告する。不合格にはしない。"""
    result = rec(
        repo,
        run,
        _lead_proposal(repo, run, comparison={**COMPARISON, "population": "談合が疑われる契約"}),
    )
    [warning] = result["warnings"]
    assert warning["code"] == "W7_STRONG_WORD" and "comparison.population" in warning["message"]
    result = rec(
        repo,
        run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "unresolved",
            "hold_reason": "comparison",
            "reopen_condition": "同種業務の対応を確かめたら再開する",
            "comparison": {"formula": "異例の増加を除いた合計の差"},
        },
        name="c.json",
    )
    [warning] = result["warnings"]
    assert warning["code"] == "W7_STRONG_WORD" and "comparison.formula" in warning["message"]


# ---------------------------------------------------------------------------
# 回帰（D-087）：一覧表のページの長い引用は、ページを evidence の position.page に置き、本文には
# 資料ID・契約件名・判定を支える箇所の説明を書く（investigate の Skill の案内。D-086）。本文に
# ページの番号を書くと V7_RAW_NUMBER（位置の番号は例外にしない。D-051）
# ---------------------------------------------------------------------------

TABLE_PAGE = (
    "番号 件名 契約担当官等 契約日 契約金額 契約方式\n"
    "1 架空の資料作成業務 （氏名） 令和7年4月1日 1,000,000 随意契約（性質）\n"
    "2 見本の調査業務 （氏名） 令和7年5月1日 2,000,000 一般競争\n"
    "令和８年５月下旬頃公表予定"
)


def test_d087_table_quote_guidance_can_be_recorded(repo: Path, run: str, lead: dict) -> None:
    """IN-2・KN-3 回帰（D-087）：案内どおりの主張と判定は登録できる。本文に「3ページ」と書くと E_V7。"""
    source = add_evidence(repo, run, ["表紙。", "目次。", TABLE_PAGE])
    evidence = [
        {"source_id": source, "position": {"page": 3}, "quote": TABLE_PAGE, "role": "support"}
    ]
    guided = (
        f"{source} の一覧表で、件名が「架空の資料作成業務」の行の契約方式の欄が、随意契約（性質）と"
        "記載している（引用のうちこの行の方式の欄が判定を支える）。"
    )
    claim = {
        "kind": "claim",
        "lead_id": lead["lead"],
        "type": "publisher_explanation",
        "text": guided,
        "calc_ids": [],
        "evidence": evidence,
    }
    explanation = {
        "kind": "explanation",
        "lead_id": lead["lead"],
        "type": "T2",
        "judgment": "refute",
        "evidence": evidence,
        "searched": guided,
    }
    rec(repo, run, claim, name="claim.json")
    rec(repo, run, explanation, name="explanation.json")

    with_page = f"{source} の3ページの「架空の資料作成業務」の行が根拠である。"
    for data in ({**claim, "text": with_page}, {**explanation, "searched": with_page}):
        err = fails(repo, run, data, "E_V7")
        assert "V7_RAW_NUMBER" in err.message and "3" in err.message
