"""巡回数（test_plan.md の CK-3。implementation_spec.md 3.6・4.6、設計書4.3）。

結論の案（保留を除く）を登録した後の check を1巡と数える。上限は MAX_ROUNDS（2巡）で、保留のメモを書いた候補を
調べ直す場合は1巡を足す（保留の結論の案を登録しただけでは足さない。D-056②）。上限に達した候補は、保留以外の
結論の案を E_ROUNDS_EXCEEDED で拒否する（record の結論の案、上限を超えることになる check、最後に数えた check に
合格した中身でない render）。保留の案の check と render の中の検査は数えない。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ledger_of, put, rec
from kizashi import check, limits, record, render
from kizashi.errors import KizashiError
from test_check import explanation, prepare


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


def memo_files(repo: Path, run_id: str) -> list[str]:
    memos = repo / "data" / "runs" / run_id / "memos"
    return sorted(p.name for p in memos.iterdir()) if memos.is_dir() else []


def test_ck3_registered_hold_alone_adds_no_round(repo: Path, synthetic_run: str) -> None:
    """CK-3（D-056②）：保留の結論の案を登録しただけでは1巡を足さない。設計書4.3 の「保留にした候補」は、保留の
    メモを書いた（state が hold になった）候補で、それを調べ直すときだけ1巡を足す。"""
    lid = prepare(repo, synthetic_run, conclusion=False)["lead"]
    rec(repo, synthetic_run, conclusion(lid, "change"))
    check.run_check(repo, synthetic_run, lid)
    check.run_check(repo, synthetic_run, lid)
    rec(repo, synthetic_run, conclusion(lid, "hold"))
    with pytest.raises(KizashiError) as info:  # 登録しただけの保留では上限は2巡のまま
        record.record(repo, synthetic_run, put(repo, "c.json", conclusion(lid, "change")))
    assert info.value.code == "E_ROUNDS_EXCEEDED" and "2巡" in info.value.message
    assert render.render_lead(repo, synthetic_run, lid)["state"] == "hold"
    rec(repo, synthetic_run, conclusion(lid, "change"))
    assert check.run_check(repo, synthetic_run, lid)["rounds"] == 3


def test_ck3_render_after_limit_refuses_content_not_passed(repo: Path, synthetic_run: str) -> None:
    """CK-3（D-056②）：2巡とも不合格の後に判定を直し、結論の案を登録し直さずに render しても、比較成立のメモは
    書かない（E_ROUNDS_EXCEEDED。render の中の検査に合格しても、上限に達した後の中身は数えた check を通って
    いない）。保留の結論の案にすれば書ける。"""
    lid = prepare(repo, synthetic_run)["lead"]
    # 説明の状況が「なし」の比較成立なのに、業務・数量の変化（T1）を支持にした（V8_EXPLANATION_STATUS）
    support = explanation(
        lid, "T1", "support", scope="unknown", no_quant_reason="数量の対応を確かめていない。"
    )
    rec(repo, synthetic_run, support)
    for _ in range(2):
        with pytest.raises(KizashiError):
            check.run_check(repo, synthetic_run, lid)
    assert rounds_of(repo, synthetic_run, lid) == 2
    rec(repo, synthetic_run, explanation(lid, "T1", "unknown"))  # 直した
    with pytest.raises(KizashiError) as info:
        render.render_lead(repo, synthetic_run, lid)
    assert (info.value.code, info.value.stage) == ("E_ROUNDS_EXCEEDED", "render")
    assert "保留" in info.value.next_action
    assert memo_files(repo, synthetic_run) == []
    rec(repo, synthetic_run, conclusion(lid, "hold"))
    assert render.render_lead(repo, synthetic_run, lid)["state"] == "hold"


def test_ck3_render_after_limit_writes_passed_second_round(repo: Path, synthetic_run: str) -> None:
    """CK-3（D-056②）：2巡目の check に合格した中身は、上限に達していても書ける（同じ中身を書き直すのも可）。
    合格の後に台帳を直した中身は書かない。"""
    lid = prepare(repo, synthetic_run)["lead"]
    check.run_check(repo, synthetic_run, lid)
    assert check.run_check(repo, synthetic_run, lid)["rounds"] == 2
    assert render.render_lead(repo, synthetic_run, lid)["memo_no"] == 1
    assert render.render_lead(repo, synthetic_run, lid, public=True)["memo_no"] == 2
    rec(
        repo,
        synthetic_run,
        explanation(lid, "T1", "unknown", searched="公表元の告知を探し直した。"),
    )
    with pytest.raises(KizashiError) as info:
        render.render_lead(repo, synthetic_run, lid)
    assert info.value.code == "E_ROUNDS_EXCEEDED"
    assert memo_files(repo, synthetic_run) == [f"{lid}.v1.md", f"{lid}.v2.md"]
