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


def direct_calc_ids(conn: sqlite3.Connection, lead_id: str) -> list[str]:
    """候補のメモに直接出る計算（順に、重複なし）：比べる2年度、系列、候補が持つ計算、主張の計算と本文の
    {calc:ID}、判定の再計算、補正の前後、仮説・質問の本文の {calc:ID}。存在しない ID も含めて返す
    （check が V2_REF_MISSING にする）。"""
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
        "SELECT calc_ids_json, text FROM claims WHERE lead_id = ? AND state <> 'withdrawn'"
        " ORDER BY claim_id",
        (lead_id,),
    ):
        ids += json.loads(claim["calc_ids_json"]) + _calc_refs(claim["text"])
    ids += [
        r[0]
        for r in conn.execute(
            "SELECT recalc_calc_id FROM explanations WHERE lead_id = ? AND recalc_calc_id IS NOT NULL"
            " ORDER BY type",
            (lead_id,),
        )
    ]
    if lead["adjustment_json"]:
        adjustment = json.loads(lead["adjustment_json"])
        ids += [adjustment.get("before"), adjustment.get("after")]
    ids += _calc_refs(lead["hypothesis"]) + _calc_refs(lead["withdraw_if"])
    for question in json.loads(lead["questions_json"]):
        for value in question.values():
            ids += _calc_refs(value if isinstance(value, str) else None)
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


def check_hash(conn: sqlite3.Connection, lead_id: str) -> str:
    """検証記録のハッシュ（11章）。render が読む行を集め、表の名前と ID の順に正規化JSONで連結した SHA-256。"""
    items: list[tuple[str, str, object]] = []
    (lead,) = dict_rows(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    items.append(
        ("leads", lead_id, {k: v for k, v in lead.items() if k not in LEAD_COLUMNS_NOT_HASHED})
    )
    sources: set[str] = set()
    for claim in dict_rows(
        conn, "SELECT * FROM claims WHERE lead_id = ? AND state <> 'withdrawn'", (lead_id,)
    ):
        items.append(("claims", claim["claim_id"], claim))
        sources |= _evidence_sources(json.loads(claim["evidence_json"]))
    for exp in dict_rows(conn, "SELECT * FROM explanations WHERE lead_id = ?", (lead_id,)):
        items.append(("explanations", exp["type"], exp))
        sources |= _evidence_sources(json.loads(exp["evidence_json"]))
    for match in dict_rows(conn, "SELECT * FROM procurement_matches WHERE lead_id = ?", (lead_id,)):
        items.append(("procurement_matches", match["match_id"], match))
        sources |= _evidence_sources(json.loads(match["evidence_json"]))
    record_ids: set[str] = set()
    calc_ids = set(memo_calc_ids(conn, lead_id))
    for exp in dict_rows(
        conn, "SELECT evidence_json FROM explanations WHERE lead_id = ?", (lead_id,)
    ):
        calc_ids |= {
            e["calc_id"]
            for e in json.loads(exp["evidence_json"])
            if isinstance(e, dict) and "calc_id" in e
        }
    for calc_id in sorted(calc_ids):
        for comp in dict_rows(conn, "SELECT * FROM computations WHERE calc_id = ?", (calc_id,)):
            items.append(("computations", calc_id, comp))
            record_ids |= set(json.loads(comp["row_refs_json"] or "[]"))
            request = json.loads(comp["request_json"])
            if comp["kind"] == "doc_value":
                sources.add(request.get("source_id"))
    for record_id in sorted(record_ids):
        row = conn.execute(
            "SELECT source_id, cells_sha256 FROM records WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is not None:
            items.append(("records", record_id, {"cells_sha256": row[1]}))
            sources.add(row[0])
    for source_id in sorted(s for s in sources if s):
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
