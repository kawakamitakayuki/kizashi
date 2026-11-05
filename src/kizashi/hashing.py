"""正規化したJSONとハッシュ（implementation_spec.md 11章）。

- canonical_json・sha256_*：records.cells_sha256 などに使う正規化したJSONと SHA-256（11章の2）。
- memo_calc_ids：候補のメモに出る計算（候補が持つ計算、候補・主張・判定・結論が参照する計算と、派生計算が
  参照する計算）。check・render・check_hash が同じものを使う。
- check_hash：検証記録のハッシュ（11章）。render が主張と候補の状態を更新した後に計算し、inspect が計算し直して
  比べる（失効の表示は P0-8）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

CALC_REF_RE = re.compile(r"\{calc:([^}]*)\}")
# 検証記録のハッシュに入れない候補の列（11章の1。メモの番号、ハッシュ、巡回数）
LEAD_COLUMNS_NOT_HASHED = ("memo_no", "memo_hash", "rounds")


def canonical_json(obj: object) -> str:
    """キーを並べ替え、区切りの空白をなくしたJSON（11章の2）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dict_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _calc_refs(text: str | None) -> list[str]:
    return CALC_REF_RE.findall(text or "")


def lead_texts(conn: sqlite3.Connection, lead_id: str) -> list[tuple[str, str]]:
    """メモが {calc:ID} を展開する欄のすべて（欄の名前, 文字列）。render は同じ欄を展開し、check と検証記録の
    ハッシュは同じ欄から参照を集める（実装仕様9章の「メモに出る計算」。D-041③⑧）。"""
    (lead,) = dict_rows(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    texts: list[tuple[str, str]] = []

    def add(name: str, value: object) -> None:
        if isinstance(value, str) and value:
            texts.append((name, value))

    for claim in dict_rows(
        conn,
        "SELECT claim_id, text FROM claims WHERE lead_id = ? AND state <> 'withdrawn' ORDER BY claim_id",
        (lead_id,),
    ):
        add(f"{claim['claim_id']}.text", claim["text"])
    for exp in dict_rows(
        conn, "SELECT * FROM explanations WHERE lead_id = ? ORDER BY type", (lead_id,)
    ):
        add(f"{exp['type']}.searched", exp["searched"])
        add(f"{exp['type']}.no_quant_reason", exp["no_quant_reason"])
    for key in ("hypothesis", "withdraw_if", "note", "reopen_condition"):
        add(key, lead[key])
    for i, question in enumerate(json.loads(lead["questions_json"])):
        for key, value in question.items():
            add(f"questions[{i}].{key}", value)
    for key, value in json.loads(lead["comparison_json"]).items():
        add(f"comparison.{key}", value)
    if lead["adjustment_json"]:
        add("adjustment.method", json.loads(lead["adjustment_json"]).get("method"))
    for coef in json.loads(lead["coefficients_json"]):
        for key in ("meaning", "why_needed", "apply_to", "reporter_note"):
            add(f"coefficients.{coef.get('key')}.{key}", coef.get(key))
    for match in dict_rows(
        conn, "SELECT match_id, reason FROM procurement_matches WHERE lead_id = ?", (lead_id,)
    ):
        add(f"{match['match_id']}.reason", match["reason"])
    return texts


def text_calc_refs(conn: sqlite3.Connection, lead_id: str) -> list[str]:
    """メモが展開する欄の {calc:ID} の参照（順に、重複なし）。"""
    refs = [r for _, text in lead_texts(conn, lead_id) for r in _calc_refs(text)]
    return list(dict.fromkeys(refs))


def direct_calc_ids(conn: sqlite3.Connection, lead_id: str) -> list[str]:
    """候補のメモに直接出る計算（順に、重複なし）：比べる2年度、系列、候補が持つ計算、主張の計算、判定の再計算と
    残る差、補正の前後、メモが展開する欄の {calc:ID}。存在しない ID も含めて返す（check が V2_REF_MISSING に
    する）。"""
    (lead,) = dict_rows(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    ids: list[str] = [lead["calc_from"], lead["calc_to"], *json.loads(lead["calc_ids_json"])]
    ids += [
        r[0]
        for r in conn.execute(
            "SELECT calc_id FROM computations WHERE lead_id = ? AND state = 'active'"
            " ORDER BY calc_id",
            (lead_id,),
        )
    ]
    for claim in dict_rows(
        conn,
        "SELECT calc_ids_json FROM claims WHERE lead_id = ? AND state <> 'withdrawn'"
        " ORDER BY claim_id",
        (lead_id,),
    ):
        ids += json.loads(claim["calc_ids_json"])
    for exp in dict_rows(
        conn,
        "SELECT recalc_calc_id, residual_calc_id FROM explanations WHERE lead_id = ? ORDER BY type",
        (lead_id,),
    ):
        ids += [exp["recalc_calc_id"], exp["residual_calc_id"]]
    if lead["adjustment_json"]:
        adjustment = json.loads(lead["adjustment_json"])
        ids += [adjustment.get("before"), adjustment.get("after")]
    ids += text_calc_refs(conn, lead_id)
    return list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))


def memo_calc_ids(conn: sqlite3.Connection, lead_id: str) -> list[str]:
    """direct_calc_ids に、派生計算が参照する計算（入れ子を含む）を足したもの（順に、重複なし）。"""
    seen: dict[str, None] = {}
    stack = list(reversed(direct_calc_ids(conn, lead_id)))
    while stack:
        calc_id = stack.pop()
        if calc_id in seen:
            continue
        seen[calc_id] = None
        row = conn.execute(
            "SELECT ref_calc_ids_json FROM computations WHERE calc_id = ?", (calc_id,)
        ).fetchone()
        if row and row[0]:
            stack.extend(reversed(json.loads(row[0])))
    return list(seen)


