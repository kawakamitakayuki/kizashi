"""bf1bede の実装レビューの修正のうち、書き出しと inspect の回帰テスト（1-10〜1-13・D-090⑦・D-091②）。

test_plan.md の RD-1（メモの節）、RD-3（実行の概要）、CP-1（係数の提案のメモ）と、inspect（4.3）の回帰。期待値は
implementation_spec.md の 4.3（memo の7項目）、9章の「メモに出る計算」（展開する欄）、10.1・10.3 と、決定の
D-090⑦・D-091②から書き写した。
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import ledger_of, rec
from kizashi import inspect_cmd, render
from test_check import ROW_FROM, ROW_TO, prepare

MEMO_KEYS = {"memo_no", "file", "check_hash", "current_hash", "stale", "status", "note"}


def hold_memo(repo: Path, run_id: str, *, coefficient: dict | None = None) -> tuple[dict, str]:
    """保留（比較の条件）のメモを書いた候補と、メモの本文。"""
    lead = prepare(repo, run_id, conclusion=False)
    if coefficient is not None:
        rec(repo, run_id, {"kind": "coefficient", "lead_id": lead["lead"], **coefficient})
    rec(
        repo,
        run_id,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "comparable",
            "hold_reason": "comparison",
        },
    )
    result = render.render_lead(repo, run_id, lead["lead"])
    return lead, (repo / result["memo"]).read_text(encoding="utf-8")


def section(text: str, start: str, end: str) -> str:
    return text.split(start)[1].split(end)[0]


# ---------------------------------------------------------------------------
# 1-10（CP-1）：7節の係数の提案の文字列も {calc:ID} を展開する
# ---------------------------------------------------------------------------


def test_cp1_coefficient_meaning_is_expanded_in_section7(repo: Path, synthetic_run: str) -> None:
    """CP-1（1-10）：係数の意味に書いた {calc:ID} を、4節と同じく7節でも値・意味・計算IDに展開する。"""
    conn = ledger_of(repo, synthetic_run)
    (calc_from,) = conn.execute(
        "SELECT calc_from FROM leads WHERE screen_id = 's1_amount_change' AND lead_key = '翻訳業務'"
    ).fetchone()
    conn.close()
    _, text = hold_memo(
        repo,
        synthetic_run,
        coefficient={
            "key": "tax",
            "value": "1.1",
            "meaning": f"税込と税抜の換算（基準は{{calc:{calc_from}}}）",
            "why_needed": "片方の年度が税抜かもしれない。",
            "apply_to": "両年度の合計",
            "basis": [],
            "status": "proposed",
        },
    )
    section7 = section(text, "## 7 記者の確認リスト", "## 8")
    line = next(x for x in section7.splitlines() if x.startswith("- tax："))
    assert "{calc:" not in line
    assert f"計算{calc_from}" in line


# ---------------------------------------------------------------------------
# D-090⑦（RD-1）：7節に複合キーと、引用をたどる案内（資料ID・位置・掲載節）を出す
# ---------------------------------------------------------------------------


def test_rd1_section7_has_composite_key_and_quote_locations(repo: Path, synthetic_run: str) -> None:
    """RD-1（D-090⑦）：確認リストの各行に複合キー（件名・契約日・金額・相手方の元の値）を出す。引用は、資料ID・
    位置・掲載節で引ける一覧を出す（長い全文は再掲しない）。"""
    lead, text = hold_memo(repo, synthetic_run)
    section7 = section(text, "## 7 記者の確認リスト", "## 8")
    conn = ledger_of(repo, synthetic_run)
    cells = json.loads(
        conn.execute("SELECT cells_json FROM records WHERE record_id = ?", (ROW_TO,)).fetchone()[0]
    )
    conn.close()
    row_line = next(x for x in section7.splitlines() if x.startswith(f"- {ROW_TO}："))
    key = "／".join(
        cells.get(k, "")
        for k in ("title_raw", "contract_date_raw", "amount_raw", "counterparty_raw")
    )
    assert f"複合キー {key}" in row_line
    quotes = section(section7, "### 引用をたどる（資料ID・位置・掲載節）", "###")
    claim_line = next(x for x in quotes.splitlines() if lead["record"] in x)
    assert "S005" in claim_line and f"{ROW_TO} の amount_raw" in claim_line
    assert "3節" in claim_line
    t2_line = next(x for x in quotes.splitlines() if "T2" in x)
    assert "S001" in t2_line and f"{ROW_FROM} の contract_date_raw" in t2_line and "4節" in t2_line
    assert "「750,000」" not in quotes  # 引用の全文は3節・4節にあり、ここでは再掲しない


# ---------------------------------------------------------------------------
# 1-12（RD-1）：メモ1節の「止まった・欠けたもの」は stop_reason の JSON の原文にしない
# ---------------------------------------------------------------------------


def test_rd1_stop_reason_is_described_in_japanese() -> None:
    """RD-1（1-12）：欠けた資料・実行しなかった検出SQL・止まった工程を日本語で書き、英字のキーを出さない。"""
    reason = {
        "missing": [
            {
                "source_id": "S004",
                "logical_key": "cao-procurement|2025|kyoso|csv",
                "status": "failed",
                "failure_reason": "HTTP 404",
            }
        ],
        "screens_not_run": [{"screen": "s2_method_change", "missing": ["kyoso:2025"]}],
    }
    text = render.stop_summary(reason)
    assert "S004（cao-procurement|2025|kyoso|csv。取得できなかった。HTTP 404）" in text
    assert "s2_method_change（欠けた資料：kyoso:2025）" in text
    for key in ("missing", "screens_not_run", "logical_key", "failure_reason", "{", '"'):
        assert key not in text
    stage = render.stop_summary(
        {"stage": "normalize", "code": "E_V7", "retryable": False, "message": "氏名の検査"}
    )
    assert stage == "止まった工程：normalize E_V7：氏名の検査"
    assert render.stop_summary(None) is None


def test_rd1_memo_section1_uses_stop_summary(repo: Path, synthetic_run: str) -> None:
    """RD-1（1-12）：メモ1節にも整形した文を出す（JSON の原文を出さない）。"""
    conn = ledger_of(repo, synthetic_run)
    reason = {"screens_not_run": [{"screen": "s9_other", "missing": ["zuikei:2025"]}]}
    conn.execute("UPDATE runs SET stop_reason = ?", (json.dumps(reason),))
    conn.commit()
    conn.close()
    _, text = hold_memo(repo, synthetic_run)
    section1 = section(text, "## 1 状態と範囲", "## 2")
    assert "s9_other（欠けた資料：zuikei:2025）" in section1
    assert "screens_not_run" not in section1


# ---------------------------------------------------------------------------
# 1-11・D-091②（RD-3）：実行の概要の品質の要約に、Q05 の要確認を数と項目の両方で出す
# ---------------------------------------------------------------------------


def test_rd3_quality_summary_counts_needs_review(repo: Path, synthetic_run: str) -> None:
    """RD-3（D-091②）：要確認（needs_review）を合格と混同せず、表の数と項目の両方に出す。表の数の合計は項目の数と
    合う。項目の詳しい中身は品質検査の記録の文（行ID）だけで、氏名の断片は出さない。"""
    conn = ledger_of(repo, synthetic_run)
    counts = json.loads(conn.execute("SELECT counts_json FROM runs").fetchone()[0])
    source_id = min(counts["quality"])
    counts["quality"][source_id]["Q05"] = {
        "state": "needs_review",
        "detail": "分けられないセルが1件：S001:000003",
        "count": 1,
    }
    conn.execute("UPDATE runs SET counts_json = ?", (json.dumps(counts, ensure_ascii=False),))
    conn.commit()
    conn.close()
    result = render.render_run(repo, synthetic_run)
    text = (repo / result["summary"]).read_text(encoding="utf-8")
    summary = section(text, "## 品質レポートの要約", "## 検出")
    header = next(x for x in summary.splitlines() if x.startswith("| 資料ID"))
    assert "要確認" in header
    row = next(x for x in summary.splitlines() if x.startswith(f"| {source_id} "))
    cells = [c.strip() for c in row.strip("|").split("|")]
    items = {k for k, v in counts["quality"][source_id].items() if isinstance(v, dict)}
    assert sum(int(c) for c in cells[1:]) == len(items)
    assert cells[header.strip("|").split("|").index(" 要確認 ")] == "1"
    listed = section(summary, "不合格・未実行・要確認：", "- 報告")
    assert f"- {source_id} Q05（要確認）：分けられないセルが1件：S001:000003" in listed


# ---------------------------------------------------------------------------
# 1-13（4.3）：inspect の候補の memo は、いつも7項目を出す
# ---------------------------------------------------------------------------


def test_inspect_memo_has_seven_keys_without_memo(repo: Path, synthetic_run: str) -> None:
    """4.3（1-13）：メモがない候補でも memo_no・file・check_hash・current_hash・stale・status・note を出す。"""
    data = inspect_cmd.inspect(repo, "L0001", synthetic_run)
    assert set(data["memo"]) == MEMO_KEYS
    assert data["memo"]["status"] == "メモなし"
    assert data["memo"]["check_hash"] is None and data["memo"]["current_hash"] is None


def test_inspect_memo_has_seven_keys_with_valid_memo(repo: Path, synthetic_run: str) -> None:
    """4.3（1-13）：有効なメモでも note の項目を出す（値は null）。"""
    lead, _ = hold_memo(repo, synthetic_run)
    data = inspect_cmd.inspect(repo, lead["lead"], synthetic_run)
    assert set(data["memo"]) == MEMO_KEYS
    assert (data["memo"]["status"], data["memo"]["note"]) == ("有効", None)
