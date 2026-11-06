"""inspect の表示（implementation_spec.md 4.3、3.2 の1ページ20件、D-023）。

合成データの実行（S001〜S009、照合の計算 K0001〜K0008、検出の候補 L0001〜L0016 と計算 K0009〜K0080）に、
record で候補（L0017 から）・計算（K0081 から）・対応・係数・主張を足して確かめる。期待値は合成の CSV（tests/fixtures/make_cao_fixtures.py）と、このテストで登録した値から決めた。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import add_evidence, all_text_in_ledger, ledger_of, make_lead, rec
from kizashi import inspect_cmd
from kizashi.errors import KizashiError


@pytest.fixture
def filled(repo: Path, synthetic_run: str) -> dict:
    ref = repo / "references"
    ref.mkdir()
    (ref / "investigative_reporting_playbook_v13.0.md").write_text(
        "### C900\n②契約金額を税込か確認\n", encoding="utf-8"
    )
    run_id = synthetic_run
    lead = make_lead(repo, run_id)
    lid = lead["lead"]
    rec(
        repo,
        run_id,
        {
            "kind": "hypothesis",
            "lead_id": lid,
            "text": "量が増えた可能性がある。",
            "withdraw_if": "量が同じと分かった場合。",
        },
    )
    rec(
        repo,
        run_id,
        {
            "kind": "match",
            "lead_id": lid,
            "left": ["S001:000012"],
            "right": ["S005:000007"],
            "relation": "1:1",
            "reason": "同じ翻訳業務。",
            "evidence": [],
            "status": "confirmed",
        },
    )
    rec(
        repo,
        run_id,
        {
            "kind": "coefficient",
            "lead_id": lid,
            "key": "tax_basis",
            "value": "1.1",
            "meaning": "税抜きを税込みにする係数",
            "why_needed": "税込みかが分からない",
            "apply_to": "令和6年度の合計に掛ける",
            "basis": [{"file": "playbook", "locator": "C900", "quote": "契約金額を税込か確認"}],
            "status": "proposed",
        },
    )
    claim = rec(
        repo,
        run_id,
        {
            "kind": "claim",
            "lead_id": lid,
            "type": "observation",
            "text": f"令和7年度は{{calc:{lead['to']}}}である。",
            "calc_ids": [lead["to"]],
            "evidence": [],
        },
    )
    return {"run_id": run_id, **lead, "claim": claim["id"]}


def show(repo: Path, target: str, run_id: str, page: int = 1) -> dict:
    return inspect_cmd.inspect(repo, target, run_id, page=page)


def test_inspect_run(repo: Path, filled: dict) -> None:
    """4.3 実行ID：問い、状態、工程、件数、上限の消費、候補の一覧（総数と未調査）、次の作業。"""
    data = show(repo, filled["run_id"], filled["run_id"])
    assert data["question"].startswith("同種の業務で") and data["state"] == "completed"
    assert list(data["stages"]) == [
        "plan",
        "fetch",
        "normalize",
        "quality",
        "reconcile_import",
        "reconcile_source",
        "detect",
    ]
    assert data["counts"]["explore"] == {"compared": 1}
    assert data["counts"]["s3_reason_change"] == {
        "state": "done",
        "compared": 5,
        "incomparable": 0,
        "leads": 1,
    }
    assert data["limits"]["deep_leads"] == {"used": 1, "max": 3}
    assert data["limits"]["evidence_fetches"] == {"used": 0, "max": 12}
    assert data["limits"]["http_requests"] == {"used": 9, "max": 40}
    leads = data["leads"]
    # 検出の候補16件（未調査）と、探索の候補1件（調査中）
    assert (leads["total"], leads["uninvestigated"], leads["page"]) == (17, 16, 1)
    assert len(leads["items"]) == 17
    [item] = [x for x in leads["items"] if x["screen_id"] == "explore"]
    assert (item["lead_id"], item["screen_id"], item["state"]) == (
        "L0017",
        "explore",
        "investigating",
    )
    assert (item["calc_from"]["value"], item["calc_to"]["value"]) == ("650000", "750000")
    assert "next_page" not in data and data["next_actions"]


def test_inspect_run_paging_and_order(repo: Path, synthetic_run: str) -> None:
    """3.2・4.3：候補は区分ごとに差の絶対値の大きい順、1ページ20件。超えた分は next_page。"""
    conn = sqlite3.connect(repo / "data" / "runs" / synthetic_run / "ledger.sqlite")
    # 検出（detect）の候補と計算を除き、このテストで入れる22件だけにする
    conn.execute("DELETE FROM computations WHERE lead_id IS NOT NULL")
    conn.execute("DELETE FROM leads")
    for i in range(22):
        k_from, k_to = f"K{100 + 2 * i:04d}", f"K{101 + 2 * i:04d}"
        for cid, value in ((k_from, "1000"), (k_to, str(1000 + (i * 7) % 22 * 100))):
            conn.execute(
                "INSERT INTO computations (calc_id, kind, request_json, value, unit, meaning, origin,"
                " created_at) VALUES (?, 'base', '{}', ?, '円', 'テスト', 'run', 'now')",
                (cid, value),
            )
        conn.execute(
            "INSERT INTO leads (lead_id, screen_id, lead_key, change_category, period_from, period_to,"
            " calc_from, calc_to) VALUES (?, 's1_amount_change', ?, ?, '2024', '2025', ?, ?)",
            (f"L{i + 1:04d}", f"業務{i}", "amount_up" if i % 2 else "amount_down", k_from, k_to),
        )
    conn.commit()
    conn.close()
    first = show(repo, synthetic_run, synthetic_run)
    second = show(repo, synthetic_run, synthetic_run, page=2)
    assert (first["leads"]["total"], first["leads"]["uninvestigated"]) == (22, 22)
    assert len(first["leads"]["items"]) == 20 and first["next_page"] == 2
    assert len(second["leads"]["items"]) == 2 and "next_page" not in second
    items = first["leads"]["items"] + second["leads"]["items"]
    # 手で並べた順：amount_down（i が偶数）→ amount_up（i が奇数）、それぞれ差（(i×7 mod 22)×100）の大きい順
    expected = sorted(
        range(22), key=lambda i: ("amount_up" if i % 2 else "amount_down", -((i * 7) % 22), i)
    )
    assert [x["lead_id"] for x in items] == [f"L{i + 1:04d}" for i in expected]


def test_inspect_lead_shows_matches_and_coefficients(repo: Path, filled: dict) -> None:
    """4.3 候補ID（D-023）：計算、比較の条件、仮説、主張、同種業務の対応、係数の提案、メモの番号。"""
    data = show(repo, filled["lead"], filled["run_id"])
    assert (data["lead_id"], data["screen_id"], data["query_file"]) == (
        "L0017",
        "explore",
        "q-001.sql",
    )
    assert data["calc_from"] == {
        "calc_id": filled["from"],
        "kind": "base",
        "value": "650000",
        "unit": "円",
        "meaning": "翻訳業務の契約金額の合計",
        "state": "active",
        "row_count": 1,
    }
    assert data["comparison"]["row_meaning"] == "1件の契約"
    assert data["hypothesis"]["text"] == "量が増えた可能性がある。"
    assert [(m["match_id"], m["status"], m["left"], m["right"]) for m in data["matches"]] == [
        ("M001", "confirmed", ["S001:000012"], ["S005:000007"])
    ]
    assert [(c["key"], c["value"], c["status"]) for c in data["coefficients"]] == [
        ("tax_basis", "1.1", "proposed")
    ]
    assert [(c["claim_id"], c["state"]) for c in data["claims"]] == [(filled["claim"], "draft")]
    assert data["memo"] == {"memo_no": 0, "file": None, "stale": None, "status": "メモなし"}
    assert (data["state"], data["rounds"]) == ("investigating", 0)


def test_inspect_lead_shows_residual_and_reflected_conditions(repo: Path, filled: dict) -> None:
    """4.3 候補ID（D-045③）：判定ごとに、説明を反映した値（recalc_calc_id）、残る差（residual_calc_id）、説明に
    よって足した・変えた条件（reflected_conditions）を出す。登録した値がそのまま読める。書いていない判定では空。"""
    run_id, lid = filled["run_id"], filled["lead"]
    count = rec(
        repo,
        run_id,
        {
            "kind": "calc",
            "lead_id": lid,
            "op": "count",
            "table": "procurement_contracts",
            "filter": [
                {"col": "fiscal_year", "op": "=", "value": 2025},
                {"col": "group_key", "op": "=", "value": "翻訳業務"},
                {"col": "unit_price", "op": "=", "value": 0},
            ],
            "label": "令和7年度の単価契約でない行の件数",
            "unit": "件",
        },
    )["id"]
    reflected = [{"col": "unit_price", "op": "=", "value": 0}]
    rec(
        repo,
        run_id,
        {
            "kind": "explanation",
            "lead_id": lid,
            "type": "T3",
            "judgment": "support",
            "scope": "part",
            "recalc_calc_id": count,
            "residual_calc_id": count,
            "reflected_conditions": reflected,
            "evidence": [],
            "searched": "仕様書を探した。",
        },
    )
    rec(
        repo,
        run_id,
        {
            "kind": "explanation",
            "lead_id": lid,
            "type": "T1",
            "judgment": "unknown",
            "evidence": [],
            "searched": "公表元の告知を探した。",
        },
    )
    data = show(repo, lid, run_id)
    by_type = {e["type"]: e for e in data["explanations"]}
    assert (
        by_type["T3"]["recalc_calc_id"],
        by_type["T3"]["residual_calc_id"],
        by_type["T3"]["reflected_conditions"],
    ) == (count, count, reflected)
    assert (
        by_type["T1"]["recalc_calc_id"],
        by_type["T1"]["residual_calc_id"],
        by_type["T1"]["reflected_conditions"],
    ) == (None, None, None)


def _drop_explanation_columns(repo: Path, run_id: str, columns: tuple[str, ...]) -> None:
    """explanations の表を、columns の列のない古い版の形に作り直す（residual_calc_id は版10、
    reflected_conditions_json は版12で足した列）。外部キーのある列は DROP COLUMN できないので作り直す。"""
    conn = ledger_of(repo, run_id)
    keep = [r[1] for r in conn.execute("PRAGMA table_info(explanations)") if r[1] not in columns]
    names = ", ".join(keep)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f"CREATE TABLE explanations_old AS SELECT {names} FROM explanations")
    conn.execute("DROP TABLE explanations")
    conn.execute("ALTER TABLE explanations_old RENAME TO explanations")
    conn.execute(
        "UPDATE runs SET rule_versions_json = json_set(rule_versions_json, '$.rules_version', '11')"
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "columns",
    [("reflected_conditions_json",), ("residual_calc_id", "reflected_conditions_json")],
    ids=["v11", "v9"],
)
def test_inspect_old_ledger_shows_missing_columns_as_unrecorded(
    repo: Path, filled: dict, columns: tuple[str, ...]
) -> None:
    """4.3（D-047）：判定の表示に足した列（D-045③）のない古い版の台帳でも、inspect は失敗せず、その項目を未記録
    （値は null、unrecorded に項目の名前）として出す。台帳は書き換えない。版の違う台帳への check は、これとは別に
    E_RULE_VERSION で拒否する。"""
    from kizashi import check

    run_id, lid = filled["run_id"], filled["lead"]
    rec(
        repo,
        run_id,
        {
            "kind": "explanation",
            "lead_id": lid,
            "type": "T1",
            "judgment": "unknown",
            "evidence": [],
            "searched": "公表元の告知を探した。",
        },
    )
    _drop_explanation_columns(repo, run_id, columns)
    before = all_text_in_ledger(ledger_of(repo, run_id))
    names = {
        "residual_calc_id": "residual_calc_id",
        "reflected_conditions_json": "reflected_conditions",
    }

    [t1] = show(repo, lid, run_id)["explanations"]
    assert (t1["type"], t1["judgment"], t1["recalc_calc_id"]) == ("T1", "unknown", None)
    assert t1["unrecorded"] == [names[c] for c in columns]
    assert all(t1[names[c]] is None for c in columns)
    conn = ledger_of(repo, run_id)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(explanations)")}
    assert not cols & set(columns)  # 列を足して書き換えていない
    assert all_text_in_ledger(conn) == before
    with pytest.raises(KizashiError) as info:
        check.run_check(repo, run_id, lid)
    assert (info.value.code, info.value.exit_code) == ("E_RULE_VERSION", 3)


def test_inspect_source_row_calc_claim_match(repo: Path, filled: dict) -> None:
    """4.3 資料ID・row_ref（take のキーと見出し、正規化した値、照合の結果）・計算ID・主張ID・対応ID。"""
    run_id = filled["run_id"]
    src = show(repo, "S005", run_id)
    assert (src["source_id"], src["kind"], src["status"], src["logical_key"]) == (
        "S005",
        "dataset",
        "fetched",
        "cao-procurement|2025|kyoso|csv",
    )
    assert len(src["sha256"]) == 64 and src["superseded_by"] == []

    row = show(repo, "S005:000007", run_id)
    cells = {c["key"]: (c["header"], c["value"]) for c in row["cells"]}
    assert cells["amount_raw"] == ("契約金額", "750,000")
    assert cells["title_raw"] == ("物品役務等の名称及び数量", "翻訳業務")
    assert "officer_raw" not in cells
    assert [c["key"] for c in row["cells"]] == [  # 原本の列の順（dataset.yaml の kyoso_16 の take）
        "no", "title_raw", "contract_date_raw", "counterparty_raw", "corp_number_raw", "method_raw",
        "estimate_raw", "amount_raw", "rate_raw", "note_raw",
    ]  # fmt: skip
    assert row["domain"]["amount_yen"] == 750000 and row["domain"]["group_key"] == "翻訳業務"
    assert row["reconcile"]["import"]["row_status"] == "match"
    assert row["reconcile"]["source"]["row_status"] == "matched"
    assert row["reconcile"]["source"]["pdf_page"] >= 1

    calc = show(repo, filled["from"], run_id)
    assert (calc["kind"], calc["value"], calc["row_count"], calc["row_refs"]) == (
        "base",
        "650000",
        1,
        ["S001:000012"],
    )
    claim = show(repo, filled["claim"], run_id)
    assert (claim["type"], claim["state"], claim["calc_ids"]) == (
        "observation",
        "draft",
        [filled["to"]],
    )
    match = show(repo, "M001", run_id)
    assert (match["relation"], match["status"]) == ("1:1", "confirmed")


def test_inspect_calc_paging_and_reconcile(repo: Path, synthetic_run: str) -> None:
    """4.3 計算ID：row_refs の件数とハッシュ、一覧はページ分け。照合の計算はその行の結果だけを出す。"""
    rec(
        repo,
        synthetic_run,
        {
            "kind": "calc",
            "op": "count",
            "table": "procurement_contracts",
            "filter": [{"col": "fiscal_year", "op": "=", "value": 2024}],
            "label": "令和6年度の件数",
            "unit": "件",
        },
    )
    first = show(repo, "K0081", synthetic_run)  # 検出の計算（K0009〜K0080）の次
    second = show(repo, "K0081", synthetic_run, page=2)
    assert (first["value"], first["row_count"], len(first["row_refs"]), first["next_page"]) == (
        "21",
        21,
        20,
        2,
    )
    assert second["row_refs"] == ["S003:000009"] and "next_page" not in second
    recon = show(repo, "K0001", synthetic_run)  # S001 の取り込みの照合（12行）
    assert recon["kind"] == "reconcile" and recon["result"]["type"] == "import"
    assert recon["result"]["counts"]["match"] == 12
    assert sorted(recon["result"]["rows"]) == recon["row_refs"] and len(recon["row_refs"]) == 12


def test_inspect_does_not_change_ledger(repo: Path, filled: dict) -> None:
    """4.3：inspect は台帳を変えない（読み取り専用の接続）。"""
    run_id = filled["run_id"]
    before = all_text_in_ledger(ledger_of(repo, run_id))
    for target in (
        run_id,
        filled["lead"],
        "S005",
        "S005:000007",
        filled["from"],
        filled["claim"],
        "M001",
    ):
        show(repo, target, run_id)
    assert all_text_in_ledger(ledger_of(repo, run_id)) == before


def test_inspect_evidence_source_and_missing_ids(repo: Path, synthetic_run: str) -> None:
    """4.3：追加資料のページ数。ない ID は E_REF_NOT_FOUND。"""
    sid = add_evidence(repo, synthetic_run, ["一", "二"])
    assert show(repo, sid, synthetic_run)["page_count"] == 2
    for target in ("L0099", "S099", "S005:000099", "K0999", "C0099", "M099"):
        with pytest.raises(KizashiError) as info:
            show(repo, target, synthetic_run)
        assert info.value.code == "E_REF_NOT_FOUND"
    with pytest.raises(KizashiError) as info:
        show(repo, "20261105-000000-none", "20261105-000000-none")
    assert info.value.code == "E_REF_NOT_FOUND"
