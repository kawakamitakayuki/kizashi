"""検証（kizashi check。test_plan.md の CK-1（V1〜V4・V8）、CK-2、CK-5、CP-1（V8）。implementation_spec.md
4.6・9章）。

合成データの実行の検出の候補「翻訳業務」（S1・amount_up）を使う。期待値は合成の CSV（tests/fixtures/
make_cao_fixtures.py）から手で決めた：令和6年度は S001:000012（650,000円。契約日 R7.3.31。PDF の3ページ）、
令和7年度は S005:000007（750,000円。契約日 R7.6.1。PDF の2ページ）。どちらも取り込みの照合と原本照合で一致する。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import ledger_of, rec
from kizashi import check, cli, hashing
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

GROUP = "翻訳業務"
ROW_FROM, ROW_TO = "S001:000012", "S005:000007"
SEARCHED = "公表ページと公表元の告知を探したが、該当する記載は見つからなかった。"


def lead_row(repo: Path, run_id: str, group: str = GROUP) -> sqlite3.Row:
    conn = ledger_of(repo, run_id)
    row = conn.execute(
        "SELECT * FROM leads WHERE screen_id = 's1_amount_change' AND lead_key = ?", (group,)
    ).fetchone()
    conn.close()
    return row


def explanation(lead_id: str, etype: str, judgment: str, **extra) -> dict:
    return {
        "kind": "explanation",
        "lead_id": lead_id,
        "type": etype,
        "judgment": judgment,
        "evidence": [],
        "searched": SEARCHED,
        **extra,
    }


T2_EVIDENCE = {
    "source_id": "S001",
    "position": {"row_ref": ROW_FROM, "column": "contract_date_raw"},
    "quote": "R7.3.31",
    "role": "background",
}


def prepare(
    repo: Path,
    run_id: str,
    *,
    conclusion: dict | None = None,
    match: str = "full",
    lead: sqlite3.Row | None = None,
    purposes: tuple[str, ...] = ("verify_record", "missing_documents"),
) -> dict:
    """比較成立の案の必須の中身をそろえた候補（conclusion を False にすると結論の案を登録しない）。
    lead を渡さなければ、検出の候補の「翻訳業務」を使う。"""
    lead = lead if lead is not None else lead_row(repo, run_id)
    lid = lead["lead_id"]
    hypothesis = {
        "kind": "hypothesis",
        "lead_id": lid,
        "text": "翻訳の分量が増えたため、契約金額が増えた。",
        "withdraw_if": "仕様書で分量が同じと分かった場合。",
    }
    rec(repo, run_id, hypothesis)
    if match == "full":
        rec(
            repo,
            run_id,
            {
                "kind": "match",
                "lead_id": lid,
                "left": [ROW_FROM],
                "right": [ROW_TO],
                "relation": "1:1",
                "reason": "件名と業務の内容が同じ。",
                "evidence": [],
                "status": "confirmed",
            },
        )
    observation = rec(
        repo,
        run_id,
        {
            "kind": "claim",
            "lead_id": lid,
            "type": "observation",
            "text": f"契約金額の合計は{{calc:{lead['calc_from']}}}から{{calc:{lead['calc_to']}}}に増えた。",
            "calc_ids": [lead["calc_from"], lead["calc_to"]],
            "evidence": [],
        },
    )["id"]
    record_claim = rec(
        repo,
        run_id,
        {
            "kind": "claim",
            "lead_id": lid,
            "type": "record",
            "fiscal_year": 2025,
            "text": "令和7年度の公表データの契約金額の欄に、翌年度の金額が記載されている。",
            "calc_ids": [],
            "evidence": [
                {
                    "source_id": "S005",
                    "position": {"row_ref": ROW_TO, "column": "amount_raw"},
                    "quote": "750,000",
                    "role": "support",
                }
            ],
        },
    )["id"]
    for etype in ("T1", "T3", "T4", "T5", "T6c"):
        rec(repo, run_id, explanation(lid, etype, "unknown"))
    rec(repo, run_id, explanation(lid, "T2", "refute", evidence=[T2_EVIDENCE], searched=""))
    rec(
        repo,
        run_id,
        {
            "kind": "question",
            "lead_id": lid,
            "to": "内閣府の会計担当",
            "question": "翻訳の分量と単価を示す仕様書はあるか。公表データの契約金額は契約書と同じか。",
            "documents": "仕様書、契約書",
            "decision_impact": "分量が増えていれば、金額の増加の説明になる。",
            "purposes": list(purposes),
        },
    )
    rec(repo, run_id, {**hypothesis, "result": "kept"})
    if conclusion is not False:
        rec(
            repo,
            run_id,
            conclusion
            or {
                "kind": "conclusion",
                "lead_id": lid,
                "outcome": "change",
                "comparison_state": "comparable",
                "explanation_status": "none",
            },
        )
    return {"lead": lid, "observation": observation, "record": record_claim, **dict(lead)}


def codes(info: pytest.ExceptionInfo) -> list[str]:
    return [f["code"] for f in info.value.failures]


def failing(repo: Path, run_id: str, lead_id: str) -> list[str]:
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, run_id, lead_id)
    assert info.value.stage == "check" and info.value.exit_code == 1
    assert info.value.code == info.value.failures[0]["code"]
    return codes(info)


def tamper(repo: Path, run_id: str, sql: str, params: tuple = ()) -> None:
    conn = ledger_of(repo, run_id)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 合格と、T6a・T6b の機械の判定
# ---------------------------------------------------------------------------


def test_check_passes_and_marks_claims_checked(repo: Path, synthetic_run: str) -> None:
    """CK-1：必須の中身をそろえた比較成立の案は合格する。メモに出る主張を checked にし、T6a・T6b を照合の
    計算から「否定」として登録し（context='cli'）、巡回数を1増やし、events に検証記録のハッシュを残す。"""
    lead = prepare(repo, synthetic_run)
    result = check.run_check(repo, synthetic_run, lead["lead"])
    assert result["passed"] is True
    assert result["t6"] == {"T6a": "refute", "T6b": "refute"}
    assert result["checked_claims"] == [lead["observation"], lead["record"]]
    assert result["rounds"] == 1
    conn = ledger_of(repo, synthetic_run)
    states = [r[0] for r in conn.execute("SELECT state FROM claims ORDER BY claim_id")]
    assert states == ["checked", "checked"]
    t6 = {
        r["type"]: r
        for r in conn.execute(
            "SELECT * FROM explanations WHERE lead_id = ? AND type IN ('T6a', 'T6b')",
            (lead["lead"],),
        )
    }
    assert {t: (r["judgment"], r["context"]) for t, r in t6.items()} == {
        "T6a": ("refute", "cli"),
        "T6b": ("refute", "cli"),
    }
    # 照合の計算：取り込みの照合 K0001（S001）・K0003（S005）、原本照合 K0005（S001）・K0007（S005）
    assert json.loads(t6["T6a"]["evidence_json"]) == [{"calc_id": "K0001"}, {"calc_id": "K0003"}]
    assert json.loads(t6["T6b"]["evidence_json"]) == [{"calc_id": "K0005"}, {"calc_id": "K0007"}]
    # 実行設定の氏名の検査（D-040）の check の events は実行IDが対象。候補の検証は候補IDが対象
    event = conn.execute(
        "SELECT * FROM events WHERE kind = 'check' AND target_id = ?", (lead["lead"],)
    ).fetchone()
    assert json.loads(event["result_json"]) == {"passed": True, "failures": []}
    assert (
        event["check_hash"]
        == hashing.check_hash(conn, lead["lead"], load_adapter(repo, "cao-procurement"))
        == result["check_hash"]
    )
    conn.close()


def test_candidate_rows_are_the_memo_base_rows(repo: Path, synthetic_run: str) -> None:
    """9章：候補の行は、メモに出る基礎集計の row_refs（比べる2年度の合計と件数）。"""
    lead = prepare(repo, synthetic_run)
    conn = ledger_of(repo, synthetic_run)
    assert check.candidate_rows(conn, lead["lead"]) == [ROW_FROM, ROW_TO]
    conn.close()


def test_ck2_machine_t6a_t6b_override_and_fail_change(repo: Path, synthetic_run: str) -> None:
    """CK-2：T6a・T6b は check のたびに照合の計算から機械で決める。取り込みの照合で候補の行が不一致なら T6a は
    「支持」、原本照合で項目が不一致なら T6b は「支持」になり、比較成立の案は V8_CHANGE_NOT_NEGATED。"""
    lead = prepare(repo, synthetic_run)
    conn = ledger_of(repo, synthetic_run)
    (result,) = conn.execute(
        "SELECT result_json FROM computations WHERE calc_id = 'K0001'"
    ).fetchone()
    data = json.loads(result)
    data["rows"][ROW_FROM]["row_status"] = "mismatch"
    conn.execute(
        "UPDATE computations SET result_json = ? WHERE calc_id = 'K0001'", (json.dumps(data),)
    )
    (result,) = conn.execute(
        "SELECT result_json FROM computations WHERE calc_id = 'K0007'"
    ).fetchone()
    data = json.loads(result)
    data["rows"][ROW_TO]["fields"]["amount_yen"] = "mismatch"
    conn.execute(
        "UPDATE computations SET result_json = ? WHERE calc_id = 'K0007'", (json.dumps(data),)
    )
    conn.commit()
    conn.close()
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V8_CHANGE_NOT_NEGATED"]
    assert "T6a" in info.value.failures[0]["message"] and "T6b" in info.value.failures[0]["message"]
    conn = ledger_of(repo, synthetic_run)
    judged = dict(
        conn.execute(
            "SELECT type, judgment FROM explanations WHERE lead_id = ? AND context = 'cli'",
            (lead["lead"],),
        ).fetchall()
    )
    assert judged == {"T6a": "support", "T6b": "support"}
    # 不合格でも T6 の判定・巡回数・events は残る（check の記録）
    assert (
        conn.execute("SELECT rounds FROM leads WHERE lead_id = ?", (lead["lead"],)).fetchone()[0]
        == 1
    )
    assert conn.execute("SELECT state FROM claims").fetchall()[0][0] == "draft"
    conn.close()


def test_ck2_unverifiable_source_check_needs_hold(repo: Path, synthetic_run: str) -> None:
    """CK-2：原本照合で照合不能（unverifiable）の項目があれば T6b は「未確認」。比較成立は不合格、保留
    （source_check）は合格する。"""
    lead = prepare(repo, synthetic_run, conclusion=False)
    conn = ledger_of(repo, synthetic_run)
    (result,) = conn.execute(
        "SELECT result_json FROM computations WHERE calc_id = 'K0005'"
    ).fetchone()
    data = json.loads(result)
    data["rows"][ROW_FROM]["fields"]["contract_date"] = "unverifiable"
    conn.execute(
        "UPDATE computations SET result_json = ? WHERE calc_id = 'K0005'", (json.dumps(data),)
    )
    conn.commit()
    conn.close()
    base = {"kind": "conclusion", "lead_id": lead["lead"]}
    rec(
        repo,
        synthetic_run,
        {
            **base,
            "outcome": "change",
            "comparison_state": "comparable",
            "explanation_status": "none",
        },
    )
    assert failing(repo, synthetic_run, lead["lead"]) == ["V8_CHANGE_NOT_NEGATED"]
    rec(
        repo,
        synthetic_run,
        {
            **base,
            "outcome": "hold",
            "comparison_state": "comparable",
            "hold_reason": "source_check",
        },
    )
    assert check.run_check(repo, synthetic_run, lead["lead"])["t6"]["T6b"] == "unknown"


# ---------------------------------------------------------------------------
# CK-5：V8_MATCH_UNCONFIRMED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["none", "partial", "pending", "withdrawn"])
def test_ck5_match_must_cover_both_years(repo: Path, synthetic_run: str, case: str) -> None:
    """CK-5：比較成立の案で、対応がない・calc_to の行を覆わない・pending だけ・取り下げ済みだけは
    V8_MATCH_UNCONFIRMED。同じ条件で保留（comparison）の案は合格する。"""
    lead = prepare(repo, synthetic_run, match="none")
    match = {
        "kind": "match",
        "lead_id": lead["lead"],
        "left": [ROW_FROM],
        "right": [ROW_TO],
        "relation": "1:1",
        "reason": "件名が同じ。",
        "evidence": [],
        "status": "confirmed",
    }
    if case == "partial":
        rec(repo, synthetic_run, {**match, "right": [ROW_FROM]})  # 令和7年度の行を覆わない
    elif case == "pending":
        rec(repo, synthetic_run, {**match, "status": "pending"})
    elif case == "withdrawn":
        mid = rec(repo, synthetic_run, match)["id"]
        rec(
            repo,
            synthetic_run,
            {"kind": "withdraw", "target": mid, "reason": "別の業務と分かった。"},
        )
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V8_MATCH_UNCONFIRMED"]
    assert ROW_TO in info.value.failures[0]["affected_ids"]
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
    assert check.run_check(repo, synthetic_run, lead["lead"])["passed"] is True


# ---------------------------------------------------------------------------
# CP-1：V8_COEFFICIENT_PENDING
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "passes"), [("proposed", False), ("accepted", False), ("declined", True)]
)
def test_cp1_pending_coefficient_blocks_change(
    repo: Path, synthetic_run: str, status: str, passes: bool
) -> None:
    """CP-1：係数の提案が proposed・accepted のまま残る比較成立は V8_COEFFICIENT_PENDING。declined だけなら
    合格。保留は残っていても合格する。"""
    lead = prepare(repo, synthetic_run)
    coefficient = {
        "kind": "coefficient",
        "lead_id": lead["lead"],
        "key": "tax",
        "value": "1.1",
        "meaning": "税込と税抜の換算",
        "why_needed": "片方の年度が税抜かもしれない。",
        "apply_to": "両年度の合計",
        "basis": [],
        "status": status,
    }
    if status != "proposed":
        coefficient["reporter_note"] = "記者の判断の要約。"
    rec(repo, synthetic_run, coefficient)
    if passes:
        assert check.run_check(repo, synthetic_run, lead["lead"])["passed"] is True
        return
    assert failing(repo, synthetic_run, lead["lead"]) == ["V8_COEFFICIENT_PENDING"]
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
    assert check.run_check(repo, synthetic_run, lead["lead"])["passed"] is True


# ---------------------------------------------------------------------------
# CK-1：V1〜V4 の各コード（台帳か原本を手で壊す）
# ---------------------------------------------------------------------------


def _rewrite_raw_csv(repo: Path, run_id: str, source_id: str, old: bytes, new: bytes) -> None:
    """原本の CSV を書き換え、台帳の SHA-256 も合わせる（V2 の照合を通して V3(b) だけを見るため）。"""
    import hashlib

    conn = ledger_of(repo, run_id)
    (sha,) = conn.execute("SELECT sha256 FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    raw = repo / "data" / "raw" / f"{sha}.csv"
    data = raw.read_bytes()
    assert old in data
    changed = data.replace(old, new, 1)
    new_sha = hashlib.sha256(changed).hexdigest()
    (repo / "data" / "raw" / f"{new_sha}.csv").write_bytes(changed)
    conn.execute("UPDATE sources SET sha256 = ? WHERE source_id = ?", (new_sha, source_id))
    conn.commit()
    conn.close()


CK1_CASES = {
    "V1_RUN_FAILED": ("UPDATE runs SET state = 'failed'", ()),
    "V1_QUALITY_NOT_RUN": (
        "UPDATE runs SET stages_json = json_set(stages_json, '$.quality.state', 'failed')",
        (),
    ),
    "V1_INPUT_EMPTY": (
        "UPDATE runs SET counts_json = json_set(counts_json, '$.records.S005.adopted', 0)",
        (),
    ),
    "V2_REF_MISSING": (
        "UPDATE claims SET calc_ids_json = '[\"K9999\"]' WHERE type = 'observation'",
        (),
    ),
    "V2_STALE_OR_WITHDRAWN": ("UPDATE claims SET state = 'stale' WHERE type = 'observation'", ()),
    "V2_BAD_EVIDENCE_SOURCE": ("UPDATE claims SET evidence_json = '[]' WHERE type = 'record'", ()),
    "V3_UNCITED_NUMBER": (
        "UPDATE claims SET calc_ids_json = '[\"K0001\"]' WHERE type = 'observation'",
        (),
    ),
    "V4_QUOTE_NOT_FOUND": (
        (
            "UPDATE claims SET evidence_json = json_set(evidence_json, '$[0].quote', '760,000')"
            " WHERE type = 'record'"
        ),
        (),
    ),
    "V4_QUOTE_TRUNCATED": (
        (
            "UPDATE claims SET evidence_json = json_set(evidence_json, '$[0].quote', '50,000')"
            " WHERE type = 'record'"
        ),
        (),
    ),
    "V4_QUOTE_YEAR": ("UPDATE claims SET fiscal_year = 2024 WHERE type = 'record'", ()),
}


@pytest.mark.parametrize("code", sorted(CK1_CASES))
def test_ck1_v1_to_v4_codes(repo: Path, synthetic_run: str, code: str) -> None:
    """CK-1：合格する候補の台帳を1か所ずつ壊すと、9章の表のそのコードで不合格になる（ほかの不合格を伴わない）。"""
    lead = prepare(repo, synthetic_run)
    sql, params = CK1_CASES[code]
    tamper(repo, synthetic_run, sql, params)
    assert failing(repo, synthetic_run, lead["lead"]) == [code]


def test_ck1_v2_hash_mismatch(repo: Path, synthetic_run: str) -> None:
    """CK-1：候補の行の原本（data/raw の CSV）の中身が台帳の SHA-256 と違えば V2_HASH_MISMATCH。"""
    lead = prepare(repo, synthetic_run)
    conn = ledger_of(repo, synthetic_run)
    (sha,) = conn.execute("SELECT sha256 FROM sources WHERE source_id = 'S005'").fetchone()
    conn.close()
    raw = repo / "data" / "raw" / f"{sha}.csv"
    raw.write_bytes(raw.read_bytes() + b"\r\n")
    assert "V2_HASH_MISMATCH" in failing(repo, synthetic_run, lead["lead"])


def test_ck1_v3_recalc_and_derive(repo: Path, synthetic_run: str) -> None:
    """CK-1：基礎集計の値を書き換えると V3A_RECALC_MISMATCH（同じ台帳で計算し直す）。派生計算の値を書き換えると
    V3C_DERIVE_MISMATCH。意味か単位が空なら V3_NO_MEANING。"""
    lead = prepare(repo, synthetic_run)
    tamper(
        repo,
        synthetic_run,
        "UPDATE computations SET value = '650001' WHERE calc_id = ?",
        (lead["calc_from"],),
    )
    found = failing(repo, synthetic_run, lead["lead"])
    assert "V3A_RECALC_MISMATCH" in found and "V3B_REAGG_MISMATCH" in found
    tamper(
        repo,
        synthetic_run,
        "UPDATE computations SET value = '650000' WHERE calc_id = ?",
        (lead["calc_from"],),
    )
    tamper(
        repo,
        synthetic_run,
        "UPDATE computations SET value = '1' WHERE lead_id = ? AND kind = 'derived'"
        " AND json_extract(request_json, '$.op') = 'diff'",
        (lead["lead"],),
    )
    assert failing(repo, synthetic_run, lead["lead"]) == ["V3C_DERIVE_MISMATCH"]
    tamper(
        repo,
        synthetic_run,
        "UPDATE computations SET value = '100000' WHERE lead_id = ? AND kind = 'derived'"
        " AND json_extract(request_json, '$.op') = 'diff'",
        (lead["lead"],),
    )
    tamper(
        repo,
        synthetic_run,
        "UPDATE computations SET unit = '' WHERE calc_id = ?",
        (lead["calc_to"],),
    )
    assert failing(repo, synthetic_run, lead["lead"]) == ["V3_NO_MEANING"]


def test_ck1_v3b_reaggregation_from_raw(repo: Path, synthetic_run: str) -> None:
    """CK-1：原本の金額のセルを書き換えると（台帳の SHA-256 も合わせる）、別経路で数え直した合計が違い
    V3B_REAGG_MISMATCH。見出しを書き換えると V3B_HEADER_UNRESOLVED。台帳の値で計算し直す V3(a) は合格のまま。"""
    lead = prepare(repo, synthetic_run)
    target = "750,000".encode("cp932")
    _rewrite_raw_csv(repo, synthetic_run, "S005", target, "760,000".encode("cp932"))
    assert failing(repo, synthetic_run, lead["lead"]) == ["V3B_REAGG_MISMATCH"]
    _rewrite_raw_csv(repo, synthetic_run, "S005", "760,000".encode("cp932"), target)
    _rewrite_raw_csv(
        repo, synthetic_run, "S005", "契約金額".encode("cp932"), "契約の金額".encode("cp932")
    )
    assert failing(repo, synthetic_run, lead["lead"]) == ["V3B_HEADER_UNRESOLVED"]


def test_ck1_rule_version_is_exit_3(
    repo: Path, synthetic_run: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """CK-1（V2）：実行の規則の版といまの版が違えば E_RULE_VERSION（終了コード3）で、台帳を変えない。"""
    lead = prepare(repo, synthetic_run)
    yaml_path = repo / "datasets" / "cao-procurement" / "dataset.yaml"
    yaml_path.write_text(yaml_path.read_text(encoding="utf-8") + "# 変更\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["check", lead["lead"], "--run", synthetic_run]) == 3
    out = json.loads(capsys.readouterr().out)
    assert (
        out["error"]["code"] == "E_RULE_VERSION"
        and "dataset_yaml_sha256" in out["error"]["message"]
    )
    conn = ledger_of(repo, synthetic_run)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'check' AND target_id = ?", (lead["lead"],)
        ).fetchone()[0]
        == 0
    )
    conn.close()


# ---------------------------------------------------------------------------
# CK-1：V8 の各コード
# ---------------------------------------------------------------------------


def test_ck1_v8_required_and_withdraw_result(repo: Path, synthetic_run: str) -> None:
    """CK-1：結論の案がなければ V8_REQUIRED_MISSING。比較成立で取り下げ条件の結果がなければ
    V8_NO_WITHDRAW_RESULT。"""
    lead = prepare(repo, synthetic_run, conclusion=False)
    assert failing(repo, synthetic_run, lead["lead"]) == ["V8_REQUIRED_MISSING"]
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET proposed_outcome = 'change', comparison_state = 'comparable',"
        " explanation_status = 'none', hypothesis_result = NULL WHERE lead_id = ?",
        (lead["lead"],),
    )
    assert failing(repo, synthetic_run, lead["lead"]) == ["V8_NO_WITHDRAW_RESULT"]


def test_ck1_v8_questions_and_types(repo: Path, synthetic_run: str) -> None:
    """CK-1：比較成立で取材質問がなければ V8_T6C_QUESTION と V8_REQUIRED_MISSING。型の判定が欠ければ
    V8_REQUIRED_MISSING。"""
    lead = prepare(repo, synthetic_run)
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET questions_json = '[]' WHERE lead_id = ?",
        (lead["lead"],),
    )
    assert sorted(failing(repo, synthetic_run, lead["lead"])) == [
        "V8_REQUIRED_MISSING",
        "V8_T6C_QUESTION",
    ]
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET questions_json = ? WHERE lead_id = ?",
        (
            json.dumps(
                [
                    {
                        "to": "a",
                        "question": "b",
                        "documents": "c",
                        "decision_impact": "d",
                        "purposes": ["verify_record"],
                    }
                ]
            ),
            lead["lead"],
        ),
    )
    tamper(repo, synthetic_run, "DELETE FROM explanations WHERE type = 'T4'")
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V8_REQUIRED_MISSING"] and "T4" in info.value.message


def test_ck1_v8_change_not_negated_and_explanation_status(repo: Path, synthetic_run: str) -> None:
    """CK-1：比較成立で T2 が否定されていなければ V8_CHANGE_NOT_NEGATED。T3 が支持なのに説明の状況が
    「なし」なら V8_EXPLANATION_STATUS。"""
    lead = prepare(repo, synthetic_run)
    rec(repo, synthetic_run, explanation(lead["lead"], "T2", "unknown"))
    assert failing(repo, synthetic_run, lead["lead"]) == ["V8_CHANGE_NOT_NEGATED"]
    rec(
        repo,
        synthetic_run,
        explanation(lead["lead"], "T2", "refute", evidence=[T2_EVIDENCE], searched=""),
    )
    rec(
        repo,
        synthetic_run,
        explanation(
            lead["lead"],
            "T3",
            "support",
            scope="unknown",
            no_quant_reason="仕様書に分量の記載がない。",
        ),
    )
    assert failing(repo, synthetic_run, lead["lead"]) == ["V8_EXPLANATION_STATUS"]


def test_ck1_v8_artifact_and_hold(repo: Path, synthetic_run: str) -> None:
    """CK-1：見かけの変化で、T2・T5・T6a・T6b のどれも根拠付きで支持されていなければ V8_ARTIFACT_NO_FACTOR。
    T2 を根拠付きで支持すれば合格。保留で取材質問がなければ V8_HOLD_REASON。"""
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
    assert failing(repo, synthetic_run, lead["lead"]) == ["V8_ARTIFACT_NO_FACTOR"]
    rec(repo, synthetic_run, explanation(lead["lead"], "T2", "support", evidence=[T2_EVIDENCE]))
    assert check.run_check(repo, synthetic_run, lead["lead"])["passed"] is True
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
    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET questions_json = '[]' WHERE lead_id = ?",
        (lead["lead"],),
    )
    assert failing(repo, synthetic_run, lead["lead"]) == ["V8_HOLD_REASON"]


def test_check_collects_all_failures_and_counts_rounds(repo: Path, synthetic_run: str) -> None:
    """4.6：不合格をすべて集めて返す（最初の1件で止めない）。巡回数は比較成立・見かけの変化の案の check で
    1増え、保留の案では増えない。"""
    lead = prepare(repo, synthetic_run)
    tamper(repo, synthetic_run, "UPDATE runs SET state = 'failed'")
    tamper(repo, synthetic_run, "UPDATE claims SET state = 'stale' WHERE type = 'observation'")
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V1_RUN_FAILED", "V2_STALE_OR_WITHDRAWN"]
    assert info.value.code == "V1_RUN_FAILED"
    assert lead_row(repo, synthetic_run)["rounds"] == 1
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
    with pytest.raises(KizashiError):
        check.run_check(repo, synthetic_run, lead["lead"])
    assert lead_row(repo, synthetic_run)["rounds"] == 1


# ---------------------------------------------------------------------------
# D-041 の後のレビューの回帰（先に失敗するテストとして書いた）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["changed", "deleted"])
def test_d041_reconcile_pdf_is_checked_as_dependency(
    repo: Path, synthetic_run: str, how: str
) -> None:
    """V2（D-041⑥）：T6b の根拠の照合の計算が読んだ PDF（S002：令和6年度の競争入札）を書き換えるか消すと、
    V2_HASH_MISMATCH（候補の行は CSV だけだが、照合の PDF も検証が依存する資料）。"""
    lead = prepare(repo, synthetic_run)
    conn = ledger_of(repo, synthetic_run)
    (sha,) = conn.execute("SELECT sha256 FROM sources WHERE source_id = 'S002'").fetchone()
    conn.close()
    pdf = repo / "data" / "raw" / f"{sha}.pdf"
    if how == "changed":
        pdf.write_bytes(pdf.read_bytes() + b"\n%changed\n")
    else:
        pdf.unlink()
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V2_HASH_MISMATCH"]
    assert "S002" in info.value.failures[0]["affected_ids"]


def test_d041_reconcile_pdf_sha_is_in_check_hash(repo: Path, synthetic_run: str) -> None:
    """11章（D-041⑬）：照合の PDF の SHA-256 を変えると、検証記録のハッシュが変わる。"""
    lead = prepare(repo, synthetic_run)
    conn = ledger_of(repo, synthetic_run)
    before = hashing.check_hash(conn, lead["lead"], load_adapter(repo, "cao-procurement"))
    conn.execute("UPDATE sources SET sha256 = ? WHERE source_id = 'S002'", ("f" * 64,))
    assert hashing.check_hash(conn, lead["lead"], load_adapter(repo, "cao-procurement")) != before
    conn.close()


def test_d041_unchanged_recheck_keeps_check_hash(
    repo: Path, synthetic_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """11章（D-041⑬）：内容を変えずに時刻を変えて check し直しても、検証記録のハッシュは変わらない（T6 の判定の
    updated_at を変えない）。"""
    from kizashi import ledger as ledger_module

    lead = prepare(repo, synthetic_run)
    with monkeypatch.context() as m:
        m.setattr(ledger_module, "now_iso", lambda: "2026-11-06T00:00:00+09:00")
        first = check.run_check(repo, synthetic_run, lead["lead"])["check_hash"]
    with monkeypatch.context() as m:
        m.setattr(ledger_module, "now_iso", lambda: "2026-11-06T00:00:01+09:00")
        second = check.run_check(repo, synthetic_run, lead["lead"])["check_hash"]
    assert second == first


def test_d041_explore_lead_without_method_needs_all_families(repo: Path) -> None:
    """V1（D-041⑤）：令和7年度の随意契約の CSV が 404 の partial の実行で、方式を限らない探索の候補（条件は
    group_key と fiscal_year だけ）は V1_RUN_FAILED（取得できた行の方式から推し量らない）。条件で競争入札に
    限った探索の候補は、競争入札の資料だけに依存するので合格する。"""
    import httpx

    from conftest import URL, Site, calc_sum, make_lead, write_config
    from kizashi import query, runner

    result = runner.start_run(
        repo,
        "cao-procurement",
        write_config(repo),
        transport=Site({URL["z25_csv"]: httpx.Response(404)}).transport,
    )
    assert result["state"] == "partial"
    run_id = result["run_id"]
    explore = make_lead(repo, run_id)
    conn = ledger_of(repo, run_id)
    row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (explore["lead"],)).fetchone()
    conn.close()
    lead = prepare(repo, run_id, lead=row)
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, run_id, lead["lead"])
    assert codes(info) == ["V1_RUN_FAILED"]
    assert "zuikei:2025" in info.value.failures[0]["message"]

    (repo / "inbox" / "q-kyoso.sql").write_text(
        "SELECT fiscal_year, amount_yen FROM procurement_contracts"
        " WHERE group_key = '翻訳業務' AND method_family = 'kyoso'",
        encoding="utf-8",
    )
    query.run_query(repo, run_id, "inbox/q-kyoso.sql")
    kyoso = {"col": "method_family", "op": "=", "value": "kyoso"}
    k_from = rec(
        repo, run_id, calc_sum(GROUP, 2024, filter=[*calc_sum(GROUP, 2024)["filter"], kyoso])
    )["id"]
    k_to = rec(
        repo, run_id, calc_sum(GROUP, 2025, filter=[*calc_sum(GROUP, 2025)["filter"], kyoso])
    )["id"]
    from conftest import COMPARISON

    lid = rec(
        repo,
        run_id,
        {
            "kind": "lead",
            "query_file": "q-kyoso.sql",
            "lead_key": GROUP + "（競争入札）",
            "change_category": "amount_up",
            "period_from": 2024,
            "period_to": 2025,
            "calc_from": k_from,
            "calc_to": k_to,
            "calc_ids": [],
            "comparison": COMPARISON,
            "note": "競争入札に限った探索",
        },
    )["id"]
    conn = ledger_of(repo, run_id)
    row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lid,)).fetchone()
    conn.close()
    narrow = prepare(repo, run_id, lead=row)
    assert check.run_check(repo, run_id, narrow["lead"])["passed"] is True


def test_d041_withdrawn_calc_in_explanation_text(repo: Path, synthetic_run: str) -> None:
    """V2・V3（D-041③⑧）：判定の searched の {calc:ID} の計算を取り下げると V2_STALE_OR_WITHDRAWN。照合の計算を
    {calc:ID} で数値として参照すると V3_UNCITED_NUMBER。"""
    from conftest import calc_sum

    lead = prepare(repo, synthetic_run)
    cid = rec(repo, synthetic_run, calc_sum(GROUP, 2025))["id"]
    rec(
        repo,
        synthetic_run,
        explanation(
            lead["lead"], "T1", "unknown", searched=f"{{calc:{cid}}}を手がかりに告知を探した。"
        ),
    )
    assert check.run_check(repo, synthetic_run, lead["lead"])["passed"] is True
    rec(repo, synthetic_run, {"kind": "withdraw", "target": cid, "reason": "この集計は使わない。"})
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert (
        codes(info) == ["V2_STALE_OR_WITHDRAWN"] and cid in info.value.failures[0]["affected_ids"]
    )
    rec(
        repo,
        synthetic_run,
        explanation(
            lead["lead"], "T1", "unknown", searched="{calc:K0001}を手がかりに告知を探した。"
        ),
    )
    assert failing(repo, synthetic_run, lead["lead"]) == ["V3_UNCITED_NUMBER"]


def test_d041_question_purposes(repo: Path, synthetic_run: str) -> None:
    """V8（D-041⑩）：質問は目的（purposes）を持つ。目的がなければ record で E_SCHEMA。比較成立は元の記載の確認
    （verify_record）の質問がなければ V8_T6C_QUESTION（面会の日程だけの質問では通らない）。保留は不足資料の照会
    （missing_documents）の質問がなければ V8_HOLD_REASON。"""
    from conftest import put
    from kizashi import record

    lead = prepare(repo, synthetic_run, purposes=("other",))
    lid = lead["lead"]
    question = {
        "kind": "question",
        "lead_id": lid,
        "to": "内閣府の会計担当",
        "question": "取材を受けられる曜日を教えてほしい。",
        "documents": "なし",
        "decision_impact": "取材の日程を決める。",
    }
    with pytest.raises(KizashiError) as info:
        record.record(repo, synthetic_run, put(repo, "q.json", question))
    assert info.value.code == "E_SCHEMA" and "purposes" in info.value.message
    with pytest.raises(KizashiError) as info:
        record.record(
            repo, synthetic_run, put(repo, "q.json", {**question, "purposes": ["unknown_purpose"]})
        )
    assert info.value.code == "E_SCHEMA"
    rec(repo, synthetic_run, {**question, "purposes": ["other"]})
    assert failing(repo, synthetic_run, lid) == ["V8_T6C_QUESTION"]
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lid,
            "outcome": "hold",
            "comparison_state": "unresolved",
            "hold_reason": "comparison",
        },
    )
    assert failing(repo, synthetic_run, lid) == ["V8_HOLD_REASON"]
    rec(
        repo,
        synthetic_run,
        {**question, "question": "仕様書を示してほしい。", "purposes": ["missing_documents"]},
    )
    assert check.run_check(repo, synthetic_run, lid)["passed"] is True


def fetch_html_evidence(repo: Path, run_id: str, text: str) -> str:
    """追加資料（HTML）を取得の経路で台帳と data/raw に足し、資料IDを返す（通信はまねる）。"""
    import httpx

    from kizashi import fetcher

    body = f"<html><body><p>{text}</p></body></html>".encode()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=body
        )
    )
    url = "https://www.cao.go.jp/test/translation-volume.html"
    return fetcher.fetch_evidence(repo, run_id, url, "evidence", transport=transport)["source_id"]


VOLUME_TEXT = "翻訳の分量を前年度の1.15倍とした。"


def adjusted_by_volume(repo: Path, run_id: str, lead: dict) -> tuple[str, str, dict]:
    """説明を反映した値：資料の倍率（文書値 1.15）×令和6年度の合計＝747,500円（650,000×1.15。手で計算した）。
    戻り値は (説明を反映した値の計算ID, 文書値の計算ID, 根拠)。"""
    sid = fetch_html_evidence(repo, run_id, VOLUME_TEXT)
    evidence = {"source_id": sid, "position": {"page": 1}, "quote": VOLUME_TEXT, "role": "support"}
    ratio = rec(
        repo,
        run_id,
        {
            "kind": "doc_value",
            "lead_id": lead["lead"],
            **{k: evidence[k] for k in ("source_id", "position", "quote")},
            "value": "1.15",
            "label": "翻訳の分量の前年度に対する倍率（公表元の説明）",
            "unit": "倍",
        },
    )["id"]
    adjusted = rec(
        repo,
        run_id,
        {
            "kind": "derive",
            "op": "product",
            "args": [lead["calc_from"], ratio],
            "label": "分量の増加を反映した令和7年度の見込みの合計",
            "unit": "円",
            "lead_id": lead["lead"],
        },
    )
    assert adjusted["value"] == "747500.00"
    return adjusted["id"], ratio, evidence


def test_d041_explanation_needs_recalc_and_residual(repo: Path, synthetic_run: str) -> None:
    """V8（D-041⑩・D-042③）：説明の状況「全部」は、説明を反映した値が候補の比べる年度の合計そのもの
    （calc_from）なら不合格。資料の倍率で説明を反映した値（747,500円）を出し、比較対象（calc_to）との diff を残る差
    （750,000−747,500＝2,500円）にすれば合格する。範囲不明は、理由・内訳の確認（reason_breakdown）の質問が
    なければ不合格。"""
    lead = prepare(repo, synthetic_run)
    lid = lead["lead"]
    adjusted, _, evidence = adjusted_by_volume(repo, synthetic_run, lead)
    support = {"evidence": [evidence], "searched": "公表元の説明を確かめた。"}
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lid,
            "outcome": "change",
            "comparison_state": "comparable",
            "explanation_status": "full",
        },
    )
    residual = rec(
        repo,
        synthetic_run,
        {
            "kind": "derive",
            "op": "diff",
            "args": [lead["calc_to"], adjusted],
            "label": "説明を反映した後に残る差",
            "unit": "円",
        },
    )
    assert residual["value"] == "2500.00"
    for recalc in (lead["calc_from"], lead["calc_to"]):  # 比べる年度の合計そのものは不可
        rec(
            repo,
            synthetic_run,
            explanation(
                lid,
                "T3",
                "support",
                scope="full",
                recalc_calc_id=recalc,
                residual_calc_id=residual["id"],
                **support,
            ),
        )
        assert failing(repo, synthetic_run, lid) == ["V8_EXPLANATION_STATUS"]
    rec(
        repo,
        synthetic_run,
        explanation(
            lid,
            "T3",
            "support",
            scope="full",
            recalc_calc_id=adjusted,
            residual_calc_id=residual["id"],
            **support,
        ),
    )
    assert check.run_check(repo, synthetic_run, lid)["passed"] is True
    rec(
        repo,
        synthetic_run,
        explanation(
            lid, "T3", "support", scope="unknown", no_quant_reason="分量の内訳がない。", **support
        ),
    )
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lid,
            "outcome": "change",
            "comparison_state": "comparable",
            "explanation_status": "unknown",
        },
    )
    assert failing(repo, synthetic_run, lid) == ["V8_EXPLANATION_STATUS"]


def test_d041_t6b_missing_applicable_field_is_unknown(repo: Path, synthetic_run: str) -> None:
    """T6b（D-041④）：分野の規則でその行に当たる照合項目（競争入札の行の契約を締結した日）の結果がなければ未確認。"""
    lead = prepare(repo, synthetic_run, conclusion=False)
    conn = ledger_of(repo, synthetic_run)
    (result,) = conn.execute(
        "SELECT result_json FROM computations WHERE calc_id = 'K0005'"
    ).fetchone()
    data = json.loads(result)
    del data["rows"][ROW_FROM]["fields"]["contract_date"]
    conn.execute(
        "UPDATE computations SET result_json = ? WHERE calc_id = 'K0005'", (json.dumps(data),)
    )
    conn.commit()
    conn.close()
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
    assert check.run_check(repo, synthetic_run, lead["lead"])["t6"]["T6b"] == "unknown"


# ---------------------------------------------------------------------------
# D-042 の回帰（先に失敗するテストとして書いた）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["changed", "deleted"])
def test_d042_required_csv_without_rows_is_dependency(
    repo: Path, synthetic_run: str, how: str
) -> None:
    """V2（D-042①）：S1 の「翻訳業務」は両方式の CSV に依存する。該当する行のない令和7年度の随意契約の CSV（S007）
    を書き換えるか消しても V2_HASH_MISMATCH。その SHA-256 は検証記録のハッシュにも入る。"""
    lead = prepare(repo, synthetic_run)
    conn = ledger_of(repo, synthetic_run)
    assert "S007" in hashing.dependency_sources(
        conn, lead["lead"], load_adapter(repo, "cao-procurement")
    )
    before = hashing.check_hash(conn, lead["lead"], load_adapter(repo, "cao-procurement"))
    conn.execute("UPDATE sources SET sha256 = sha256 WHERE source_id = 'S007'")
    (sha,) = conn.execute("SELECT sha256 FROM sources WHERE source_id = 'S007'").fetchone()
    conn.execute("UPDATE sources SET sha256 = ? WHERE source_id = 'S007'", ("e" * 64,))
    assert hashing.check_hash(conn, lead["lead"], load_adapter(repo, "cao-procurement")) != before
    conn.rollback()
    conn.close()
    raw = repo / "data" / "raw" / f"{sha}.csv"
    if how == "changed":
        raw.write_bytes(raw.read_bytes() + b"\r\n")
    else:
        raw.unlink()
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lead["lead"])
    assert codes(info) == ["V2_HASH_MISMATCH"] and "S007" in info.value.failures[0]["affected_ids"]


def test_d042_string_years_are_rejected_and_not_dropped(repo: Path, synthetic_run: str) -> None:
    """V1（D-042②）：record は条件の fiscal_year の文字列（"2024"、["2024"]）を E_SCHEMA にする。台帳に整数でない
    年度があっても、依存する資料を空にせず、series_years のすべての年度を求める。"""
    from conftest import calc_sum, put
    from kizashi import record

    for value, op in (("2024", "="), (["2024"], "in"), (True, "=")):
        data = calc_sum(GROUP, 2024)
        data["filter"] = [
            f if f["col"] != "fiscal_year" else {**f, "op": op, "value": value}
            for f in data["filter"]
        ]
        with pytest.raises(KizashiError) as info:
            record.record(repo, synthetic_run, put(repo, "y.json", data))
        assert info.value.code == "E_SCHEMA" and "fiscal_year" in info.value.message
    lead = prepare(repo, synthetic_run)
    tamper(
        repo,
        synthetic_run,
        "UPDATE computations SET request_json = json_set(request_json, '$.filter[0].value', '2024')"
        " WHERE calc_id = ?",
        (lead["calc_from"],),
    )
    conn = ledger_of(repo, synthetic_run)
    needed = hashing.required_sources(conn, load_adapter(repo, "cao-procurement"), lead["lead"])
    conn.close()
    assert needed == [(2024, "kyoso"), (2024, "zuikei"), (2025, "kyoso"), (2025, "zuikei")]


def test_d042_amount_residual_must_be_diff_with_comparison(repo: Path, synthetic_run: str) -> None:
    """V8（D-042③）：金額の候補の残る差に、前の年度の行の件数、説明を反映した値を引数に含む掛け算、元の合計と同じ
    集計を別のIDで作り直した値との差を使うと V8_EXPLANATION_STATUS。"""
    from conftest import calc_sum

    lead = prepare(repo, synthetic_run)
    lid = lead["lead"]
    adjusted, ratio, evidence = adjusted_by_volume(repo, synthetic_run, lead)
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lid,
            "outcome": "change",
            "comparison_state": "comparable",
            "explanation_status": "full",
        },
    )
    count = rec(
        repo,
        synthetic_run,
        {
            "kind": "calc",
            "op": "count",
            "table": "procurement_contracts",
            "filter": [
                {"col": "fiscal_year", "op": "=", "value": 2024},
                {"col": "record_id", "op": "in", "value": [ROW_FROM]},
            ],
            "label": "残る行の件数",
            "unit": "件",
        },
    )["id"]
    product = rec(
        repo,
        synthetic_run,
        {
            "kind": "derive",
            "op": "product",
            "args": [adjusted, ratio],
            "label": "残る差",
            "unit": "円",
        },
    )["id"]
    duplicate = rec(repo, synthetic_run, calc_sum(GROUP, 2025, label="作り直した令和7年度の合計"))[
        "id"
    ]
    dup_diff = rec(
        repo,
        synthetic_run,
        {
            "kind": "derive",
            "op": "diff",
            "args": [duplicate, lead["calc_from"]],
            "label": "残る差",
            "unit": "円",
        },
    )["id"]
    for recalc, residual in ((adjusted, count), (adjusted, product), (duplicate, dup_diff)):
        rec(
            repo,
            synthetic_run,
            explanation(
                lid,
                "T3",
                "support",
                scope="full",
                recalc_calc_id=recalc,
                residual_calc_id=residual,
                evidence=[evidence],
                searched="公表元の説明を確かめた。",
            ),
        )
        assert failing(repo, synthetic_run, lid) == ["V8_EXPLANATION_STATUS"], (recalc, residual)


def s3_lead(repo: Path, run_id: str) -> dict:
    """S3 の候補「調査研究委託」（令和6年度 S003:000001 → 令和7年度 S007:000001。理由の分類の変化）に、比較成立の
    必須の中身をそろえる（対応はこの候補の行で登録する）。"""
    conn = ledger_of(repo, run_id)
    row = conn.execute(
        "SELECT * FROM leads WHERE screen_id = 's3_reason_change' AND lead_key = '調査研究委託'"
    ).fetchone()
    conn.close()
    lead = prepare(repo, run_id, lead=row, match="none", conclusion=False)
    rec(
        repo,
        run_id,
        {
            "kind": "match",
            "lead_id": lead["lead"],
            "left": ["S003:000001"],
            "right": ["S007:000001"],
            "relation": "1:1",
            "reason": "件名と業務の内容が同じ。",
            "evidence": [],
            "status": "confirmed",
        },
    )
    rec(
        repo,
        run_id,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "change",
            "comparison_state": "comparable",
            "explanation_status": "part",
        },
    )
    return lead


def zuikei_count(repo: Path, run_id: str, year: int, group: str, **extra) -> str:
    return rec(
        repo,
        run_id,
        {
            "kind": "calc",
            "op": "count",
            "table": "procurement_contracts",
            "filter": [
                {"col": "fiscal_year", "op": "=", "value": year},
                {"col": "group_key", "op": "=", "value": group},
                {"col": "method_family", "op": "=", "value": "zuikei"},
            ],
            "label": "説明を反映した条件で残る随意契約の行の件数",
            "unit": "件",
            **extra,
        },
    )["id"]


RULE_TEXT = "令和7年度から、調査研究の委託は企画競争により相手方を選ぶこととした。"
PLANNING = {"col": "reason_category", "op": "=", "value": "planning_competition"}


def count_2025(repo: Path, run_id: str, filters: list[dict]) -> str:
    return rec(
        repo,
        run_id,
        {
            "kind": "calc",
            "op": "count",
            "table": "procurement_contracts",
            "filter": filters,
            "label": "説明を反映した条件で残る行の件数",
            "unit": "件",
        },
    )["id"]


def rule_change_evidence(repo: Path, run_id: str) -> dict:
    """条件の変更（企画競争）を裏付ける合成の追加資料（取得の経路で data/raw に保存する）。"""
    sid = fetch_html_evidence(repo, run_id, RULE_TEXT)
    return {"source_id": sid, "position": {"page": 1}, "quote": RULE_TEXT, "role": "support"}


def test_d042_count_residual_for_non_amount_lead(repo: Path, synthetic_run: str) -> None:
    """V8（D-042③・D-043①②）：金額以外の候補（S3「調査研究委託」。令和6年度は公募、令和7年度は企画競争）では、
    条件をそろえた後に残る行の件数を、説明を反映した値と残る差の両方にできる。条件は fiscal_year = 2025・
    group_key・説明によって足した reason_category = planning_competition（S007:000001 だけ。候補の行の部分集合）。
    根拠は企画競争への変更を書いた合成の追加資料。合格し、メモの4節に足した条件が出る。"""
    lead = s3_lead(repo, synthetic_run)
    lid = lead["lead"]
    evidence = rule_change_evidence(repo, synthetic_run)
    good = count_2025(
        repo,
        synthetic_run,
        [
            {"col": "fiscal_year", "op": "=", "value": 2025},
            {"col": "group_key", "op": "=", "value": "調査研究委託"},
            PLANNING,
        ],
    )
    rec(
        repo,
        synthetic_run,
        explanation(
            lid,
            "T1",
            "support",
            scope="part",
            recalc_calc_id=good,
            residual_calc_id=good,
            reflected_conditions=[PLANNING],
            evidence=[evidence],
            searched="公表元の告知を確かめた。",
        ),
    )
    assert check.run_check(repo, synthetic_run, lid)["passed"] is True
    from kizashi import render

    text = (repo / render.render_lead(repo, synthetic_run, lid)["memo"]).read_text(encoding="utf-8")
    assert "説明によって足した・変えた条件：reason_category = planning_competition" in text
    assert "条件をそろえた後に残る行：S007:000001" in text


def test_d043_non_amount_residual_rejects_rewrites_and_unrelated(
    repo: Path, synthetic_run: str
) -> None:
    """V8（D-043①②）：金額以外の候補で、説明を反映した値に無関係な業務の金額（翻訳業務の令和6年度の合計）を使う、
    元の fiscal_year の条件を重ねただけ、値が1つの in を = に書き換えただけ、候補を選び直すだけの group_key の条件、
    残る件数の条件にない条件を足したと書く、足した条件を書かない、のどれも V8_EXPLANATION_STATUS。"""
    from conftest import calc_sum

    lead = s3_lead(repo, synthetic_run)
    lid = lead["lead"]
    evidence = rule_change_evidence(repo, synthetic_run)
    fy = {"col": "fiscal_year", "op": "=", "value": 2025}
    good = count_2025(
        repo,
        synthetic_run,
        [fy, {"col": "group_key", "op": "=", "value": "調査研究委託"}, PLANNING],
    )
    unrelated = rec(repo, synthetic_run, calc_sum(GROUP, 2024, label="翻訳業務の令和6年度の合計"))[
        "id"
    ]
    doubled = count_2025(
        repo, synthetic_run, [fy, fy, {"col": "record_id", "op": "in", "value": ["S007:000001"]}]
    )
    rewritten = count_2025(
        repo, synthetic_run, [fy, {"col": "record_id", "op": "=", "value": "S007:000001"}]
    )
    regroup = count_2025(
        repo, synthetic_run, [fy, {"col": "group_key", "op": "=", "value": "調査研究委託"}]
    )
    open_call = {"col": "reason_category", "op": "=", "value": "open_call"}
    cases = [
        (unrelated, good, [PLANNING]),
        (doubled, doubled, [fy]),
        (rewritten, rewritten, [{"col": "record_id", "op": "=", "value": "S007:000001"}]),
        (regroup, regroup, [{"col": "group_key", "op": "=", "value": "調査研究委託"}]),
        (good, good, [open_call]),  # 残る件数の条件にない
        (good, good, None),  # 足した条件を書かない
    ]
    for recalc, residual, reflected in cases:
        extra = {} if reflected is None else {"reflected_conditions": reflected}
        rec(
            repo,
            synthetic_run,
            explanation(
                lid,
                "T1",
                "support",
                scope="part",
                recalc_calc_id=recalc,
                residual_calc_id=residual,
                evidence=[evidence],
                searched="公表元の告知を確かめた。",
                **extra,
            ),
        )
        assert failing(repo, synthetic_run, lid) == ["V8_EXPLANATION_STATUS"], (
            recalc,
            residual,
            reflected,
        )


def test_d043_reflected_conditions_are_validated_at_record(repo: Path, synthetic_run: str) -> None:
    """6.2（D-043②）：reflected_conditions は filter の要素の形で、使える列・演算・整数の年度だけ（record で検査）。"""
    from conftest import put
    from kizashi import record

    lead = s3_lead(repo, synthetic_run)
    for bad in (
        "reason_category",
        [{"col": "nosuch", "op": "=", "value": 1}],
        [{"col": "fiscal_year", "op": "=", "value": "2025"}],
    ):
        data = explanation(lead["lead"], "T1", "unknown", reflected_conditions=bad)
        with pytest.raises(KizashiError) as info:
            record.record(repo, synthetic_run, put(repo, "e.json", data))
        assert info.value.code in ("E_SCHEMA", "E_NOT_ALLOWED_COLUMN"), bad


def test_d042_record_on_other_rule_version_is_typed(repo: Path, synthetic_run: str) -> None:
    """4.5（D-042⑤・D-043④）：照合する4つの版がいまと違う台帳への record は、SQL の例外ではなく E_RULE_VERSION
    （終了コード3）で拒否し、提案を inbox/ に残し、台帳（開始時の版）を変えず、新しい実行が要ることを示す。
    記録だけのもの（git のコミット、Skill とルールのファイル）の違いでは拒否しない。"""
    from conftest import calc_sum, put
    from kizashi import record

    tamper(
        repo,
        synthetic_run,
        "UPDATE runs SET rule_versions_json = json_set(rule_versions_json, '$.git_commit', 'abc1234',"
        ' \'$.rule_files\', json(\'{"files": {"CLAUDE.md": "00"}}\'))',
    )
    assert (
        record.record(repo, synthetic_run, put(repo, "ok.json", calc_sum(GROUP, 2024)))["kind"]
        == "calc"
    )
    tamper(
        repo,
        synthetic_run,
        "UPDATE runs SET rule_versions_json = json_set(rule_versions_json, '$.rules_version', '0')",
    )
    conn = ledger_of(repo, synthetic_run)
    before = conn.execute("SELECT rule_versions_json FROM runs").fetchone()[0]
    n_calcs = conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0]
    conn.close()
    arg = put(repo, "c.json", calc_sum(GROUP, 2025))
    with pytest.raises(KizashiError) as info:
        record.record(repo, synthetic_run, arg)
    assert (info.value.code, info.value.exit_code) == ("E_RULE_VERSION", 3)
    assert "新しい実行" in info.value.next_action and "rules_version" in info.value.message
    assert (repo / arg).is_file()
    conn = ledger_of(repo, synthetic_run)
    assert conn.execute("SELECT rule_versions_json FROM runs").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0] == n_calcs
    conn.close()
