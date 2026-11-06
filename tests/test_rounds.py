"""巡回数（test_plan.md の CK-3。implementation_spec.md 3.6・4.6、設計書4.3）。

結論の案（保留を除く）を登録した後の check を1巡と数える。上限は MAX_ROUNDS（2巡）で、保留にしたことのある候補を
調べ直す場合は1巡を足す。上限に達した候補は、保留以外の結論の案を E_ROUNDS_EXCEEDED で拒否する（record の
結論の案と、上限を超えることになる check）。保留の案の check と render の中の検査は数えない。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ledger_of, put, rec
from kizashi import check, limits, record, render
from kizashi.errors import KizashiError
from test_check import prepare


def rounds_of(repo: Path, run_id: str, lead_id: str) -> int:
    conn = ledger_of(repo, run_id)
    (value,) = conn.execute("SELECT rounds FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    conn.close()
    return value


def check_events(repo: Path, run_id: str, lead_id: str) -> int:
    conn = ledger_of(repo, run_id)
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'check' AND target_id = ?", (lead_id,)
    ).fetchone()
    conn.close()
    return n


def conclusion(lead_id: str, outcome: str) -> dict:
    data = {
        "kind": "conclusion",
        "lead_id": lead_id,
        "outcome": outcome,
        "comparison_state": "comparable",
    }
    if outcome == "change":
        data["explanation_status"] = "none"
    if outcome == "hold":
        data["hold_reason"] = "limit"
    return data


def test_ck3_rounds_limit_hold_and_render(repo: Path, synthetic_run: str) -> None:
    """CK-3：比較成立の案の check は1巡ずつ数え、render の中の検査は数えない。2巡の後の check と、保留以外の
    結論の案は E_ROUNDS_EXCEEDED（巡回数も events も変えない。提案は inbox/ に残る）。保留の案は受け付け、その
    check は数えない。保留にした候補を調べ直すと1巡を足す（3巡まで）。"""
    assert limits.MAX_ROUNDS == 2
    lid = prepare(repo, synthetic_run)["lead"]
    assert rounds_of(repo, synthetic_run, lid) == 0
    assert check.run_check(repo, synthetic_run, lid)["rounds"] == 1
    render.render_lead(repo, synthetic_run, lid)  # render の中の検査は数えない
    assert rounds_of(repo, synthetic_run, lid) == 1
    assert check.run_check(repo, synthetic_run, lid)["rounds"] == 2
    events = check_events(repo, synthetic_run, lid)

    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lid)
    assert (info.value.code, info.value.stage, info.value.exit_code) == (
        "E_ROUNDS_EXCEEDED",
        "check",
        1,
    )
    assert "保留" in info.value.next_action
    assert rounds_of(repo, synthetic_run, lid) == 2
    assert check_events(repo, synthetic_run, lid) == events

    for outcome in ("change", "artifact"):
        with pytest.raises(KizashiError) as info:
            record.record(repo, synthetic_run, put(repo, "c.json", conclusion(lid, outcome)))
        assert (info.value.code, info.value.stage) == ("E_ROUNDS_EXCEEDED", "record")
        assert (repo / "inbox" / "c.json").exists()

    rec(repo, synthetic_run, conclusion(lid, "hold"))
    for _ in range(3):  # 保留の案の check は数えない
        assert check.run_check(repo, synthetic_run, lid)["rounds"] == 2
    assert render.render_lead(repo, synthetic_run, lid)["state"] == "hold"
    assert rounds_of(repo, synthetic_run, lid) == 2

    # 保留にした候補を調べ直す：1巡を足す
    rec(repo, synthetic_run, conclusion(lid, "change"))
    assert check.run_check(repo, synthetic_run, lid)["rounds"] == 3
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lid)
    assert info.value.code == "E_ROUNDS_EXCEEDED" and "3巡" in info.value.message
    with pytest.raises(KizashiError) as info:
        record.record(repo, synthetic_run, put(repo, "c.json", conclusion(lid, "change")))
    assert info.value.code == "E_ROUNDS_EXCEEDED"


def test_ck3_registered_hold_counts_as_held(repo: Path, synthetic_run: str) -> None:
    """CK-3：保留の結論の案を登録したことがあれば（メモを書く前でも）、保留にした候補として1巡を足す。"""
    lid = prepare(repo, synthetic_run, conclusion=False)["lead"]
    rec(repo, synthetic_run, conclusion(lid, "change"))
    check.run_check(repo, synthetic_run, lid)
    check.run_check(repo, synthetic_run, lid)
    with pytest.raises(KizashiError):
        record.record(repo, synthetic_run, put(repo, "c.json", conclusion(lid, "change")))
    rec(repo, synthetic_run, conclusion(lid, "hold"))
    rec(repo, synthetic_run, conclusion(lid, "change"))
    assert check.run_check(repo, synthetic_run, lid)["rounds"] == 3
