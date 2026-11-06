"""失効（test_plan.md の HS-1〜HS-3。implementation_spec.md 3.5・4.2（--recheck）・4.3・10.2・11章、設計書4.2）。

合成データの実行の検出の候補「翻訳業務」（S1・amount_up）を、test_check.py の prepare で比較成立の案にして
メモを書いてから、台帳を変える。行は S001:000012（令和6年度の競争入札）と S005:000007（令和7年度）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import URL, Site, fixture_bytes, ledger_of, rec
from kizashi import cli, fetcher, inspect_cmd, render
from kizashi.errors import KizashiError
from test_check import ROW_FROM, explanation, prepare

EVIDENCE_URL = "https://www.cao.go.jp/test/translation-notice.html"
NOTICE = "翻訳業務の仕様は前年度と同じとした。"


def memo_of(repo: Path, run_id: str, lead_id: str) -> dict:
    return inspect_cmd.inspect(repo, lead_id, run_id)["memo"]


def tamper(repo: Path, run_id: str, sql: str, params: tuple = ()) -> None:
    conn = ledger_of(repo, run_id)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


@pytest.fixture
def rendered(repo: Path, synthetic_run: str) -> dict:
    lead = prepare(repo, synthetic_run)
    render.render_lead(repo, synthetic_run, lead["lead"])
    assert memo_of(repo, synthetic_run, lead["lead"])["status"] == "有効"
    return lead


# ハッシュの対象（11章の1）のそれぞれを1つずつ変える
HASHED = {
    "leads の列": "UPDATE leads SET note = 'メモの後に書き足した注記' WHERE lead_id = :lead",
    "leads の状態": "UPDATE leads SET state = 'investigating' WHERE lead_id = :lead",
    "主張": "UPDATE claims SET text = text || '（直した）' WHERE lead_id = :lead AND type = 'observation'",
    "判定": "UPDATE explanations SET searched = searched || '（直した）' WHERE lead_id = :lead AND type = 'T1'",
    "計算": "UPDATE computations SET meaning = meaning || '（直した）' WHERE calc_id = :calc",
    "レコード": f"UPDATE records SET cells_sha256 = '{'0' * 64}' WHERE record_id = '{ROW_FROM}'",
    "対応表": "UPDATE procurement_matches SET reason = reason || '（直した）' WHERE lead_id = :lead",
    "資料の版": f"UPDATE sources SET sha256 = '{'1' * 64}' WHERE source_id = 'S001'",
    "規則の版": "UPDATE runs SET rule_versions_json = json_set(rule_versions_json, '$.rules_version', '0')",
}


@pytest.mark.parametrize("target", sorted(HASHED))
def test_hs1_any_hashed_row_makes_memo_stale(
    repo: Path, synthetic_run: str, rendered: dict, target: str
) -> None:
    """HS-1：ハッシュの対象のどれを変えても、inspect がメモを「失効」と表示する。"""
    before = memo_of(repo, synthetic_run, rendered["lead"])
    tamper(
        repo,
        synthetic_run,
        HASHED[target],
        {"lead": rendered["lead"], "calc": rendered["calc_from"]},
    )
    after = memo_of(repo, synthetic_run, rendered["lead"])
    assert after["status"] == "失効" and after["stale"] is True
    assert after["check_hash"] == before["check_hash"] != after["current_hash"]
    assert "check と render をやり直す" in after["note"]


@pytest.mark.parametrize(
    "columns",
    [("reflected_conditions_json",), ("residual_calc_id", "reflected_conditions_json")],
    ids=["v11", "v9"],
)
def test_hs1_old_ledger_memo_status_without_later_columns(
    repo: Path, synthetic_run: str, rendered: dict, columns: tuple[str, ...]
) -> None:
    """HS-1（D-047）：判定の表に後の版で足した列（residual_calc_id は版10、reflected_conditions_json は版12）の
    ない古い版の台帳でも、inspect の失効の表示は失敗せず、台帳を書き換えない。台帳の形と規則の版を変えたので
    記録したハッシュとは違い「失効」。その台帳でメモを書いた状態（記録したハッシュ＝その台帳のハッシュ）なら
    「有効」で、主張を変えると「失効」。"""
    from test_inspect import _drop_explanation_columns

    lead = rendered["lead"]
    _drop_explanation_columns(repo, synthetic_run, columns)
    conn = ledger_of(repo, synthetic_run)
    before = list(conn.iterdump())
    conn.close()

    shown = memo_of(repo, synthetic_run, lead)
    assert shown["status"] == "失効" and shown["check_hash"] != shown["current_hash"]
    conn = ledger_of(repo, synthetic_run)
    assert list(conn.iterdump()) == before  # 列を足して書き換えていない
    conn.close()

    tamper(
        repo,
        synthetic_run,
        "UPDATE leads SET memo_hash = ? WHERE lead_id = ?",
        (shown["current_hash"], lead),
    )
    assert memo_of(repo, synthetic_run, lead)["status"] == "有効"
    tamper(repo, synthetic_run, HASHED["主張"], {"lead": lead})
    assert memo_of(repo, synthetic_run, lead)["status"] == "失効"


def test_hs1_excluded_columns_do_not_make_memo_stale(
    repo: Path, synthetic_run: str, rendered: dict
) -> None:
    """HS-1：ハッシュに入れない3列（memo_no・memo_hash・rounds）だけを変えても、計算し直したハッシュは
    変わらず、rounds と memo_no を変えても「有効」のまま。"""
    lead = rendered["lead"]
    before = memo_of(repo, synthetic_run, lead)
    tamper(
        repo, synthetic_run, "UPDATE leads SET rounds = 7, memo_no = 1 WHERE lead_id = ?", (lead,)
    )
    assert memo_of(repo, synthetic_run, lead)["status"] == "有効"
    tamper(repo, synthetic_run, "UPDATE leads SET memo_hash = 'x' WHERE lead_id = ?", (lead,))
    assert memo_of(repo, synthetic_run, lead)["current_hash"] == before["current_hash"]


def test_hs1_withdrawn_claim_then_check_and_render_make_new_memo(
    repo: Path, synthetic_run: str, rendered: dict, capsys
) -> None:
    """HS-1（P0-8 の完了条件の形）：主張を1つ取り下げると失効と表示され、check と render をやり直すと
    新しい番号のメモ（v2）ができ、v1 は残る。"""
    lead = rendered["lead"]
    rec(
        repo,
        synthetic_run,
        {"kind": "withdraw", "target": rendered["record"], "reason": "言い過ぎた"},
    )
    assert memo_of(repo, synthetic_run, lead)["status"] == "失効"
    assert cli.main(["check", lead, "--run", synthetic_run]) == 0
    assert cli.main(["render", lead, "--run", synthetic_run]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["data"]["memo_no"] == 2
    memos = repo / "data" / "runs" / synthetic_run / "memos"
    assert (memos / f"{lead}.v1.md").is_file() and (memos / f"{lead}.v2.md").is_file()
    view = memo_of(repo, synthetic_run, lead)
    assert view["memo_no"] == 2 and view["status"] == "有効"
    assert view["file"] == f"data/runs/{synthetic_run}/memos/{lead}.v2.md"


def test_hs2_rule_version_drift_stops_check_and_render(
    repo: Path, synthetic_run: str, rendered: dict
) -> None:
    """HS-2：いまの規則の版が実行の記録と違えば、check と render（候補・実行の概要）が E_RULE_VERSION（終了コード
    3）で止まり、メモを書かない。inspect は止めずに失効の有無を出す（規則の版の食い違いとは分ける）。"""
    lead = rendered["lead"]
    sql = repo / "datasets" / "cao-procurement" / "screens" / "s1_amount_change.sql"
    sql.write_text(sql.read_text(encoding="utf-8") + "\n-- 変更\n", encoding="utf-8")
    for call in (
        lambda: cli_check(repo, synthetic_run, lead),
        lambda: render.render_lead(repo, synthetic_run, lead),
        lambda: render.render_run(repo, synthetic_run),
    ):
        with pytest.raises(KizashiError) as info:
            call()
        assert info.value.code == "E_RULE_VERSION" and info.value.exit_code == 3
        assert "sql_sha256" in info.value.message
    memos = repo / "data" / "runs" / synthetic_run / "memos"
    assert sorted(p.name for p in memos.iterdir()) == [f"{lead}.v1.md"]
    assert memo_of(repo, synthetic_run, lead)["status"] == "有効"


def cli_check(repo: Path, run_id: str, lead_id: str) -> dict:
    from kizashi import check

    return check.run_check(repo, run_id, lead_id)


# ---------------------------------------------------------------------------
# HS-3：fetch --recheck
# ---------------------------------------------------------------------------


def html(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=f"<html><body><p>{text}</p></body></html>".encode(),
    )


def explained_lead(repo: Path, run_id: str, site: Site) -> tuple[dict, str, str]:
    """公表元の説明（追加資料の HTML の引用）を主張に足してメモを書いた候補。(候補, 資料ID, 主張ID)。"""
    lead = prepare(repo, run_id)
    site.routes[EVIDENCE_URL] = html(NOTICE)
    source = fetcher.fetch_evidence(
        repo, run_id, EVIDENCE_URL, "evidence", transport=site.transport
    )
    claim = rec(
        repo,
        run_id,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "publisher_explanation",
            "text": "担当室は、翻訳業務の仕様を前年度と同じとしたと説明している。",
            "calc_ids": [],
            "evidence": [
                {
                    "source_id": source["source_id"],
                    "position": {"page": 1},
                    "quote": NOTICE,
                    "role": "support",
                }
            ],
        },
    )["id"]
    render.render_lead(repo, run_id, lead["lead"])
    return lead, source["source_id"], claim


def test_hs3_recheck_same_hash_changes_nothing(repo: Path, synthetic_run: str, site: Site) -> None:
    """HS-3：取り直してハッシュが同じなら、資料も主張も変えない（通信は数え、追加資料の数には数えない）。"""
    lead, source_id, claim = explained_lead(repo, synthetic_run, site)
    conn = ledger_of(repo, synthetic_run)
    sources_before = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    budget_before = json.loads(conn.execute("SELECT limits_json FROM runs").fetchone()[0])
    conn.close()
    result = fetcher.recheck_source(repo, synthetic_run, source_id, transport=site.transport)
    assert result["changed"] is False and result["new_source_id"] is None
    conn = ledger_of(repo, synthetic_run)
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == sources_before
    assert (
        conn.execute("SELECT state FROM claims WHERE claim_id = ?", (claim,)).fetchone()[0]
        == "checked"
    )
    budget = json.loads(conn.execute("SELECT limits_json FROM runs").fetchone()[0])
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'recheck'").fetchone()[0] == 0
    conn.close()
    assert budget["http_requests"] == budget_before["http_requests"] + 1
    assert budget["evidence_fetches"] == budget_before["evidence_fetches"]
    assert memo_of(repo, synthetic_run, lead["lead"])["status"] == "有効"


def test_hs3_new_version_makes_claims_stale_and_blocks_render(
    repo: Path, synthetic_run: str, site: Site
) -> None:
    """HS-3：新しい版を取ると新しい資料ID（supersedes に古い資料ID）で登録し、古い版に依存する主張を stale に
    し、events に recheck を残す。stale の主張を含む候補は check を通らず、render できない。"""
    lead, source_id, claim = explained_lead(repo, synthetic_run, site)
    site.routes[EVIDENCE_URL] = html(NOTICE + "（令和7年度に改めた）")
    result = fetcher.recheck_source(repo, synthetic_run, source_id, transport=site.transport)
    assert result["changed"] is True and result["stale_claims"] == [claim]
    new_id = result["new_source_id"]
    conn = ledger_of(repo, synthetic_run)
    row = conn.execute(
        "SELECT kind, supersedes, url, status FROM sources WHERE source_id = ?", (new_id,)
    ).fetchone()
    assert tuple(row) == ("evidence", source_id, EVIDENCE_URL, "fetched")
    assert (
        conn.execute("SELECT state FROM claims WHERE claim_id = ?", (claim,)).fetchone()[0]
        == "stale"
    )
    (after,) = conn.execute("SELECT after_json FROM events WHERE kind = 'recheck'").fetchone()
    conn.close()
    assert json.loads(after)["source_id"] == new_id
    pages = repo / "data" / "runs" / synthetic_run / "evidence" / new_id / "pages" / "1.txt"
    assert "令和7年度に改めた" in pages.read_text(encoding="utf-8")
    assert memo_of(repo, synthetic_run, lead["lead"])["status"] == "失効"
    with pytest.raises(KizashiError) as info:
        render.render_lead(repo, synthetic_run, lead["lead"])
    assert "V2_STALE_OR_WITHDRAWN" in [f["code"] for f in info.value.failures]
    assert inspect_cmd.inspect(repo, source_id, synthetic_run)["superseded_by"] == [new_id]
    with pytest.raises(KizashiError) as again:
        fetcher.recheck_source(repo, synthetic_run, source_id, transport=site.transport)
    assert again.value.code == "E_USAGE" and new_id in again.value.message


def test_hs3_dataset_recheck_follows_public_page(
    repo: Path, synthetic_run: str, site: Site
) -> None:
    """HS-3：公表一覧の資料は、公表ページから同じ論理資料のリンクをたどり直す。令和6年度の競争入札の CSV
    （S001）が差し替わると、その行を数えた計算を参照する主張（観測。S001:000012 を含む合計）が stale になる。
    新しい版は正規化しない（候補が依存する資料は元の版のまま）。"""
    lead = prepare(repo, synthetic_run)
    render.render_lead(repo, synthetic_run, lead["lead"])
    changed = fixture_bytes("kyoso_2024_mini.csv").replace(
        "令和７年７月11日現在".encode("cp932"), "令和７年８月11日現在".encode("cp932")
    )
    site.routes[URL["k24_csv"]] = httpx.Response(
        200, headers={"content-type": "text/csv"}, content=changed
    )
    result = fetcher.recheck_source(repo, synthetic_run, "S001", transport=site.transport)
    assert result["changed"] is True
    assert result["stale_claims"] == [lead["observation"]]
    conn = ledger_of(repo, synthetic_run)
    row = conn.execute(
        "SELECT logical_key, kind, supersedes, url FROM sources WHERE source_id = ?",
        (result["new_source_id"],),
    ).fetchone()
    assert tuple(row) == ("cao-procurement|2024|kyoso|csv", "dataset", "S001", URL["k24_csv"])
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM records WHERE source_id = ?", (result["new_source_id"],)
        ).fetchone()[0]
        == 0
    )
    conn.close()
    assert memo_of(repo, synthetic_run, lead["lead"])["status"] == "失効"
    assert site.requests[-2:] == ["https://www.cao.go.jp/chotatsu/kohyo/kohyo.html", URL["k24_csv"]]


CHANGED = "翻訳業務の仕様は前年度から変更した。"


def judged_lead(repo: Path, run_id: str, site: Site) -> tuple[dict, str]:
    """業務・数量の変化（T3）の判定だけが追加資料を根拠にした候補のメモ（主張は追加資料に依存しない）。"""
    lead = prepare(repo, run_id)
    site.routes[EVIDENCE_URL] = html(NOTICE)
    source_id = fetcher.fetch_evidence(
        repo, run_id, EVIDENCE_URL, "evidence", transport=site.transport
    )["source_id"]
    rec(repo, run_id, judgment_t3(lead["lead"], source_id, NOTICE))
    render.render_lead(repo, run_id, lead["lead"])
    return lead, source_id


def judgment_t3(lead_id: str, source_id: str, quote: str) -> dict:
    evidence = {"source_id": source_id, "position": {"page": 1}, "quote": quote, "role": "support"}
    return explanation(lead_id, "T3", "refute", searched="", evidence=[evidence])


def stale_source_failures(info: pytest.ExceptionInfo, source_id: str) -> list[dict]:
    return [
        f
        for f in info.value.failures
        if f["code"] == "V2_STALE_OR_WITHDRAWN" and source_id in f["affected_ids"]
    ]


def test_hs3_new_version_of_judgment_evidence_makes_memo_stale(
    repo: Path, synthetic_run: str, site: Site
) -> None:
    """HS-3（D-056①）：判定だけが根拠にした追加資料の新しい版を取ると、主張は stale にならないが、検証が依存する
    資料に新しい版があるので、メモは失効と表示され、check と render は V2_STALE_OR_WITHDRAWN で通らない。
    recheck の結果に、検証が古い版に依存する候補を出す。新しい版を根拠に判定を登録し直すと check を通り、新しい
    番号のメモができる。"""
    from kizashi import check

    lead, source_id = judged_lead(repo, synthetic_run, site)
    lid = lead["lead"]
    site.routes[EVIDENCE_URL] = html(CHANGED)
    result = fetcher.recheck_source(repo, synthetic_run, source_id, transport=site.transport)
    new_id = result["new_source_id"]
    assert result["changed"] is True and result["stale_claims"] == []
    assert result["affected_leads"] == [lid]
    assert lid in result["message"]
    view = memo_of(repo, synthetic_run, lid)
    assert view["status"] == "失効" and view["check_hash"] != view["current_hash"]
    for call in (check.run_check, render.render_lead):
        with pytest.raises(KizashiError) as info:
            call(repo, synthetic_run, lid)
        [failure] = stale_source_failures(info, source_id)
        assert new_id in failure["affected_ids"] and new_id in failure["message"]
    memos = repo / "data" / "runs" / synthetic_run / "memos"
    assert sorted(p.name for p in memos.iterdir()) == [f"{lid}.v1.md"]

    rec(repo, synthetic_run, judgment_t3(lid, new_id, CHANGED))
    assert check.run_check(repo, synthetic_run, lid)["passed"] is True
    assert render.render_lead(repo, synthetic_run, lid)["memo_no"] == 2
    assert memo_of(repo, synthetic_run, lid)["status"] == "有効"


def test_hs3_new_version_of_zero_row_dependency_csv_makes_memo_stale(
    repo: Path, synthetic_run: str, site: Site
) -> None:
    """HS-3（D-056①）：候補の行のない依存する CSV（令和6年度の随意契約 S003。翻訳業務の行は競争入札の S001・
    S005 にだけある）の新しい版でも、メモは失効と表示され、check は通らない（この実行の計算は古い版による
    ので、新しい実行で調べ直す）。主張は stale にならない。"""
    from kizashi import check

    lead = prepare(repo, synthetic_run)
    lid = lead["lead"]
    render.render_lead(repo, synthetic_run, lid)
    site.routes[URL["z24_csv"]] = httpx.Response(
        200,
        headers={"content-type": "text/csv"},
        content=fixture_bytes("zuikei_2024_mini.csv") + b"\r\n",
    )
    result = fetcher.recheck_source(repo, synthetic_run, "S003", transport=site.transport)
    assert result["changed"] is True and result["stale_claims"] == []
    assert result["affected_leads"] == [lid]
    assert memo_of(repo, synthetic_run, lid)["status"] == "失効"
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, synthetic_run, lid)
    [failure] = stale_source_failures(info, "S003")
    assert result["new_source_id"] in failure["affected_ids"]
    assert "新しい実行" in failure["message"]
