"""bf1bede の実装レビューの修正のうち、check の回帰テスト（1-1・D-090③④）。

test_plan.md の CK-1（failures の順）、CK-2（候補の行と率の分母）、CK-4（不合格の check の強い語の警告）。
期待値は implementation_spec.md の 3.2（failures は9章の表の順）、9章の表と T6a・T6b の段落（率の分母だけに
使う行）、9章の V7（強い語は warnings）と、決定の D-090③④から書き写した。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ledger_of, make_lead, rec
from kizashi import check, cli, hashing, ledger
from kizashi.errors import KizashiError

# 9章の表の順（implementation_spec.md の 9章から書き写した）
TABLE_ORDER = [
    "V1_INPUT_EMPTY",
    "V1_QUALITY_NOT_RUN",
    "V1_RUN_FAILED",
    "V2_REF_MISSING",
    "V2_HASH_MISMATCH",
    "V2_STALE_OR_WITHDRAWN",
    "V2_BAD_EVIDENCE_SOURCE",
    "V3A_RECALC_MISMATCH",
    "V3B_REAGG_MISMATCH",
    "V3B_HEADER_UNRESOLVED",
    "V3D_DOC_VALUE_MISMATCH",
    "V3C_DERIVE_MISMATCH",
    "V3_UNCITED_NUMBER",
    "V3_NO_MEANING",
    "V4_QUOTE_NOT_FOUND",
    "V4_QUOTE_TRUNCATED",
    "V4_QUOTE_YEAR",
    "V4_PAGE_TEXT_MISMATCH",
    "V5_CONDITION_MISSING",
    "V5_UNRESOLVED_COMPARISON",
    "V5_ADJUSTED_NO_VALUES",
    "V5_BAD_RATE",
    "V5_CHANGE_NOT_COMPARABLE",
    "V5_PARTIAL_YEAR",
    "V6_TYPE_MISSING",
    "V6_NO_EVIDENCE",
    "V6_SELF_EVIDENCE",
    "V6_NO_SEARCH_SCOPE",
    "V7_RAW_NUMBER",
    "V7_PERSONAL_NAME",
    "V8_REQUIRED_MISSING",
    "V8_MATCH_UNCONFIRMED",
    "V8_COEFFICIENT_PENDING",
    "V8_CHANGE_NOT_NEGATED",
    "V8_ARTIFACT_NO_FACTOR",
    "V8_EXPLANATION_STATUS",
    "V8_NO_WITHDRAW_RESULT",
    "V8_HOLD_REASON",
    "V8_T6C_QUESTION",
]


def failures_of(repo: Path, run_id: str, lead_id: str) -> KizashiError:
    with pytest.raises(KizashiError) as exc:
        check.run_check(repo, run_id, lead_id)
    assert exc.value.failures
    return exc.value


def assert_table_order(error: KizashiError) -> None:
    codes = [f["code"] for f in error.failures or []]
    positions = [TABLE_ORDER.index(c) for c in codes]
    assert positions == sorted(positions), codes
    assert error.code == codes[0]


# ---------------------------------------------------------------------------
# 1-1（CK-1）：failures は9章の表の順。code は最初の不合格
# ---------------------------------------------------------------------------


def _dataset_pdf(repo: Path, run_id: str) -> str:
    conn = ledger_of(repo, run_id)
    (source_id,) = conn.execute(
        "SELECT source_id FROM sources WHERE kind = 'dataset' AND logical_key LIKE '%|pdf'"
        " ORDER BY source_id LIMIT 1"
    ).fetchone()
    conn.close()
    return source_id


def test_ck1_failures_follow_table_order(repo: Path, synthetic_run: str) -> None:
    """CK-1（1-1）：V4 の中で見つけた V2_BAD_EVIDENCE_SOURCE は V3_NO_MEANING より前、V8_REQUIRED_MISSING は
    ほかの V8 より前に並ぶ。code は並べた後の最初の不合格。"""
    conn = ledger_of(repo, synthetic_run)
    (calc_to,) = conn.execute("SELECT calc_to FROM leads WHERE lead_id = 'L0001'").fetchone()
    conn.execute("UPDATE computations SET unit = '' WHERE calc_id = ?", (calc_to,))
    conn.execute(
        "UPDATE leads SET proposed_outcome = 'change', comparison_state = 'comparable',"
        " explanation_status = 'none' WHERE lead_id = 'L0001'"
    )
    evidence = [
        {
            "source_id": _dataset_pdf(repo, synthetic_run),
            "position": {"page": 1},
            "quote": "公表一覧のページの文。",
            "role": "support",
        }
    ]
    conn.execute(
        "INSERT INTO explanations (lead_id, type, judgment, evidence_json, searched, scope,"
        " no_quant_reason, updated_at) VALUES ('L0001', 'T1', 'support', ?, '制度を調べた。',"
        " 'unknown', '定量化できない。', ?)",
        (json.dumps(evidence, ensure_ascii=False), ledger.now_iso()),
    )
    conn.commit()
    conn.close()
    error = failures_of(repo, synthetic_run, "L0001")
    codes = [f["code"] for f in error.failures or []]
    assert {"V2_BAD_EVIDENCE_SOURCE", "V3_NO_MEANING", "V8_REQUIRED_MISSING"} <= set(codes)
    assert_table_order(error)
    assert error.code == "V2_BAD_EVIDENCE_SOURCE"


def test_ck1_failures_order_on_plain_candidate(repo: Path, synthetic_run: str) -> None:
    """CK-1（1-1）：調べていない検出の候補（判定・仮説・質問がない）の不合格も、表の順に並ぶ。"""
    assert_table_order(failures_of(repo, synthetic_run, "L0002"))


def test_ck1_cli_error_lists_failures_in_table_order(
    repo: Path, synthetic_run: str, capsys
) -> None:
    """CK-1（1-1）：CLI の出力の error.code と failures も同じ順（3.2）。"""
    code = cli.main(["check", "L0002", "--run", synthetic_run])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    codes = [f["code"] for f in payload["error"]["failures"]]
    assert [TABLE_ORDER.index(c) for c in codes] == sorted(TABLE_ORDER.index(c) for c in codes)
    assert payload["error"]["code"] == codes[0]


# ---------------------------------------------------------------------------
# D-090③（CK-2）：候補の行から除くのは、ratio の分母にしか使わない計算の行だけ
# ---------------------------------------------------------------------------


def _total_2024(repo: Path, run_id: str) -> str:
    """令和6年度の全行の契約金額の合計（候補に所属させない。分母に使う）。"""
    return rec(
        repo,
        run_id,
        {
            "kind": "calc",
            "op": "sum",
            "table": "procurement_contracts",
            "column": "amount_yen",
            "filter": [{"col": "fiscal_year", "op": "=", "value": 2024}],
            "label": "令和6年度の契約金額の合計（全体）",
            "unit": "円",
        },
        name="all.json",
    )["id"]


def _derive(repo: Path, run_id: str, op: str, args: list[str], lead_id: str, unit: str) -> str:
    return rec(
        repo,
        run_id,
        {
            "kind": "derive",
            "op": op,
            "args": args,
            "label": f"{op} の派生計算",
            "unit": unit,
            "lead_id": lead_id,
        },
        name=f"d-{op}.json",
    )["id"]


def _rows(conn, calc_id: str) -> set[str]:
    (refs,) = conn.execute(
        "SELECT row_refs_json FROM computations WHERE calc_id = ?", (calc_id,)
    ).fetchone()
    return set(json.loads(refs))


def test_ck2_denominator_only_rows_are_excluded(repo: Path, synthetic_run: str) -> None:
    """CK-2（D-090③）：ratio の分母にしか使わない計算（令和6年度の全体の合計）の行は、候補の行に入れない。
    ただし、メモに出る計算には入る（依存する資料の検査と検証記録のハッシュの対象）。"""
    lead = make_lead(repo, synthetic_run)
    total = _total_2024(repo, synthetic_run)
    _derive(repo, synthetic_run, "ratio", [lead["from"], total], lead["lead"], "比率")
    conn = ledger_of(repo, synthetic_run)
    expected = _rows(conn, lead["from"]) | _rows(conn, lead["to"])
    assert set(check.candidate_rows(conn, lead["lead"])) == expected
    assert _rows(conn, total) - expected  # 全体の合計にはほかの業務の行がある
    assert total in hashing.memo_calc_ids(conn, lead["lead"])
    conn.close()


def test_ck2_denominator_also_used_elsewhere_keeps_rows(repo: Path, synthetic_run: str) -> None:
    """CK-2（D-090③）：分母に使う計算を、派生計算を通じて別の用途（diff）にも使えば、その行は候補の行に残す。"""
    lead = make_lead(repo, synthetic_run)
    total = _total_2024(repo, synthetic_run)
    _derive(repo, synthetic_run, "ratio", [lead["from"], total], lead["lead"], "比率")
    _derive(repo, synthetic_run, "diff", [total, lead["from"]], lead["lead"], "円")
    conn = ledger_of(repo, synthetic_run)
    expected = _rows(conn, lead["from"]) | _rows(conn, lead["to"]) | _rows(conn, total)
    assert set(check.candidate_rows(conn, lead["lead"])) == expected
    conn.close()


def test_ck2_rate_base_rows_are_kept(repo: Path, synthetic_run: str) -> None:
    """CK-2（D-090③）：rate の基準の年度の行は、分母ではないので候補の行に残す。"""
    lead = make_lead(repo, synthetic_run)
    total = _total_2024(repo, synthetic_run)
    _derive(repo, synthetic_run, "rate", [lead["to"], total], lead["lead"], "比率")
    conn = ledger_of(repo, synthetic_run)
    expected = _rows(conn, lead["from"]) | _rows(conn, lead["to"]) | _rows(conn, total)
    assert set(check.candidate_rows(conn, lead["lead"])) == expected
    conn.close()


# ---------------------------------------------------------------------------
# D-090④（CK-4）：不合格の check でも、強い語の警告を最上位の warnings に返す
# ---------------------------------------------------------------------------


def _strong_hypothesis(repo: Path, run_id: str) -> None:
    rec(
        repo,
        run_id,
        {
            "kind": "hypothesis",
            "lead_id": "L0002",
            "text": "談合があったかを確かめる。",
            "withdraw_if": "同じ条件でそろえると差がなくなる。",
        },
        name="h.json",
    )


def test_ck4_failed_check_returns_strong_word_warnings(repo: Path, synthetic_run: str) -> None:
    """CK-4（D-090④）：不合格でも、得られた W7_STRONG_WORD を返す。合否は変えない。"""
    _strong_hypothesis(repo, synthetic_run)
    error = failures_of(repo, synthetic_run, "L0002")
    assert [w["code"] for w in error.warnings or []] == ["W7_STRONG_WORD"]
    assert error.exit_code == 1


def test_ck4_failed_check_cli_has_top_level_warnings(
    repo: Path, synthetic_run: str, capsys
) -> None:
    """CK-4（D-090④）：CLI の出力では warnings を error の外（最上位）に置き、終了コードは1のまま（3.2）。"""
    _strong_hypothesis(repo, synthetic_run)
    capsys.readouterr()
    code = cli.main(["check", "L0002", "--run", synthetic_run])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert [w["code"] for w in payload["warnings"]] == ["W7_STRONG_WORD"]
    assert "warnings" not in payload["error"]