def _evidence_sources(items: list) -> set[str]:
    return {
        e["source_id"] for e in items if isinstance(e, dict) and isinstance(e.get("source_id"), str)
    }


def t6_calc_ids(conn: sqlite3.Connection, lead_id: str) -> list[str]:
    """T6a・T6b の材料の照合の計算ID：メモに出る基礎集計の行を含む照合の計算と、T6 の判定（CLI）の根拠の計算。"""
    rows: set[str] = set()
    for calc_id in memo_calc_ids(conn, lead_id):
        for comp in dict_rows(
            conn, "SELECT kind, row_refs_json FROM computations WHERE calc_id = ?", (calc_id,)
        ):
            if comp["kind"] == "base":
                rows |= set(json.loads(comp["row_refs_json"] or "[]"))
    ids = [
        c["calc_id"]
        for c in dict_rows(
            conn,
            "SELECT calc_id, row_refs_json FROM computations WHERE kind = 'reconcile'"
            " AND state = 'active' ORDER BY calc_id",
        )
        if rows & set(json.loads(c["row_refs_json"] or "[]"))
    ]
    for exp in dict_rows(
        conn,
        "SELECT evidence_json FROM explanations WHERE lead_id = ? AND type IN ('T6a', 'T6b')"
        " ORDER BY type",
        (lead_id,),
    ):
        ids += [
            e["calc_id"]
            for e in json.loads(exp["evidence_json"])
            if isinstance(e, dict) and isinstance(e.get("calc_id"), str)
        ]
    return list(dict.fromkeys(ids))


def dependency_sources(conn: sqlite3.Connection, lead_id: str) -> list[str]:
    """検証が依存する資料（実装仕様9章。D-041⑥）：メモに出る基礎集計の行の資料（率の分母だけに使う行を含む）、
    T6a・T6b の根拠の照合の計算が読んだ CSV と PDF、主張・判定・対応・係数の根拠と文書値の資料。"""
    sources: set[str] = set()
    for calc_id in [*memo_calc_ids(conn, lead_id), *t6_calc_ids(conn, lead_id)]:
        for comp in dict_rows(
            conn,
            "SELECT kind, request_json, row_refs_json FROM computations WHERE calc_id = ?",
            (calc_id,),
        ):
            request = json.loads(comp["request_json"])
            if comp["kind"] == "base":
                sources |= {r.split(":", 1)[0] for r in json.loads(comp["row_refs_json"] or "[]")}
            elif comp["kind"] == "doc_value":
                sources.add(request.get("source_id"))
            elif comp["kind"] == "reconcile":
                sources |= {request.get(k) for k in ("source", "csv_source", "pdf_source")}
    for table in ("claims", "explanations", "procurement_matches"):
        where = " AND state <> 'withdrawn'" if table == "claims" else ""
        for row in dict_rows(
            conn, f"SELECT evidence_json FROM {table} WHERE lead_id = ?{where}", (lead_id,)
        ):
            sources |= _evidence_sources(json.loads(row["evidence_json"]))
    (lead,) = dict_rows(conn, "SELECT coefficients_json FROM leads WHERE lead_id = ?", (lead_id,))
    for coef in json.loads(lead["coefficients_json"]):
        sources |= _evidence_sources(coef.get("basis") or [])
    return sorted(s for s in sources if isinstance(s, str) and s)


def check_hash(conn: sqlite3.Connection, lead_id: str) -> str:
    """検証記録のハッシュ（11章）。render が読む行を集め、表の名前と ID の順に正規化JSONで連結した SHA-256。"""
    items: list[tuple[str, str, object]] = []
    (lead,) = dict_rows(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    items.append(
        ("leads", lead_id, {k: v for k, v in lead.items() if k not in LEAD_COLUMNS_NOT_HASHED})
    )
    for claim in dict_rows(
        conn, "SELECT * FROM claims WHERE lead_id = ? AND state <> 'withdrawn'", (lead_id,)
    ):
        items.append(("claims", claim["claim_id"], claim))
    for exp in dict_rows(conn, "SELECT * FROM explanations WHERE lead_id = ?", (lead_id,)):
        items.append(("explanations", exp["type"], exp))
    for match in dict_rows(conn, "SELECT * FROM procurement_matches WHERE lead_id = ?", (lead_id,)):
        items.append(("procurement_matches", match["match_id"], match))
    record_ids: set[str] = set()
    for calc_id in sorted({*memo_calc_ids(conn, lead_id), *t6_calc_ids(conn, lead_id)}):
        for comp in dict_rows(conn, "SELECT * FROM computations WHERE calc_id = ?", (calc_id,)):
            items.append(("computations", calc_id, comp))
            record_ids |= set(json.loads(comp["row_refs_json"] or "[]"))
    for record_id in sorted(record_ids):
        row = conn.execute(
            "SELECT cells_sha256 FROM records WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is not None:
            items.append(("records", record_id, {"cells_sha256": row[0]}))
    for source_id in dependency_sources(conn, lead_id):
        row = conn.execute(
            "SELECT sha256 FROM sources WHERE source_id = ?", (source_id,)
        ).fetchone()
        items.append(("sources", source_id, {"sha256": None if row is None else row[0]}))
    (versions,) = conn.execute("SELECT rule_versions_json FROM runs").fetchone()
    items.append(("runs", "rule_versions", json.loads(versions)))
    joined = "".join(
        canonical_json({"table": t, "id": i, "row": row})
        for t, i, row in sorted(items, key=lambda x: (x[0], x[1]))
    )
    return sha256_text(joined)
