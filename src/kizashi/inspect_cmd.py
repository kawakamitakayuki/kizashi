"""inspect の表示（implementation_spec.md 2章の inspect_cmd.py、4.3、3.2）。

kizashi inspect <run_id> [--page <n>]
kizashi inspect <ID> --run <run_id> [--page <n>]

- ID の接頭辞で対象を決める（実行・候補・資料・レコード・計算・主張・対応）。台帳は読み取り専用の接続で開き、
  変えない。
- 一覧は1ページ20件。超えた分は next_page を返す。本文の抜粋は1件2,000字まで。
- 候補IDでは、同種業務の対応（matches）と係数の提案（coefficients）も出す（D-023）。
- メモの失効の表示は P0-8 で仕上げる。いまは memo_hash の有無だけを出す。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from kizashi import ledger, limits, paths, runner
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

STAGE = "inspect"
PAGE_SIZE = 20
EXCERPT_CHARS = 2000


def _not_found(what: str, target: str, run_id: str) -> KizashiError:
    return KizashiError(
        "E_REF_NOT_FOUND",
        STAGE,
        f"{what}がない：{target}",
        affected_ids=[target],
        next_action=f"kizashi inspect {run_id} で ID を確かめる。",
    )


def _excerpt(text: str | None) -> str | None:
    if text is None or len(text) <= EXCERPT_CHARS:
        return text
    return text[:EXCERPT_CHARS] + f"…（以下略。全{len(text)}字）"


def _page(items: list, page: int) -> tuple[list, int | None]:
    start = (page - 1) * PAGE_SIZE
    chunk = items[start : start + PAGE_SIZE]
    return chunk, (page + 1 if len(items) > start + PAGE_SIZE else None)


def _with_next(data: dict, next_page: int | None) -> dict:
    if next_page is not None:
        data["next_page"] = next_page
    return data


def _row(conn: sqlite3.Connection, sql: str, params: tuple) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def _calc_summary(conn: sqlite3.Connection, calc_id: str | None) -> dict | None:
    if calc_id is None:
        return None
    row = _row(conn, "SELECT * FROM computations WHERE calc_id = ?", (calc_id,))
    if row is None:
        return {"calc_id": calc_id, "missing": True}
    return {
        "calc_id": row["calc_id"],
        "kind": row["kind"],
        "value": row["value"],
        "unit": row["unit"],
        "meaning": row["meaning"],
        "state": row["state"],
        "row_count": row["row_count"],
    }


# ---------------------------------------------------------------------------
# 実行ID
# ---------------------------------------------------------------------------


def _abs_diff(conn: sqlite3.Connection, lead: sqlite3.Row) -> Decimal | None:
    values = []
    for cid in (lead["calc_from"], lead["calc_to"]):
        row = (
            None
            if cid is None
            else _row(conn, "SELECT value FROM computations WHERE calc_id = ?", (cid,))
        )
        if row is None or row[0] is None:
            return None
        values.append(Decimal(row[0]))
    return abs(values[1] - values[0])


def _next_actions(run: sqlite3.Row, stages: dict, leads: list[sqlite3.Row]) -> list[str]:
    run_id = run["run_id"]
    actions: list[str] = []
    if run["state"] == "failed":
        actions.append("実行は failed。stop_reason を記者に伝え、新しい実行を作る（kizashi run）。")
        return actions
    if run["state"] == "running":
        actions.append(f"実行が途中で止まっている。kizashi run --resume {run_id} で続ける。")
        return actions
    if run["state"] == "partial":
        actions.append(
            "partial：取得できなかった資料がある。stop_reason を記者に伝える（候補なしと書かない）。"
        )
    if "detect" not in stages:
        actions.append(
            "検出（detect）は実行していない。探索SQL（kizashi query）で変化を探し、calc で計算してから"
            " kind: lead で登録できる。"
        )
    investigating = [x for x in leads if x["state"] == "investigating"]
    if leads and not investigating:
        actions.append(
            f"候補を kizashi inspect <lead_id> --run {run_id} で読み、詳しく調べる候補"
            f"（最大{limits.MAX_DEEP_LEADS}件）に kind: hypothesis を登録する。"
        )
    for lead in investigating:
        if lead["proposed_outcome"] is None:
            actions.append(
                f"{lead['lead_id']}：6つの型を当て、kind: conclusion を登録して check する。"
            )
    return actions


def _inspect_run(conn: sqlite3.Connection, run_id: str, page: int) -> dict:
    run = _row(conn, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        raise _not_found("実行の記録", run_id, run_id)
    config = json.loads(run["config_json"])
    stored = json.loads(run["stages_json"])  # キーを並べ替えて保存してある。工程の順に並べ直す
    order = [*runner.STAGES, *runner.STAGES_NOT_RUN]
    stages = {
        k: stored[k]
        for k in sorted(
            stored, key=lambda k: (k not in order, order.index(k) if k in order else 0, k)
        )
    }
    budget = json.loads(run["limits_json"])
    leads = conn.execute("SELECT * FROM leads").fetchall()
    keyed = []
    for lead in leads:
        diff = _abs_diff(conn, lead)
        keyed.append(((lead["change_category"], diff is None, -(diff or 0), lead["lead_id"]), lead))
    ordered = [lead for _, lead in sorted(keyed, key=lambda x: x[0])]
    chunk, next_page = _page(ordered, page)
    items = [
        {
            "lead_id": lead["lead_id"],
            "screen_id": lead["screen_id"],
            "change_category": lead["change_category"],
            "lead_key": lead["lead_key"],
            "period_from": lead["period_from"],
            "period_to": lead["period_to"],
            "calc_from": _calc_summary(conn, lead["calc_from"]),
            "calc_to": _calc_summary(conn, lead["calc_to"]),
            "peer_count": lead["peer_count"],
            "incomparable_reasons": json.loads(lead["incomparable_reasons_json"]),
            "state": lead["state"],
        }
        for lead in chunk
    ]
    investigated = sum(1 for lead in leads if lead["hypothesis"] is not None)
    data = {
        "run_id": run_id,
        "domain": run["domain"],
        "question": config.get("question"),
        "compare_years": config.get("compare_years"),
        "series_years": config.get("series_years"),
        "scope": config.get("scope"),
        "state": run["state"],
        "stop_reason": None if run["stop_reason"] is None else json.loads(run["stop_reason"]),
        "profile": run["profile"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "stages": stages,
        "counts": json.loads(run["counts_json"]),
        "limits": {
            "deep_leads": {"used": investigated, "max": limits.MAX_DEEP_LEADS},
            "evidence_fetches": {
                "used": int(budget.get("evidence_fetches", 0)),
                "max": limits.MAX_EVIDENCE_FETCH,
            },
            "http_requests": {
                "used": int(budget.get("http_requests", 0)),
                "max": limits.MAX_HTTP_REQUESTS,
            },
            "bytes_total": {"used": int(budget.get("bytes_total", 0)), "max": limits.MAX_RUN_BYTES},
        },
        "quality_report": f"data/runs/{run_id}/quality_report.md",
        "leads": {
            "total": len(leads),
            "uninvestigated": sum(1 for lead in leads if lead["state"] == "candidate"),
            "page": page,
            "items": items,
        },
        "next_actions": _next_actions(run, stages, leads),
    }
    return _with_next(data, next_page)


# ---------------------------------------------------------------------------
# 候補ID
# ---------------------------------------------------------------------------


def _inspect_lead(conn: sqlite3.Connection, lead_id: str, run_id: str, page: int) -> dict:
    lead = _row(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    if lead is None:
        raise _not_found("候補", lead_id, run_id)
    claims = conn.execute(
        "SELECT * FROM claims WHERE lead_id = ? ORDER BY claim_id", (lead_id,)
    ).fetchall()
    chunk, next_page = _page(claims, page)
    explanations = conn.execute(
        "SELECT * FROM explanations WHERE lead_id = ? ORDER BY type", (lead_id,)
    ).fetchall()
    matches = conn.execute(
        "SELECT * FROM procurement_matches WHERE lead_id = ? ORDER BY match_id", (lead_id,)
    ).fetchall()
    series = json.loads(lead["calc_ids_json"])
    shown = {lead["calc_from"], lead["calc_to"], *series}
    others = [
        r[0]
        for r in conn.execute(
            "SELECT calc_id FROM computations WHERE lead_id = ? ORDER BY calc_id", (lead_id,)
        )
        if r[0] not in shown
    ]
    data = {
        "lead_id": lead_id,
        "screen_id": lead["screen_id"],
        "query_file": lead["query_file"],
        "lead_key": lead["lead_key"],
        "change_category": lead["change_category"],
        "metric": lead["metric"],
        "period_from": lead["period_from"],
        "period_to": lead["period_to"],
        "calc_from": _calc_summary(conn, lead["calc_from"]),
        "calc_to": _calc_summary(conn, lead["calc_to"]),
        "series": [_calc_summary(conn, c) for c in series],
        "other_calcs": [_calc_summary(conn, c) for c in others],
        "peer_count": lead["peer_count"],
        "comparison": json.loads(lead["comparison_json"]),
        "comparison_state": lead["comparison_state"],
        "adjustment": None
        if lead["adjustment_json"] is None
        else json.loads(lead["adjustment_json"]),
        "incomparable_reasons": json.loads(lead["incomparable_reasons_json"]),
        "note": _excerpt(lead["note"]),
        "hypothesis": {
            "text": _excerpt(lead["hypothesis"]),
            "withdraw_if": _excerpt(lead["withdraw_if"]),
            "result": lead["hypothesis_result"],
        },
        "explanations": [
            {
                "type": e["type"],
                "judgment": e["judgment"],
                "scope": e["scope"],
                "searched": _excerpt(e["searched"]),
                "evidence": json.loads(e["evidence_json"]),
                "recalc_calc_id": e["recalc_calc_id"],
                "no_quant_reason": _excerpt(e["no_quant_reason"]),
                "context": e["context"],
            }
            for e in explanations
        ],
        "claims": [
            {
                "claim_id": c["claim_id"],
                "type": c["type"],
                "fiscal_year": c["fiscal_year"],
                "state": c["state"],
                "text": _excerpt(c["text"]),
                "calc_ids": json.loads(c["calc_ids_json"]),
            }
            for c in chunk
        ],
        "questions": json.loads(lead["questions_json"]),
        "matches": [
            {
                "match_id": m["match_id"],
                "left": json.loads(m["left_json"]),
                "right": json.loads(m["right_json"]),
                "relation": m["relation"],
                "reason": _excerpt(m["reason"]),
                "status": m["status"],
            }
            for m in matches
        ],
        "coefficients": json.loads(lead["coefficients_json"]),
        "proposed_outcome": lead["proposed_outcome"],
        "explanation_status": lead["explanation_status"],
        "hold_reason": lead["hold_reason"],
        "reopen_condition": lead["reopen_condition"],
        "state": lead["state"],
        "rounds": lead["rounds"],
        "memo": {"memo_no": lead["memo_no"], "memo_hash": lead["memo_hash"] is not None},
    }
    return _with_next(data, next_page)


# ---------------------------------------------------------------------------
# 資料ID・レコード・計算ID・主張ID・対応ID
# ---------------------------------------------------------------------------


def _inspect_source(conn: sqlite3.Connection, root: Path, source_id: str, run_id: str) -> dict:
    row = _row(conn, "SELECT * FROM sources WHERE source_id = ?", (source_id,))
    if row is None:
        raise _not_found("資料", source_id, run_id)
    newer = [
        r[0]
        for r in conn.execute("SELECT source_id FROM sources WHERE supersedes = ?", (source_id,))
    ]
    pages_dir = paths.run_dir(root, run_id) / "evidence" / source_id / "pages"
    data = dict(row)
    data["superseded_by"] = newer
    if pages_dir.is_dir():
        data["pages_dir"] = pages_dir.relative_to(root).as_posix()
    elif row["kind"] == "dataset" and (row["logical_key"] or "").endswith("|pdf"):
        data["pages_dir"] = None
        data["note"] = (
            "公表一覧の PDF はページ別テキストを作らない（D-021）。CSV の row_ref で引用する。"
        )
    return data


def _reconcile_of(conn: sqlite3.Connection, row_ref: str) -> dict:
    found: dict[str, dict] = {}
    for calc_id, result_json in conn.execute(
        "SELECT calc_id, result_json FROM computations WHERE kind = 'reconcile' AND state = 'active'"
        " ORDER BY calc_id"
    ):
        result = json.loads(result_json)
        entry = result.get("rows", {}).get(row_ref)
        if entry is not None:
            found[result.get("type", "unknown")] = {"calc_id": calc_id, **entry}
    return found


def _inspect_record(conn: sqlite3.Connection, root: Path, row_ref: str, run_id: str) -> dict:
    rec = _row(conn, "SELECT * FROM records WHERE record_id = ?", (row_ref,))
    if rec is None:
        raise _not_found("レコード", row_ref, run_id)
    source = _row(conn, "SELECT logical_key FROM sources WHERE source_id = ?", (rec["source_id"],))
    run = _row(conn, "SELECT domain FROM runs WHERE run_id = ?", (run_id,))
    adapter = load_adapter(root, run["domain"])
    take: dict = {}
    if source is not None and source["logical_key"]:
        _, year, family, _ = source["logical_key"].split("|")
        spec = adapter.file_spec(int(year), family)
        take = spec.take if spec is not None else {}
    cells = json.loads(rec["cells_json"])
    table = getattr(adapter.module, "RECORD_TABLE", None)
    domain = None
    if table is not None:
        drow = _row(conn, f"SELECT * FROM {table} WHERE record_id = ?", (row_ref,))
        if drow is not None:
            domain = {k: v for k, v in dict(drow).items() if k != "record_id"}
    return {
        "row_ref": row_ref,
        "source_id": rec["source_id"],
        "logical_key": None if source is None else source["logical_key"],
        "logical_no": rec["logical_no"],
        "status": rec["status"],
        "status_reason": rec["status_reason"],
        "cells": [  # 原本の列の順（take の index）に並べる
            {"key": k, "header": take.get(k, {}).get("header"), "value": _excerpt(cells[k])}
            for k in sorted(cells, key=lambda k: (int(take.get(k, {}).get("index", 999)), k))
        ],
        "domain": domain,
        "reconcile": _reconcile_of(conn, row_ref),
    }


def _inspect_calc(conn: sqlite3.Connection, calc_id: str, run_id: str, page: int) -> dict:
    row = _row(conn, "SELECT * FROM computations WHERE calc_id = ?", (calc_id,))
    if row is None:
        raise _not_found("計算", calc_id, run_id)
    refs = json.loads(row["row_refs_json"]) if row["row_refs_json"] else []
    chunk, next_page = _page(refs, page)
    result = json.loads(row["result_json"]) if row["result_json"] else None
    if result is not None and isinstance(result.get("rows"), dict):
        result = {**result, "rows": {r: result["rows"][r] for r in chunk if r in result["rows"]}}
    data = {
        "calc_id": calc_id,
        "kind": row["kind"],
        "lead_id": row["lead_id"],
        "state": row["state"],
        "origin": row["origin"],
        "meaning": row["meaning"],
        "value": row["value"],
        "unit": row["unit"],
        "formula": row["formula"],
        "ref_calc_ids": json.loads(row["ref_calc_ids_json"]) if row["ref_calc_ids_json"] else None,
        "request": json.loads(row["request_json"]),
        "sql": row["sql_text"],
        "row_count": row["row_count"],
        "row_refs_sha256": row["row_refs_sha256"],
        "page": page,
        "row_refs": chunk,
        "result": result,
        "created_at": row["created_at"],
    }
    return _with_next(data, next_page)


def _inspect_claim(conn: sqlite3.Connection, claim_id: str, run_id: str) -> dict:
    row = _row(conn, "SELECT * FROM claims WHERE claim_id = ?", (claim_id,))
    if row is None:
        raise _not_found("主張", claim_id, run_id)
    return {
        "claim_id": claim_id,
        "lead_id": row["lead_id"],
        "type": row["type"],
        "fiscal_year": row["fiscal_year"],
        "text": _excerpt(row["text"]),
        "calc_ids": json.loads(row["calc_ids_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "state": row["state"],
        "replaces": row["replaces"],
        "created_at": row["created_at"],
    }


def _inspect_match(conn: sqlite3.Connection, match_id: str, run_id: str) -> dict:
    row = _row(conn, "SELECT * FROM procurement_matches WHERE match_id = ?", (match_id,))
    if row is None:
        raise _not_found("対応", match_id, run_id)
    return {
        "match_id": match_id,
        "lead_id": row["lead_id"],
        "left": json.loads(row["left_json"]),
        "right": json.loads(row["right_json"]),
        "relation": row["relation"],
        "reason": _excerpt(row["reason"]),
        "evidence": json.loads(row["evidence_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
    }


def inspect(root: Path, target_id: str, run_id: str, *, page: int = 1) -> dict:
    """kizashi inspect。target_id の種類は cli が書式で確かめている。"""
    kind = ledger.id_kind(target_id)
    conn = ledger.open_ro(root, run_id)
    conn.row_factory = sqlite3.Row
    try:
        if kind == "run":
            if target_id != run_id:
                raise _not_found("実行", target_id, run_id)
            return _inspect_run(conn, run_id, page)
        if kind == "lead":
            return _inspect_lead(conn, target_id, run_id, page)
        if kind == "source":
            return _inspect_source(conn, root, target_id, run_id)
        if kind == "record":
            return _inspect_record(conn, root, target_id, run_id)
        if kind == "calc":
            return _inspect_calc(conn, target_id, run_id, page)
        if kind == "claim":
            return _inspect_claim(conn, target_id, run_id)
        if kind == "match":
            return _inspect_match(conn, target_id, run_id)
        raise KizashiError("E_BAD_ID", STAGE, f"inspect できない ID：{target_id}")
    finally:
        conn.close()
