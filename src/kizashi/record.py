"""提案の種類ごとの登録（implementation_spec.md 2章の record.py、4.5、6章、7章）。

kizashi record <run_id> inbox/<名前>.json

1. inbox.load_proposal で場所と形を検査する。
2. 種類（kind）ごとに、項目の形を検査する。管理項目（state、checked、created_at、rounds、計算の値など）が
   あれば E_SELF_DECLARED。知らない項目と必須の欠けは E_SCHEMA。
3. 参照先（候補、資料、計算、レコード、主張、対応、探索SQL）がなければ E_REF_NOT_FOUND。
4. 引用は quotes.verify_quote で照合する（知識ファイルの手がかりは verify_knowledge_quote）。
5. Claude が書く文字列に V7 の数値と氏名の検査をする（check.py の関数。不合格は E_V7）。強い語は警告。
6. 1つのトランザクションで登録し、成功したら提案を accepted/<連番>-<kind>.json に写して inbox/ から消す。
   失敗したら提案を残す。events に record を記録する。

run_config は run が読むもので、record では受け付けない。T6a・T6b の判定は CLI が照合の計算から
登録するので、提案では受け付けない（9章の末尾）。係数の提案は計算に使わない（6.4。D-012）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from kizashi import calc, check, detect, inbox, ledger, limits, paths, quotes
from kizashi.adapters import Adapter, load_adapter
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json

STAGE = "record"

# 種類 → (必須の項目, 任意の項目)。kind は必須。
KINDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "calc": (
        frozenset({"op", "table", "filter", "label", "unit"}),
        frozenset({"column", "lead_id"}),
    ),
    "derive": (frozenset({"op", "args", "label", "unit"}), frozenset({"lead_id"})),
    "doc_value": (
        frozenset({"lead_id", "source_id", "position", "quote", "value", "label", "unit"}),
        frozenset(),
    ),
    "lead": (
        frozenset(
            {
                "query_file",
                "lead_key",
                "change_category",
                "period_from",
                "period_to",
                "calc_from",
                "calc_to",
                "calc_ids",
                "comparison",
                "note",
            }
        ),
        frozenset(),
    ),
    "hypothesis": (frozenset({"lead_id", "text", "withdraw_if"}), frozenset({"result"})),
    "match": (
        frozenset({"lead_id", "left", "right", "relation", "reason", "evidence", "status"}),
        frozenset(),
    ),
    "claim": (
        frozenset({"lead_id", "type", "text", "calc_ids", "evidence"}),
        frozenset({"fiscal_year"}),
    ),
    "explanation": (
        frozenset({"lead_id", "type", "judgment", "evidence", "searched"}),
        frozenset(
            {
                "scope",
                "recalc_calc_id",
                "residual_calc_id",
                "reflected_conditions",
                "no_quant_reason",
            }
        ),
    ),
    "question": (
        frozenset({"lead_id", "to", "question", "documents", "decision_impact", "purposes"}),
        frozenset({"foia"}),
    ),
    "coefficient": (
        frozenset(
            {"lead_id", "key", "value", "meaning", "why_needed", "apply_to", "basis", "status"}
        ),
        frozenset({"reporter_note"}),
    ),
    "conclusion": (
        frozenset({"lead_id", "outcome", "comparison_state"}),
        frozenset(
            {"explanation_status", "adjustment", "hold_reason", "reopen_condition", "comparison"}
        ),
    ),
    "withdraw": (frozenset({"target", "reason"}), frozenset()),
}

# CLI が決める管理項目（4.5。設計書3.4の「管理項目はCLIが決め、提案に書かれた値を採らない」）。
MANAGEMENT_FIELDS = frozenset(
    {
        "state",
        "checked",
        "created_at",
        "updated_at",
        "at",
        "rounds",
        "memo_no",
        "memo_hash",
        "check_hash",
        "origin",
        "context",
        "row_refs",
        "row_refs_json",
        "row_refs_sha256",
        "row_count",
        "result",
        "result_json",
        "sql_text",
        "sql_sha256",
        "formula",
        "ref_calc_ids",
        "screen_id",
        "peer_count",
        "calc_id",
        "claim_id",
        "match_id",
        "proposed_outcome",
    }
)
# 種類ごとに CLI が決める項目（計算の値、候補の ID）。
MANAGEMENT_BY_KIND = {
    "calc": frozenset({"value"}),
    "derive": frozenset({"value"}),
    "lead": frozenset({"lead_id", "value"}),
}

COMPARISON_KEYS = ("population", "row_meaning", "period", "unit", "entity_mapping", "formula")
CLAIM_TYPES = ("record", "observation", "publisher_explanation", "hypothesis")
EXPLANATION_TYPES = ("T1", "T2", "T3", "T4", "T5", "T6c")
CLI_EXPLANATION_TYPES = ("T6a", "T6b")
JUDGMENTS = ("support", "refute", "unknown", "na")
SCOPES = ("full", "part", "unknown")
RELATIONS = ("1:1", "1:n", "n:1", "n:m")
MATCH_STATUSES = ("confirmed", "pending")
COEF_STATUSES = ("proposed", "accepted", "declined")
OUTCOMES = ("change", "artifact", "hold")
COMPARISON_STATES = ("comparable", "adjusted", "unresolved", "not_applicable")
EXPLANATION_STATUSES = ("none", "part", "full", "unknown")
HOLD_REASONS = ("comparison", "source_check", "limit")
HYPOTHESIS_RESULTS = ("kept", "withdrawn")
# 質問の目的（D-041⑩）：元の記載の確認、理由・内訳の確認、不足資料の照会、そのほか
QUESTION_PURPOSES = ("verify_record", "reason_breakdown", "missing_documents", "other")

_CALC_REF_RE = re.compile(r"\{calc:([^}]*)\}")


# ---------------------------------------------------------------------------
# 検査の部品
# ---------------------------------------------------------------------------


def _err(
    code: str, message: str, *, next_action: str = "", affected: Iterable[str] = ()
) -> KizashiError:
    return KizashiError(code, STAGE, message, affected_ids=affected, next_action=next_action)


def _schema(message: str, next_action: str = "") -> KizashiError:
    return _err(
        "E_SCHEMA",
        message,
        next_action=next_action or "書式は .claude/skills/investigate/record-examples.md を読む。",
    )


@dataclass
class Ctx:
    root: Path
    run_id: str
    conn: sqlite3.Connection
    adapter: Adapter
    data: dict
    texts: dict[str, str] = field(default_factory=dict)  # V7 の対象（欄の名前 → 文字列）
    # 強い語の警告だけを当てる欄（比較の条件。D-036）。氏名の検査は提案の中のすべての文字列に当てる
    warn_texts: dict[str, str] = field(default_factory=dict)
    quotes: list[str] = field(default_factory=list)  # 照合済みの引用（V7 の対象外）

    def text(self, key: str, *, allow_empty: bool = False, v7_numbers: bool = True) -> str:
        """Claude が書く文字列の欄。V7 の数値・強い語の対象にする（v7_numbers=False なら当てない）。

        氏名の検査は、この指定に関係なく、提案の中のすべての文字列に当てる（_v7。D-035・D-036）。
        """
        value = self.data.get(key)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise _schema(f"{key} は空でない文字列で書く。")
        if v7_numbers:
            self.texts[key] = value
        return value

    def opt_text(self, key: str) -> str | None:
        if key not in self.data:
            return None
        return self.text(key)

    def choice(self, key: str, choices: Iterable[str]) -> str:
        choices = tuple(choices)
        value = self.data.get(key)
        if value not in choices:
            raise _schema(f"{key} は {'・'.join(choices)} のどれか：{value!r}")
        return value

    def opt_choice(self, key: str, choices: Iterable[str]) -> str | None:
        return self.choice(key, choices) if key in self.data else None


def _id(value: object, kind: str, key: str) -> str:
    if not isinstance(value, str) or ledger.id_kind(value) != kind:
        raise _schema(f"{key} の ID の書式が誤っている：{value!r}")
    return value


def _lead(ctx: Ctx, key: str = "lead_id") -> sqlite3.Row:
    lead_id = _id(ctx.data.get(key), "lead", key)
    ctx.conn.row_factory = sqlite3.Row
    try:
        row = ctx.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    finally:
        ctx.conn.row_factory = None
    if row is None:
        raise _err(
            "E_REF_NOT_FOUND",
            f"候補がない：{lead_id}",
            next_action=f"kizashi inspect {ctx.run_id} で候補IDを確かめる。",
            affected=(lead_id,),
        )
    return row


def _opt_lead_id(ctx: Ctx) -> str | None:
    return _lead(ctx)["lead_id"] if "lead_id" in ctx.data else None


def _calc(ctx: Ctx, calc_id: object, key: str) -> tuple:
    """(calc_id, kind, lead_id, request_json, origin)。存在しない・取り下げた計算は E_REF_NOT_FOUND。"""
    cid = _id(calc_id, "calc", key)
    row = ctx.conn.execute(
        "SELECT calc_id, kind, lead_id, request_json, origin, state FROM computations WHERE calc_id = ?",
        (cid,),
    ).fetchone()
    if row is None:
        raise _err("E_REF_NOT_FOUND", f"計算がない：{cid}", affected=(cid,))
    if row[5] != "active":
        raise _err("E_REF_NOT_FOUND", f"取り下げた計算は参照できない：{cid}", affected=(cid,))
    return row[:5]


def _calc_list(ctx: Ctx, key: str) -> list[str]:
    value = ctx.data.get(key)
    if not isinstance(value, list):
        raise _schema(f"{key} は計算IDの配列で書く（空でよい）。")
    for item in value:
        _calc(ctx, item, key)
    if len(set(value)) != len(value):
        raise _schema(f"{key} に同じ計算IDが2回ある。")
    return list(value)


def _evidence(ctx: Ctx, key: str = "evidence", **kw: object) -> list[dict]:
    value = ctx.data.get(key)
    if not isinstance(value, list):
        raise _schema(f"{key} は根拠の配列で書く（空でよい）。")
    for item in value:
        verified = quotes.verify_quote(ctx.conn, ctx.root, ctx.run_id, ctx.adapter, item, **kw)
        ctx.quotes.append(verified.quote)
    return value


def _row_refs(ctx: Ctx, key: str) -> list[str]:
    value = ctx.data.get(key)
    if not isinstance(value, list) or not value:
        raise _schema(f"{key} は row_ref の空でない配列で書く。")
    for ref in value:
        rid = _id(ref, "record", key)
        if ctx.conn.execute("SELECT 1 FROM records WHERE record_id = ?", (rid,)).fetchone() is None:
            raise _err("E_REF_NOT_FOUND", f"レコードがない：{rid}", affected=(rid,))
    if len(set(value)) != len(value):
        raise _schema(f"{key} に同じ row_ref が2回ある。")
    return list(value)


def _year(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1900 <= value <= 2100:
        raise _schema(f"{key} は年度（西暦の整数）で書く：{value!r}")
    return value


def _update_lead(ctx: Ctx, lead_id: str, **columns: object) -> None:
    sets = ", ".join(f"{name} = ?" for name in columns)
    ctx.conn.execute(f"UPDATE leads SET {sets} WHERE lead_id = ?", (*columns.values(), lead_id))


def _value_of(ctx: Ctx, calc_id: str) -> str | None:
    return ctx.conn.execute(
        "SELECT value FROM computations WHERE calc_id = ?", (calc_id,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# 種類ごとの処理。戻り値は (対象のID, 返す追加の値, events の before)
# ---------------------------------------------------------------------------

Result = tuple[str, dict, object]


def _do_calc(ctx: Ctx) -> Result:
    lead_id = _opt_lead_id(ctx)
    label, unit = ctx.text("label"), ctx.text("unit", v7_numbers=False)
    request = {k: ctx.data[k] for k in ("op", "table", "column", "filter") if k in ctx.data}
    cid = calc.register_base(
        ctx.conn, ctx.adapter, request, meaning=label, unit=unit, lead_id=lead_id
    )
    row_count = ctx.conn.execute(
        "SELECT row_count FROM computations WHERE calc_id = ?", (cid,)
    ).fetchone()[0]
    return cid, {"value": _value_of(ctx, cid), "unit": unit, "row_count": row_count}, None


def _do_derive(ctx: Ctx) -> Result:
    lead_id = _opt_lead_id(ctx)
    label, unit = ctx.text("label"), ctx.text("unit", v7_numbers=False)
    cid = calc.register_derived(
        ctx.conn, ctx.data["op"], ctx.data["args"], meaning=label, unit=unit, lead_id=lead_id
    )
    return cid, {"value": _value_of(ctx, cid), "unit": unit}, None


def _do_doc_value(ctx: Ctx) -> Result:
    lead_id = _lead(ctx)["lead_id"]
    label, unit = ctx.text("label"), ctx.text("unit", v7_numbers=False)
    evidence = {k: ctx.data[k] for k in ("source_id", "position", "quote")} | {"role": "support"}
    verified = quotes.verify_quote(ctx.conn, ctx.root, ctx.run_id, ctx.adapter, evidence)
    ctx.quotes.append(verified.quote)
    cid = calc.register_doc_value(
        ctx.conn,
        lead_id=lead_id,
        evidence=evidence,
        value=ctx.data["value"],
        meaning=label,
        unit=unit,
    )
    return cid, {"value": _value_of(ctx, cid), "unit": unit}, None


def _has_year_filter(request: dict, year: int) -> bool:
    for item in request.get("filter") or []:
        if (
            isinstance(item, dict)
            and item.get("col") == "fiscal_year"
            and item.get("op") == "="
            and str(item.get("value")) == str(year)
        ):
            return True
    return False


def _do_lead(ctx: Ctx) -> Result:
    data = ctx.data
    query_file = data.get("query_file")
    if not isinstance(query_file, str) or "/" in query_file or not query_file.endswith(".sql"):
        raise _schema("query_file は accepted/ に写した探索SQLの名前（例 q-002.sql）で書く。")
    if not (paths.run_dir(ctx.root, ctx.run_id) / "accepted" / query_file).is_file():
        raise _err(
            "E_REF_NOT_FOUND",
            f"accepted/ に探索SQL {query_file} がない。",
            next_action="先に kizashi query で探索SQLを実行する（成功すると accepted/ に写る）。",
        )
    lead_key = ctx.text("lead_key", v7_numbers=False)
    categories = tuple(ctx.adapter.config.get("reconcile", {}).get("fields_by_category", {}))
    category = ctx.choice("change_category", categories)
    period_from, period_to = (
        _year(data["period_from"], "period_from"),
        _year(data["period_to"], "period_to"),
    )
    pairs = (("calc_from", period_from), ("calc_to", period_to))
    for key, year in pairs:
        cid, kind, owner, request_json, _ = _calc(ctx, data[key], key)
        if kind != "base":
            raise _schema(f"{key}（{cid}）が基礎集計でない。")
        if not _has_year_filter(json.loads(request_json), year):
            raise _schema(
                f"{key}（{cid}）の条件に fiscal_year = {year} がない。",
                next_action="比べる年度ごとに、fiscal_year の条件を付けた calc を登録する。",
            )
    calc_ids = _calc_list(ctx, "calc_ids")
    owned = [data["calc_from"], data["calc_to"], *calc_ids]
    for cid in owned:
        owner = _calc(ctx, cid, "calc_ids")[2]
        if owner is not None:
            raise _schema(f"計算 {cid} は別の候補（{owner}）のものである。")
    comparison = data.get("comparison")
    if not isinstance(comparison, dict) or set(comparison) != set(COMPARISON_KEYS):
        raise _schema(f"comparison は {'・'.join(COMPARISON_KEYS)} の6項目で書く。")
    # 比較の条件は V7 の数値の検査を当てない（既定値にも「1件の契約」がある。D-031③）。
    # 氏名の検査（提案のすべての文字列。D-035・D-036）と強い語の警告（D-036）は当てる
    for key in COMPARISON_KEYS:
        if not isinstance(comparison[key], str):
            raise _schema(f"comparison の {key} は文字列で書く。")
        ctx.warn_texts[f"comparison.{key}"] = comparison[key]
    note = ctx.text("note", allow_empty=True)
    comparison = _with_scope(ctx, comparison)

    lead_id = ledger.next_id(ctx.conn, "L")
    try:
        ctx.conn.execute(
            "INSERT INTO leads (lead_id, screen_id, query_file, lead_key, change_category, period_from,"
            " period_to, calc_from, calc_to, calc_ids_json, comparison_json, note)"
            " VALUES (?, 'explore', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lead_id,
                query_file,
                lead_key,
                category,
                str(period_from),
                str(period_to),
                data["calc_from"],
                data["calc_to"],
                canonical_json(calc_ids),
                canonical_json(comparison),
                note,
            ),
        )
    except sqlite3.IntegrityError:
        raise _schema(
            "同じ候補（探索、比較の単位、区分、年度）がすでにある。",
            next_action=f"kizashi inspect {ctx.run_id} で既存の候補を確かめる。",
        ) from None
    ctx.conn.executemany(
        "UPDATE computations SET lead_id = ? WHERE calc_id = ?", [(lead_id, c) for c in owned]
    )
    counts = json.loads(
        ctx.conn.execute("SELECT counts_json FROM runs WHERE run_id = ?", (ctx.run_id,)).fetchone()[
            0
        ]
    )
    explore = counts.setdefault("explore", {"compared": 0})
    explore["compared"] = int(explore.get("compared", 0)) + 1
    ctx.conn.execute(
        "UPDATE runs SET counts_json = ? WHERE run_id = ?", (canonical_json(counts), ctx.run_id)
    )
    return lead_id, {"screen_id": "explore", "explore_compared": explore["compared"]}, None


def _do_hypothesis(ctx: Ctx) -> Result:
    lead = _lead(ctx)
    text, withdraw_if = ctx.text("text"), ctx.text("withdraw_if")
    result = ctx.opt_choice("result", HYPOTHESIS_RESULTS)
    lead_id = lead["lead_id"]
    if lead["hypothesis"] is None:
        (investigated,) = ctx.conn.execute(
            "SELECT COUNT(*) FROM leads WHERE hypothesis IS NOT NULL"
        ).fetchone()
        if investigated >= limits.MAX_DEEP_LEADS:
            raise _err(
                "E_LIMIT_LEADS",
                f"詳しく調べる候補が上限（{limits.MAX_DEEP_LEADS}件）に達している。",
                next_action="この候補は調べずに残し、記者に伝える（上限に達しても結論を強めない）。",
                affected=(lead_id,),
            )
    before = {
        "hypothesis": lead["hypothesis"],
        "withdraw_if": lead["withdraw_if"],
        "hypothesis_result": lead["hypothesis_result"],
        "state": lead["state"],
    }
    _update_lead(ctx, lead_id, hypothesis=text, withdraw_if=withdraw_if, hypothesis_result=result)
    if lead["state"] == "candidate":
        _update_lead(ctx, lead_id, state="investigating")
        ledger.add_event(
            ctx.conn,
            lead_id,
            "state_change",
            before={"state": "candidate"},
            after={"state": "investigating"},
            reason="仮説の登録",
        )
    return (
        lead_id,
        {"state": "investigating" if lead["state"] == "candidate" else lead["state"]},
        before,
    )


def _do_match(ctx: Ctx) -> Result:
    lead_id = _lead(ctx)["lead_id"]
    left, right = _row_refs(ctx, "left"), _row_refs(ctx, "right")
    relation = ctx.choice("relation", RELATIONS)
    reason = ctx.text("reason")
    status = ctx.choice("status", MATCH_STATUSES)
    evidence = _evidence(ctx)
    match_id = ledger.next_id(ctx.conn, "M")
    ctx.conn.execute(
        "INSERT INTO procurement_matches (match_id, lead_id, left_json, right_json, relation, reason,"
        " evidence_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            match_id,
            lead_id,
            canonical_json(left),
            canonical_json(right),
            relation,
            reason,
            canonical_json(evidence),
            status,
            ledger.now_iso(),
        ),
    )
    return match_id, {"status": status}, None


def _do_claim(ctx: Ctx) -> Result:
    lead_id = _lead(ctx)["lead_id"]
    claim_type = ctx.choice("type", CLAIM_TYPES)
    fiscal_year = ctx.data.get("fiscal_year")
    if claim_type == "record" and fiscal_year is None:
        raise _schema("type が record（原資料の記載）の主張には fiscal_year が要る。")
    if fiscal_year is not None:
        fiscal_year = _year(fiscal_year, "fiscal_year")
    text = ctx.text("text")
    calc_ids = _calc_list(ctx, "calc_ids")
    evidence = _evidence(ctx, claim_type=claim_type, fiscal_year=fiscal_year)
    claim_id = ledger.next_id(ctx.conn, "C")
    ctx.conn.execute(
        "INSERT INTO claims (claim_id, lead_id, type, fiscal_year, text, calc_ids_json, evidence_json,"
        " state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
        (
            claim_id,
            lead_id,
            claim_type,
            fiscal_year,
            text,
            canonical_json(calc_ids),
            canonical_json(evidence),
            ledger.now_iso(),
        ),
    )
    return claim_id, {"state": "draft"}, None


def _do_explanation(ctx: Ctx) -> Result:
    lead_id = _lead(ctx)["lead_id"]
    etype = ctx.data.get("type")
    if etype in CLI_EXPLANATION_TYPES:
        raise _err(
            "E_SELF_DECLARED",
            f"{etype} の判定は kizashi が照合の計算から登録する（提案では受け付けない）。",
            next_action="T1〜T5 と T6c だけを kind: explanation で登録する。",
            affected=(lead_id,),
        )
    etype = ctx.choice("type", EXPLANATION_TYPES)
    judgment = ctx.choice("judgment", JUDGMENTS)
    evidence = _evidence(ctx)
    searched = ctx.text("searched", allow_empty=True)
    scope = ctx.opt_choice("scope", SCOPES)
    recalc = ctx.data.get("recalc_calc_id")
    if recalc is not None:
        _calc(ctx, recalc, "recalc_calc_id")
    residual = ctx.data.get("residual_calc_id")
    if residual is not None:
        _calc(ctx, residual, "residual_calc_id")
    reflected = ctx.data.get("reflected_conditions")
    if reflected is not None:
        if not isinstance(reflected, list) or not reflected:
            raise _schema(
                'reflected_conditions は、説明によって足した・変えた条件（{"col", "op", "value"}）の空でない配列で書く。'
            )
        # 基礎集計の条件と同じ検査（使える列・演算・整数の年度。D-042②）
        calc.build_query(
            ctx.adapter,
            {"op": "count", "table": ctx.adapter.module.RECORD_TABLE, "filter": reflected},
        )
    no_quant = ctx.opt_text("no_quant_reason")
    needs_scope = judgment == "support" and etype in ("T1", "T3", "T4")
    if needs_scope and (scope is None or (recalc is None and no_quant is None)):
        raise _schema(f"支持の {etype} には scope と、recalc_calc_id か no_quant_reason が要る。")
    ctx.conn.row_factory = sqlite3.Row
    try:
        prev = ctx.conn.execute(
            "SELECT * FROM explanations WHERE lead_id = ? AND type = ?", (lead_id, etype)
        ).fetchone()
    finally:
        ctx.conn.row_factory = None
    before = None if prev is None else dict(prev)
    ctx.conn.execute(
        "INSERT INTO explanations (lead_id, type, judgment, evidence_json, searched, recalc_calc_id,"
        " residual_calc_id, reflected_conditions_json, scope, no_quant_reason, context, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'main_session', ?)"
        " ON CONFLICT (lead_id, type) DO UPDATE SET judgment = excluded.judgment,"
        " evidence_json = excluded.evidence_json, searched = excluded.searched,"
        " recalc_calc_id = excluded.recalc_calc_id, residual_calc_id = excluded.residual_calc_id,"
        " reflected_conditions_json = excluded.reflected_conditions_json,"
        " scope = excluded.scope,"
        " no_quant_reason = excluded.no_quant_reason, context = excluded.context,"
        " updated_at = excluded.updated_at",
        (
            lead_id,
            etype,
            judgment,
            canonical_json(evidence),
            searched,
            recalc,
            residual,
            None if reflected is None else canonical_json(reflected),
            scope,
            no_quant,
            ledger.now_iso(),
        ),
    )
    return lead_id, {"type": etype, "judgment": judgment}, before


def _do_question(ctx: Ctx) -> Result:
    lead = _lead(ctx)
    item: dict = {k: ctx.text(k) for k in ("to", "question", "documents", "decision_impact")}
    purposes = ctx.data.get("purposes")
    if not (
        isinstance(purposes, list)
        and purposes
        and all(p in QUESTION_PURPOSES for p in purposes)
        and len(set(purposes)) == len(purposes)
    ):
        raise _schema(
            f"purposes は質問の目的（{'・'.join(QUESTION_PURPOSES)}）の空でない配列で書く（重複なし）。"
        )
    item["purposes"] = list(purposes)
    foia = ctx.opt_text("foia")
    if foia is not None:
        item["foia"] = foia
    questions = json.loads(lead["questions_json"])
    questions.append(item)
    _update_lead(ctx, lead["lead_id"], questions_json=canonical_json(questions))
    return lead["lead_id"], {"questions": len(questions)}, None


def _do_coefficient(ctx: Ctx) -> Result:
    lead = _lead(ctx)
    key = ctx.text("key", v7_numbers=False)
    value = ctx.data.get("value")
    if not isinstance(value, str) or not calc.DECIMAL_RE.fullmatch(value):
        raise _schema(f"係数の value は十進数の文字列で書く：{value!r}")
    entry = {"key": key, "value": value}
    for name in ("meaning", "why_needed", "apply_to"):
        entry[name] = ctx.text(name)
    status = ctx.choice("status", COEF_STATUSES)
    note = ctx.opt_text("reporter_note")
    if status in ("accepted", "declined") and note is None:
        raise _schema(f"status が {status} のときは reporter_note（記者の判断の要約）が要る。")
    basis = ctx.data.get("basis")
    if not isinstance(basis, list):
        raise _schema("basis は手がかりの配列で書く（手がかりがなければ空の配列）。")
    for item in basis:
        if isinstance(item, dict) and "file" in item:
            quotes.verify_knowledge_quote(ctx.root, item)
        else:
            ctx.quotes.append(
                quotes.verify_quote(ctx.conn, ctx.root, ctx.run_id, ctx.adapter, item).quote
            )
    entry.update({"basis": basis, "status": status})
    if note is not None:
        entry["reporter_note"] = note
    coefs = json.loads(lead["coefficients_json"])
    before = next((c for c in coefs if c.get("key") == key), None)
    coefs = [c for c in coefs if c.get("key") != key] + [entry]
    _update_lead(ctx, lead["lead_id"], coefficients_json=canonical_json(coefs))
    return lead["lead_id"], {"key": key, "status": status, "used_in_calc": False}, before


def _do_conclusion(ctx: Ctx) -> Result:
    lead = _lead(ctx)
    outcome = ctx.choice("outcome", OUTCOMES)
    if outcome != "hold":
        limit = check.round_limit(ctx.conn, lead)
        if lead["rounds"] >= limit:
            raise check.rounds_exceeded(lead["lead_id"], lead["rounds"], limit, STAGE)
    comparison_state = ctx.choice("comparison_state", COMPARISON_STATES)
    explanation_status = ctx.opt_choice("explanation_status", EXPLANATION_STATUSES)
    hold_reason = ctx.opt_choice("hold_reason", HOLD_REASONS)
    if outcome == "change" and explanation_status is None:
        raise _schema("outcome が change のときは explanation_status が要る。")
    if outcome == "hold" and hold_reason is None:
        raise _schema("outcome が hold のときは hold_reason が要る。")
    adjustment = ctx.data.get("adjustment")
    if comparison_state == "adjusted" and adjustment is None:
        raise _schema(
            "comparison_state が adjusted のときは adjustment（方法、補正前後の計算ID）が要る。"
        )
    if adjustment is not None:
        if not isinstance(adjustment, dict) or set(adjustment) != {"method", "before", "after"}:
            raise _schema('adjustment は {"method", "before", "after"}（補正前後の計算ID）で書く。')
        if not isinstance(adjustment["method"], str) or not adjustment["method"].strip():
            raise _schema("adjustment の method は空でない文字列で書く。")
        ctx.texts["adjustment.method"] = adjustment["method"]
        _calc(ctx, adjustment["before"], "adjustment.before")
        _calc(ctx, adjustment["after"], "adjustment.after")
    reopen = ctx.opt_text("reopen_condition")
    comparison = json.loads(lead["comparison_json"])
    override = ctx.data.get("comparison")
    if override is not None:
        if not isinstance(override, dict) or not set(override) <= set(COMPARISON_KEYS):
            raise _schema(
                f"comparison は {'・'.join(COMPARISON_KEYS)} のうち変えるものだけを書く。"
            )
        for key, value in override.items():
            if not isinstance(value, str):
                raise _schema(f"comparison の {key} は文字列で書く。")
            ctx.warn_texts[f"comparison.{key}"] = value  # 強い語の警告（D-036）
        comparison.update(override)
    comparison = _with_scope(ctx, comparison)
    before = {
        k: lead[k]
        for k in (
            "proposed_outcome",
            "comparison_state",
            "explanation_status",
            "adjustment_json",
            "hold_reason",
            "reopen_condition",
            "comparison_json",
        )
    }
    _update_lead(
        ctx,
        lead["lead_id"],
        proposed_outcome=outcome,
        comparison_state=comparison_state,
        explanation_status=explanation_status,
        adjustment_json=None if adjustment is None else canonical_json(adjustment),
        hold_reason=hold_reason,
        reopen_condition=reopen,
        comparison_json=canonical_json(comparison),
    )
    return lead["lead_id"], {"proposed_outcome": outcome}, before


def _stale_claims(ctx: Ctx, calc_ids: set[str], claim_ids: set[str]) -> list[str]:
    rows = ctx.conn.execute(
        "SELECT claim_id, calc_ids_json, text, replaces FROM claims"
        " WHERE state IN ('draft', 'checked', 'held')"
    ).fetchall()
    stale: list[str] = []
    for claim_id, calc_json, text, replaces in rows:
        refs = set(json.loads(calc_json)) | set(_CALC_REF_RE.findall(text))
        if refs & calc_ids or (replaces is not None and replaces in claim_ids):
            stale.append(claim_id)
    return stale


def _dependent_calcs(ctx: Ctx, calc_id: str) -> set[str]:
    """取り下げる計算と、それを（派生計算を通して）参照する計算。"""
    rows = ctx.conn.execute(
        "SELECT calc_id, ref_calc_ids_json FROM computations WHERE ref_calc_ids_json IS NOT NULL"
    ).fetchall()
    refs = {cid: set(json.loads(r)) for cid, r in rows}
    found = {calc_id}
    changed = True
    while changed:
        changed = False
        for cid, args in refs.items():
            if cid not in found and args & found:
                found.add(cid)
                changed = True
    return found


def _do_withdraw(ctx: Ctx) -> Result:
    target = ctx.data.get("target")
    kind = ledger.id_kind(target) if isinstance(target, str) else None
    if kind not in ("claim", "match", "calc"):
        raise _schema(f"target は主張ID・対応ID・計算IDで書く：{target!r}")
    ctx.text("reason")
    if kind == "claim":
        row = ctx.conn.execute("SELECT state FROM claims WHERE claim_id = ?", (target,)).fetchone()
        if row is None:
            raise _err("E_REF_NOT_FOUND", f"主張がない：{target}", affected=(target,))
        if row[0] == "withdrawn":
            raise _schema(f"主張 {target} はすでに取り下げている。")
        before = {"state": row[0]}
        ctx.conn.execute("UPDATE claims SET state = 'withdrawn' WHERE claim_id = ?", (target,))
        stale = _stale_claims(ctx, set(), {target})
    elif kind == "match":
        row = ctx.conn.execute(
            "SELECT status FROM procurement_matches WHERE match_id = ?", (target,)
        ).fetchone()
        if row is None:
            raise _err("E_REF_NOT_FOUND", f"対応がない：{target}", affected=(target,))
        if row[0] == "withdrawn":
            raise _schema(f"対応 {target} はすでに取り下げている。")
        before = {"status": row[0]}
        ctx.conn.execute(
            "UPDATE procurement_matches SET status = 'withdrawn' WHERE match_id = ?", (target,)
        )
        stale = []
    else:
        row = ctx.conn.execute(
            "SELECT state, origin FROM computations WHERE calc_id = ?", (target,)
        ).fetchone()
        if row is None:
            raise _err("E_REF_NOT_FOUND", f"計算がない：{target}", affected=(target,))
        if row[0] == "withdrawn":
            raise _schema(f"計算 {target} はすでに取り下げている。")
        if row[1] != "record":
            raise _schema(f"計算 {target} は run が作ったもので、取り下げられない。")
        before = {"state": row[0]}
        ctx.conn.execute("UPDATE computations SET state = 'withdrawn' WHERE calc_id = ?", (target,))
        stale = _stale_claims(ctx, _dependent_calcs(ctx, target), set())
    for claim_id in stale:
        ctx.conn.execute("UPDATE claims SET state = 'stale' WHERE claim_id = ?", (claim_id,))
        ledger.add_event(
            ctx.conn,
            claim_id,
            "state_change",
            after={"state": "stale"},
            reason=f"{target} の取り下げ",
        )
    return target, {"withdrawn": target, "stale_claims": stale}, before


HANDLERS = {
    "calc": _do_calc,
    "derive": _do_derive,
    "doc_value": _do_doc_value,
    "lead": _do_lead,
    "hypothesis": _do_hypothesis,
    "match": _do_match,
    "claim": _do_claim,
    "explanation": _do_explanation,
    "question": _do_question,
    "coefficient": _do_coefficient,
    "conclusion": _do_conclusion,
    "withdraw": _do_withdraw,
}


# ---------------------------------------------------------------------------
# 全体の流れ
# ---------------------------------------------------------------------------


def check_fields(data: dict) -> str:
    """kind と項目を検査し、kind を返す。"""
    kind = data["kind"]
    if kind not in KINDS:
        why = "run_config は run が読む" if kind == "run_config" else "知らない kind"
        raise _err(
            "E_UNKNOWN_KIND",
            f"record が扱う kind でない（{why}）：{kind!r}",
            next_action=f"使える kind：{'・'.join(KINDS)}",
        )
    required, optional = KINDS[kind]
    allowed = required | optional | {"kind"}
    extra = set(data) - allowed
    declared = sorted(
        k for k in extra if k in MANAGEMENT_FIELDS or k in MANAGEMENT_BY_KIND.get(kind, ())
    )
    if declared:
        raise _err(
            "E_SELF_DECLARED",
            f"管理項目は CLI が決める（提案に書かない）：{'・'.join(declared)}",
            next_action="その項目を消して出し直す。",
        )
    if extra:
        raise _schema(f"{kind} に知らない項目がある：{'・'.join(sorted(extra))}")
    missing = sorted(required - set(data))
    if missing:
        raise _schema(f"{kind} の必須の項目がない：{'・'.join(missing)}")
    return kind


def _check_calc_refs(ctx: Ctx) -> None:
    for text in ctx.texts.values():
        for ref in _CALC_REF_RE.findall(text):
            _calc(ctx, ref, "{calc:ID}")


def _v7(ctx: Ctx, target_id: str) -> list[dict]:
    texts = {k: v for k, v in ctx.texts.items() if v}
    # 氏名の検査は、提案の中のすべての文字列（入れ子を含む。根拠の引用、条件の値、手がかりの引用、
    # 結論での比較の条件の変更など）に当てる（D-036）。texts の欄は v7_findings が氏名も見る
    names_only = {k: v for k, v in check.string_leaves(ctx.data) if v and k not in texts}
    words_only = {k: v for k, v in ctx.warn_texts.items() if v}
    if not texts and not names_only:
        return []
    module = ctx.adapter.module
    names = check.name_variants(module.name_list(ctx.conn, ctx.root, ctx.adapter))
    # 取り込まない列の値（担当官の所在地と相手方の住所。D-032）も、氏名と同じ欄に当てる
    excluded = check.excluded_matcher(module.excluded_values(ctx.conn, ctx.root, ctx.adapter))
    errors, warnings = check.v7_findings(
        texts,
        names=names,
        quotes=ctx.quotes,
        names_only=names_only,
        words_only=words_only,
        excluded=excluded,
    )
    if errors:
        parts = [check.v7_hit_text(e) for e in errors]
        raise _err(
            "E_V7",
            "Claude が書く欄の検査（V7）に通らない：" + "、".join(parts),
            next_action="金額・件数・比率は {calc:ID} で参照し、数字を書かない。氏名と、担当官の所在地・"
            "相手方の住所を書かない（引用や条件の値も同じ。それらを含まない範囲を引用する）。",
            affected=(target_id,),
        )
    return [
        {
            "code": w["code"],
            "message": f"{w['field']} に強い語「{w['hit']}」がある。表現を見直す（不合格にはしない）。",
            "affected_ids": [target_id],
        }
        for w in warnings
    ]


def _with_scope(ctx: Ctx, comparison: dict) -> dict:
    """比較の条件の母集団に、実行の母集団の絞り込み（run_config.scope）を書き足す（実装仕様6.3。D-038⑮）。

    検出の候補と同じ関数（detect.scope_population）を使う。結論で母集団を変えても絞り込みを消さない。"""
    row = ctx.conn.execute(
        "SELECT config_json FROM runs WHERE run_id = ?", (ctx.run_id,)
    ).fetchone()
    scope = json.loads(row[0]).get("scope") if row and row[0] else None
    return {**comparison, "population": detect.scope_population(comparison["population"], scope)}


def _domain(conn: sqlite3.Connection, run_id: str) -> str:
    row = conn.execute("SELECT domain FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise _err("E_REF_NOT_FOUND", f"実行の記録がない：{run_id}", affected=(run_id,))
    return row[0]


def record(root: Path, run_id: str, arg: str) -> dict:
    """kizashi record <run_id> inbox/<名前>.json"""
    conn = ledger.open_rw(root, run_id)
    accepted: Path | None = None
    try:
        domain = _domain(conn, run_id)
        # 最初に規則の版を照らす。版の違う台帳には登録しない（check・render ができず、表の形も違いうる。
        # D-042⑤）。提案の場所・形の検査より先に行い、提案を直しても通らないことを先に知らせる（D-090⑨）
        check.check_rule_versions(conn, root, run_id, STAGE)
        proposal = inbox.load_proposal(root, arg)
        kind = check_fields(proposal.data)
        adapter = load_adapter(root, domain)
        ctx = Ctx(root=root, run_id=run_id, conn=conn, adapter=adapter, data=proposal.data)
        with ledger.transaction(conn):
            target_id, extra, before = HANDLERS[kind](ctx)
            _check_calc_refs(ctx)
            warnings = _v7(ctx, target_id)
            ledger.add_event(conn, target_id, "record", before=before, after=proposal.data)
            accepted = inbox.copy_to_accepted(root, run_id, proposal.path, kind)
    except BaseException:
        if accepted is not None:
            accepted.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    proposal.path.unlink()
    result = {
        "run_id": run_id,
        "kind": kind,
        "id": target_id,
        **extra,
        "accepted": accepted.relative_to(root).as_posix(),
    }
    if warnings:
        result["warnings"] = warnings
    return result
