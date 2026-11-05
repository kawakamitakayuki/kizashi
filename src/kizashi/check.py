"""検証（implementation_spec.md 9章の check.py。4.6 の kizashi check）。

- V7 の数値・氏名・強い語の検査の関数（P0-4）。record が Claude の書く文字列に対して使う（4.5）。
- V1〜V4・V8 の検証と check の操作（P0-6）。不合格をすべて集めて返す。T6a・T6b の判定は check のたびに
  照合の計算から機械で決め、explanations に登録する（context='cli'）。V5・V6 と、check の時点の V7 は P0-8。

- V7_RAW_NUMBER：NFKC にした後、照合済みの引用の中と、9章の表の対象外の形（{calc:ID}、ID、型、検出、
  品質検査、年度、日付、条文）を、この順に取り除いてから、残った数字を見る。
- V7_PERSONAL_NAME：氏名の一覧（呼び出し側が原本から作って渡す。保存しない）の、姓と名をつないだ全体で
  照らす。姓や名の1語だけでは照らさない。見つけた語そのものは返さない（位置だけ）。
- W7_STRONG_WORD：強い語は警告。例外語と照合済みの引用の中は対象外。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal
from pathlib import Path

from kizashi import calc, hashing, independent_reader, ledger, paths, quotes, versions
from kizashi.adapters import FAMILIES, Adapter, load_adapter
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json, dict_rows
from kizashi.normalize import nfkc

# 9章の V7 の行の「この順に取り除く」形。
_V7_ALLOWED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\{calc:K\d{4}\}",
        r"S\d{3}:\d{6}",
        r"[SKLCM]\d{3,4}",
        r"T[1-6][abc]?",
        r"S[1-3](?![\d:])",
        r"Q\d{2}",
        r"令和(\d+|元)(・\d+)*年度",
        r"平成\d+年度",
        r"\d{4}(・\d{4})*年度",
        r"R\d+(\.\d+\.\d+)?",
        r"\d+年\d+月(\d+日)?",
        r"\d+月\d+日",
        r"(第|(会計法令?|予算決算及び会計令|予決令|地方自治法施行令)第?)\d+条(の\d+)*(第?\d+項)?(第?\d+号)?",
    )
)
_NUMBER_RE = re.compile(r"\d+(?:[,.]\d+)*")

STRONG_WORDS = ("不正", "談合", "違法", "癒着", "疑惑", "異例", "不透明", "隠した")
STRONG_WORD_EXCEPTIONS = ("不正確", "不正アクセス")


def _without_quotes(text: str, quotes: Iterable[str]) -> str:
    result = nfkc(text)
    for quote in quotes:
        q = nfkc(quote).strip()
        if q:
            result = result.replace(q, " ")
    return result


def raw_numbers(text: str, quotes: Iterable[str] = ()) -> list[str]:
    """V7_RAW_NUMBER：対象外の形を取り除いた後に残る数字の並び。"""
    rest = _without_quotes(text, quotes)
    for pattern in _V7_ALLOWED:
        rest = pattern.sub(" ", rest)
    return _NUMBER_RE.findall(rest)


def name_variants(names: Iterable[Sequence[str]]) -> list[str]:
    """氏名（(姓, 名) か、分けられない1語の (氏名,)）を、照らす形（空白なし・半角・全角）にする。"""
    variants: set[str] = set()
    for name in names:
        words = [w for w in name if w]
        if len(words) >= 2:
            surname, given = words[-2], words[-1]
            variants.update({surname + given, f"{surname} {given}", f"{surname}　{given}"})
        elif words:
            variants.add(words[0])
    return sorted(variants)


def personal_names(text: str, names: Iterable[str]) -> list[tuple[int, int]]:
    """V7_PERSONAL_NAME：氏名の一覧の語が現れる位置（NFKC にした文字列の中の [始め, 終わり)）。"""
    target = nfkc(text)
    spans: set[tuple[int, int]] = set()
    for name in {nfkc(n) for n in names if n.strip()}:
        start = target.find(name)
        while start != -1:
            spans.add((start, start + len(name)))
            start = target.find(name, start + 1)
    # 同じ位置から始まる長い一致だけを残す
    result: list[tuple[int, int]] = []
    for span in sorted(spans, key=lambda s: (s[0], -s[1])):
        if not result or span[0] >= result[-1][1]:
            result.append(span)
    return result


def name_hits(fields: Mapping[str, str], names: Iterable[str]) -> list[str]:
    """欄ごとの氏名の当たりを「V7_PERSONAL_NAME：<欄> の <始め>〜<終わり> 文字目」の形で返す。
    氏名の語そのものは書かない（record 以外の経路でも使う。D-037）。"""
    names = list(names)
    return [
        f"V7_PERSONAL_NAME：{field} の {start + 1}〜{end} 文字目"
        for field, text in fields.items()
        for start, end in personal_names(text, names)
    ]


def strong_words(text: str, quotes: Iterable[str] = ()) -> list[str]:
    """W7_STRONG_WORD：強い語（現れた順）。例外語と照合済みの引用の中は除く。"""
    rest = _without_quotes(text, quotes)
    for word in STRONG_WORD_EXCEPTIONS:
        rest = rest.replace(word, " ")
    found = [(rest.find(w), w) for w in STRONG_WORDS if w in rest]
    return [w for _, w in sorted(found)]


def string_leaves(value: object, path: str = "") -> Iterator[tuple[str, str]]:
    """提案の中の文字列の値を、入れ子（辞書・配列）も含めて (欄の道筋, 値) で返す（D-036）。

    道筋は `comparison.population`・`evidence[0].quote` の形。辞書のキーそのものは返さない。
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from string_leaves(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from string_leaves(item, f"{path}[{index}]")


def v7_findings(
    texts: Mapping[str, str],
    *,
    names: Iterable[str] = (),
    quotes: Iterable[str] = (),
    names_only: Mapping[str, str] | None = None,
    words_only: Mapping[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """欄（名前 → 文字列）ごとの V7 の不合格と警告。

    texts の欄には、数値・氏名・強い語のすべてを当てる。names_only の欄（数字を書いてよい欄や引用。
    比較の条件、単位、根拠の引用、条件の値など）には氏名の検査だけを、words_only の欄（比較の条件）には
    強い語の警告だけを当てる（D-035・D-036）。
    戻り値の不合格は {"code", "field", "hit"}（数値）か {"code", "field", "at"}（氏名。語は返さない）。
    警告は {"code": "W7_STRONG_WORD", "field", "hit"}。
    """
    names = list(names)
    quotes = list(quotes)
    errors: list[dict] = []
    warnings: list[dict] = []
    for field, text in texts.items():
        if not text:
            continue
        for hit in raw_numbers(text, quotes):
            errors.append({"code": "V7_RAW_NUMBER", "field": field, "hit": hit})
        for start, end in personal_names(text, names):
            errors.append({"code": "V7_PERSONAL_NAME", "field": field, "at": [start, end]})
        for word in strong_words(text, quotes):
            warnings.append({"code": "W7_STRONG_WORD", "field": field, "hit": word})
    for field, text in (names_only or {}).items():
        for start, end in personal_names(text, names):
            errors.append({"code": "V7_PERSONAL_NAME", "field": field, "at": [start, end]})
    for field, text in (words_only or {}).items():
        for word in strong_words(text, quotes):
            warnings.append({"code": "W7_STRONG_WORD", "field": field, "hit": word})
    return errors, warnings


# ===========================================================================
# V1〜V4・V8 と kizashi check（P0-6。実装仕様4.6・9章、設計書2.1・5.4・5.5）
# ===========================================================================

CHECK_STAGE = "check"
TYPES = ("T1", "T2", "T3", "T4", "T5", "T6a", "T6b", "T6c")
TYPE_NAMES = {
    "T1": "制度・定義の変更",
    "T2": "集計範囲・期間の違い",
    "T3": "業務・数量の変化",
    "T4": "価格・市場の条件",
    "T5": "主体の同一性",
    "T6a": "取り込みの誤り",
    "T6b": "公表形式間の食い違い",
    "T6c": "元の記載の誤り",
}
COUNTED_OUTCOMES = ("change", "artifact")  # 巡回数に数える結論の案（保留は数えない。4.6）
_QUOTE_CODES = {
    "E_QUOTE_NOT_FOUND": "V4_QUOTE_NOT_FOUND",
    "E_QUOTE_TRUNCATED": "V4_QUOTE_TRUNCATED",
    "E_QUOTE_YEAR": "V4_QUOTE_YEAR",
    "E_BAD_EVIDENCE_SOURCE": "V2_BAD_EVIDENCE_SOURCE",
}
PUBLIC_KINDS = ("dataset", "evidence", "definition")


@dataclass
class Verification:
    """1つの候補の検証の結果。failures は {"code", "message", "affected_ids"} の並び（V1〜V8 の順）。"""

    lead_id: str
    failures: list[dict] = dc_field(default_factory=list)
    t6: dict[str, str] = dc_field(default_factory=dict)
    memo_claims: list[str] = dc_field(default_factory=list)
    candidate_rows: list[str] = dc_field(default_factory=list)

    def fail(self, code: str, message: str, ids: Iterable[str] = ()) -> None:
        self.failures.append(
            {"code": code, "message": message, "affected_ids": list(dict.fromkeys(ids))}
        )


def rule_version_error(run_id: str, drift: list[str], stage: str) -> KizashiError:
    return KizashiError(
        "E_RULE_VERSION",
        stage,
        f"実行 {run_id} の規則の版が、いまの版と違う（{'・'.join(drift)}）。",
        affected_ids=[run_id],
        next_action="同じ設定で新しい実行を作る（kizashi run <分野> --config inbox/<名前>.json）。",
    )


def check_rule_versions(conn: sqlite3.Connection, root: Path, run_id: str, stage: str) -> None:
    """規則の版を照らす（3.5）。違えば E_RULE_VERSION（終了コード3）。"""
    domain, recorded = conn.execute("SELECT domain, rule_versions_json FROM runs").fetchone()
    drift = versions.drift(json.loads(recorded), root, domain)
    if drift:
        raise rule_version_error(run_id, drift, stage)


def _one(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    rows = dict_rows(conn, sql, params)
    return rows[0] if rows else None


def candidate_rows(conn: sqlite3.Connection, lead_id: str) -> list[str]:
    """候補の行（5.5・9章）：メモに出る基礎集計の row_refs のうち、率（ratio）の分母だけに使う計算の行を除いたもの。"""
    direct = set(hashing.direct_calc_ids(conn, lead_id))
    comps = {
        c: row
        for c in hashing.memo_calc_ids(conn, lead_id)
        if (row := _one(conn, "SELECT * FROM computations WHERE calc_id = ?", (c,))) is not None
    }
    denominators = set()
    for row in comps.values():
        if row["kind"] == "derived":
            request = json.loads(row["request_json"])
            if request.get("op") == "ratio" and len(request.get("args", [])) == 2:
                denominators.add(request["args"][1])
    refs: set[str] = set()
    for calc_id, row in comps.items():
        if row["kind"] != "base" or row["state"] != "active":
            continue
        if calc_id in denominators and calc_id not in direct:
            continue
        refs |= set(json.loads(row["row_refs_json"] or "[]"))
    return sorted(refs)


def _reconcile_results(conn: sqlite3.Connection, kind: str) -> list[tuple[str, dict]]:
    found = []
    for calc_id, request_json, result_json in conn.execute(
        "SELECT calc_id, request_json, result_json FROM computations WHERE kind = 'reconcile'"
        " AND state = 'active' ORDER BY calc_id"
    ):
        if json.loads(request_json).get("type") == kind and result_json:
            found.append((calc_id, json.loads(result_json)))
    return found


def _t6a(conn: sqlite3.Connection, rows: list[str]) -> tuple[str, list[str], str]:
    """T6a（取り込みの誤り）：候補の行がすべて取り込みの照合で一致なら否定、1つでも不一致なら支持。"""
    status: dict[str, str] = {}
    used: list[str] = []
    for calc_id, result in _reconcile_results(conn, "import"):
        hit = [r for r in rows if r in result.get("rows", {})]
        for rid in hit:
            status[rid] = result["rows"][rid]["row_status"]
        if hit:
            used.append(calc_id)
    bad = [r for r in rows if status.get(r) == "mismatch"]
    missing = [r for r in rows if r not in status]
    if not rows:
        return "unknown", used, "候補の行がない（メモに出る基礎集計の行がない）。"
    if bad:
        return "support", used, "取り込みの照合で一致しない候補の行：" + "・".join(bad)
    if missing:
        return "unknown", used, "取り込みの照合の記録がない候補の行：" + "・".join(missing)
    return "refute", used, "候補の行のすべてが、取り込みの照合（別経路の読み込み）で一致した。"


def _t6b(
    conn: sqlite3.Connection, adapter: Adapter, rows: list[str], category: str
) -> tuple[str, list[str], str, list[str]]:
    """T6b（公表形式間・原本との食い違い）。戻り値の最後は照合できなかった行（確認リストの先頭に出す）。"""
    cfg = adapter.config.get("reconcile") or {}
    if cfg.get("source_check") != "pdf":
        return "na", [], "分野アダプタが原本照合を対象外とした（ほかの形式がない）。", []
    fields = list((cfg.get("fields_by_category") or {}).get(category) or [])
    found: dict[str, dict] = {}
    used: list[str] = []
    for calc_id, result in _reconcile_results(conn, "source"):
        hit = [r for r in rows if r in result.get("rows", {})]
        for rid in hit:
            found[rid] = result["rows"][rid]
        if hit:
            used.append(calc_id)
    pdf_fields = cfg.get("pdf_fields") or {}
    mismatch, unverified = [], []
    for rid in rows:
        entry = found.get(rid)
        if entry is None or entry.get("row_status") != "matched":
            unverified.append(rid)
            continue
        # 分野の規則でその行に当たらない項目（その年度・方式の take に列がない項目）だけを除く（D-041④）
        applicable = [f for f in fields if _field_applies(conn, adapter, rid, pdf_fields.get(f, f))]
        values = [entry.get("fields", {}).get(f) for f in applicable]
        if "mismatch" in values:
            mismatch.append(rid)
        elif any(v != "match" for v in values):
            unverified.append(rid)
    if not rows:
        return "unknown", used, "候補の行がない（メモに出る基礎集計の行がない）。", []
    if mismatch:
        return (
            "support",
            used,
            "原本照合で項目が一致しない候補の行：" + "・".join(mismatch),
            unverified,
        )
    if unverified:
        return (
            "unknown",
            used,
            "原本照合で照合できなかった候補の行（照合不能）：" + "・".join(unverified),
            unverified,
        )
    return (
        "refute",
        used,
        f"候補の行のすべてが PDF の表の行と対応し、区分の照合項目（{'・'.join(fields)}）が一致した。",
        [],
    )


def _field_applies(
    conn: sqlite3.Connection, adapter: Adapter, record_id: str, take_key: str
) -> bool:
    """照合項目（take のキー）が、その行の年度・方式の様式にあるか。様式が分からなければ当たるものとする。"""
    row = conn.execute(
        "SELECT logical_key FROM sources WHERE source_id = ?", (record_id.split(":", 1)[0],)
    ).fetchone()
    key = row[0] if row else None
    if not key or key.count("|") != 3:
        return True
    _, year, family, _ = key.split("|")
    spec = adapter.file_spec(int(year), family)
    return spec is None or take_key in spec.take


def _register_t6(
    conn: sqlite3.Connection,
    lead_id: str,
    etype: str,
    judgment: str,
    calcs: list[str],
    searched: str,
) -> None:
    """CLI の T6 の判定を登録する。判定・根拠・searched が同じなら行を変えない（updated_at も。内容の同じ再検査で
    検証記録のハッシュを変えないため。D-041⑬）。"""
    conn.execute(
        "INSERT INTO explanations (lead_id, type, judgment, evidence_json, searched, context,"
        " updated_at) VALUES (?, ?, ?, ?, ?, 'cli', ?) ON CONFLICT (lead_id, type) DO UPDATE SET"
        " judgment = excluded.judgment, evidence_json = excluded.evidence_json,"
        " searched = excluded.searched, recalc_calc_id = NULL, residual_calc_id = NULL, scope = NULL,"
        " no_quant_reason = NULL, context = 'cli', updated_at = excluded.updated_at"
        " WHERE explanations.judgment IS NOT excluded.judgment"
        " OR explanations.evidence_json IS NOT excluded.evidence_json"
        " OR explanations.searched IS NOT excluded.searched OR explanations.context IS NOT 'cli'"
        " OR explanations.recalc_calc_id IS NOT NULL OR explanations.residual_calc_id IS NOT NULL"
        " OR explanations.scope IS NOT NULL OR explanations.no_quant_reason IS NOT NULL",
        (
            lead_id,
            etype,
            judgment,
            canonical_json([{"calc_id": c} for c in calcs]),
            searched,
            ledger.now_iso(),
        ),
    )


def raw_path(root: Path, source: dict) -> Path | None:
    """資料の原本のパス（data/raw/<sha256>.<拡張子>）。なければ None。"""
    sha = source.get("sha256")
    if not sha:
        return None
    raw = paths.raw_dir(root)
    if source["kind"] == "dataset" and source.get("logical_key"):
        candidates = [raw / f"{sha}.{source['logical_key'].rsplit('|', 1)[-1]}"]
    elif source["kind"] == "index":
        candidates = [raw / f"{sha}.html"]
    else:
        candidates = sorted(raw.glob(f"{sha}.*"))
    found = [c for c in candidates if c.is_file() and not c.is_symlink()]
    return found[0] if len(found) == 1 else None


# ---------------------------------------------------------------------------
# V1 入力と実行
# ---------------------------------------------------------------------------


def _source_of(record_id: str) -> str:
    return record_id.split(":", 1)[0]


def required_sources(
    conn: sqlite3.Connection, adapter: Adapter, config: dict, lead_id: str
) -> list[tuple[int, str]]:
    """候補が依存する資料（年度, 方式）。取得できた行からではなく、メモに出る基礎集計の条件・年度と分野の規則から
    決める（実装仕様9章の「依存する資料」。D-041⑤）。"""
    screens = adapter.config.get("screens") or {}
    every_year = sorted(int(y) for y in config.get("series_years") or [])
    needed: set[tuple[int, str]] = set()
    for calc_id in hashing.memo_calc_ids(conn, lead_id):
        comp = _one(
            conn, "SELECT kind, request_json FROM computations WHERE calc_id = ?", (calc_id,)
        )
        if comp is None or comp["kind"] != "base":
            continue
        request = json.loads(comp["request_json"])
        filters = [f for f in request.get("filter", []) if isinstance(f, dict)]
        years = _filter_values(filters, "fiscal_year")
        years = every_year if years is None else [y for y in years if isinstance(y, int)]
        screen = request.get("screen_id")
        if screen in screens:
            families = list((screens[screen] or {}).get("requires") or FAMILIES)
        else:
            chosen = _filter_values(filters, "method_family")
            families = list(FAMILIES) if chosen is None else [f for f in chosen if f in FAMILIES]
        needed |= {(y, f) for y in years for f in families}
    return sorted(needed)


def _filter_values(filters: list[dict], col: str) -> list | None:
    """条件の列の値を限る `=`・`in` の値（複数あれば共通部分）。限っていなければ None。"""
    found: set | None = None
    for f in filters:
        if f.get("col") != col or f.get("op") not in ("=", "in"):
            continue
        vals = [f.get("value")] if f.get("op") == "=" else f.get("value")
        if not isinstance(vals, list):
            continue
        found = set(vals) if found is None else found & set(vals)
    return None if found is None else sorted(found, key=str)


def _v1(conn: sqlite3.Connection, adapter: Adapter, lead: dict, v: Verification) -> None:
    run = _one(conn, "SELECT state, stages_json, counts_json, config_json FROM runs", ())
    assert run is not None
    stages, counts = json.loads(run["stages_json"]), json.loads(run["counts_json"])
    if run["state"] == "failed":
        v.fail("V1_RUN_FAILED", "実行が failed で止まっている。", [lead["lead_id"]])
    if (stages.get("quality") or {}).get("state") != "done":
        v.fail(
            "V1_QUALITY_NOT_RUN",
            "必須の品質検査が終わっていない（quality の工程）。",
            [lead["lead_id"]],
        )
    if lead["screen_id"] != "explore":
        screen = counts.get(lead["screen_id"]) or {}
        if screen.get("state") != "done":
            v.fail(
                "V1_RUN_FAILED",
                f"候補が依存する検出 {lead['screen_id']} が実行されていない。",
                [lead["lead_id"]],
            )
    quality = counts.get("quality") or {}
    records = counts.get("records") or {}
    config = json.loads(run["config_json"])
    for year, family in required_sources(conn, adapter, config, lead["lead_id"]):
        key = adapter.logical_key(year, family, "csv")
        source = _one(
            conn,
            "SELECT source_id, status FROM sources WHERE logical_key = ? AND kind = 'dataset'",
            (key,),
        )
        if source is None or source["status"] != "fetched":
            v.fail(
                "V1_RUN_FAILED",
                f"候補が依存する資料 {family}:{year} を取得できていない（{key}）。",
                [lead["lead_id"]] + ([source["source_id"]] if source else []),
            )
            continue
        sid = source["source_id"]
        if sid not in quality:
            v.fail("V1_QUALITY_NOT_RUN", f"資料 {sid} の品質検査の結果がない。", [sid])
        if int((records.get(sid) or {}).get("adopted", 0)) <= 0:
            v.fail("V1_INPUT_EMPTY", f"候補が依存する資料 {sid} の有効な行が0。", [sid])


# ---------------------------------------------------------------------------
# V2 根拠の実在
# ---------------------------------------------------------------------------


def _v2(
    conn: sqlite3.Connection,
    root: Path,
    lead: dict,
    claims: list[dict],
    explanations: list[dict],
    matches: list[dict],
    rows: list[str],
    v: Verification,
) -> None:
    lead_id = lead["lead_id"]
    for calc_id in hashing.memo_calc_ids(conn, lead_id):
        row = _one(conn, "SELECT state FROM computations WHERE calc_id = ?", (calc_id,))
        if row is None:
            v.fail("V2_REF_MISSING", f"存在しない計算への参照：{calc_id}", [calc_id])
        elif row["state"] != "active":
            v.fail("V2_STALE_OR_WITHDRAWN", f"取り下げた計算への参照：{calc_id}", [calc_id])
    for claim in claims:
        if claim["state"] == "stale":
            v.fail(
                "V2_STALE_OR_WITHDRAWN",
                f"失効した（stale）主張がある：{claim['claim_id']}",
                [claim["claim_id"]],
            )
        if claim["type"] == "record":
            evidence = json.loads(claim["evidence_json"])
            kinds = [
                (
                    _one(
                        conn,
                        "SELECT kind, status FROM sources WHERE source_id = ?",
                        (e.get("source_id"),),
                    )
                    or {}
                )
                for e in evidence
                if isinstance(e, dict)
            ]
            if not any(
                k.get("kind") in PUBLIC_KINDS and k.get("status") == "fetched" for k in kinds
            ):
                v.fail(
                    "V2_BAD_EVIDENCE_SOURCE",
                    f"原資料の記載（{claim['claim_id']}）に、取得した公的資料の位置がない。",
                    [claim["claim_id"]],
                )
    for match in matches:
        for ref in json.loads(match["left_json"]) + json.loads(match["right_json"]):
            if conn.execute("SELECT 1 FROM records WHERE record_id = ?", (ref,)).fetchone() is None:
                v.fail(
                    "V2_REF_MISSING",
                    f"対応 {match['match_id']} のレコードがない：{ref}",
                    [match["match_id"], ref],
                )
    sources = hashing.dependency_sources(conn, lead_id)
    for source_id in sources:
        source = _one(conn, "SELECT * FROM sources WHERE source_id = ?", (source_id,))
        if source is None:
            v.fail("V2_REF_MISSING", f"存在しない資料への参照：{source_id}", [source_id])
            continue
        if source["kind"] == "index":
            v.fail(
                "V2_BAD_EVIDENCE_SOURCE",
                f"索引ページ（{source_id}）を根拠にしている。",
                [source_id],
            )
        path = raw_path(root, source)
        if path is None:
            v.fail("V2_HASH_MISMATCH", f"資料 {source_id} の原本が data/raw にない。", [source_id])
        elif hashing.sha256_file(path) != source["sha256"]:
            v.fail(
                "V2_HASH_MISMATCH", f"資料 {source_id} の原本の SHA-256 が台帳と違う。", [source_id]
            )


# ---------------------------------------------------------------------------
# V3 再計算と再集計
# ---------------------------------------------------------------------------

_BASE_REQUEST_KEYS = ("op", "table", "column", "filter")


def _v3(
    conn: sqlite3.Connection,
    root: Path,
    adapter: Adapter,
    lead: dict,
    claims: list[dict],
    v: Verification,
) -> None:
    referenced = [c for claim in claims for c in json.loads(claim["calc_ids_json"])]
    referenced += hashing.text_calc_refs(conn, lead["lead_id"])  # メモが数値に展開する欄のすべて
    for calc_id in dict.fromkeys(referenced):
        row = _one(conn, "SELECT kind FROM computations WHERE calc_id = ?", (calc_id,))
        if row is not None and row["kind"] == "reconcile":
            v.fail(
                "V3_UNCITED_NUMBER",
                f"主張が数値として参照する {calc_id} は、照合の記録で数値でない。",
                [calc_id],
            )
    for calc_id in hashing.memo_calc_ids(conn, lead["lead_id"]):
        comp = _one(conn, "SELECT * FROM computations WHERE calc_id = ?", (calc_id,))
        if comp is None or comp["kind"] == "reconcile":
            continue
        if not (comp["meaning"] or "").strip() or not (comp["unit"] or "").strip():
            v.fail("V3_NO_MEANING", f"計算 {calc_id} に意味か単位がない。", [calc_id])
        request = json.loads(comp["request_json"])
        if comp["kind"] == "base":
            _v3a(conn, adapter, comp, request, v)
            _v3b(conn, root, adapter, comp, request, v)
        elif comp["kind"] == "derived":
            try:
                again = calc.derive_value(conn, request.get("op"), request.get("args"))
            except KizashiError as exc:
                v.fail(
                    "V3C_DERIVE_MISMATCH",
                    f"派生計算 {calc_id} を計算し直せない：{exc.message}",
                    [calc_id],
                )
            else:
                if again.value != comp["value"]:
                    v.fail(
                        "V3C_DERIVE_MISMATCH",
                        f"派生計算 {calc_id} を計算し直すと一致しない（{again.formula}）。",
                        [calc_id],
                    )


def _v3a(
    conn: sqlite3.Connection, adapter: Adapter, comp: dict, request: dict, v: Verification
) -> None:
    core = {k: request[k] for k in _BASE_REQUEST_KEYS if k in request}
    try:
        again = calc.base_aggregate(conn, adapter, core, allow_empty_in=comp["origin"] == "run")
    except KizashiError as exc:
        v.fail(
            "V3A_RECALC_MISMATCH",
            f"基礎集計 {comp['calc_id']} を計算し直せない：{exc.message}",
            [comp["calc_id"]],
        )
        return
    if again.value != comp["value"] or again.row_refs != sorted(
        json.loads(comp["row_refs_json"] or "[]")
    ):
        v.fail(
            "V3A_RECALC_MISMATCH",
            f"基礎集計 {comp['calc_id']} を同じ台帳で計算し直すと一致しない。",
            [comp["calc_id"]],
        )


def _v3b(
    conn: sqlite3.Connection,
    root: Path,
    adapter: Adapter,
    comp: dict,
    request: dict,
    v: Verification,
) -> None:
    """row_refs が指す原本のレコードを、見出しの文字列で列を特定する別経路で読み直して数え直す（V3(b)）。"""
    calc_id = comp["calc_id"]
    refs = sorted(json.loads(comp["row_refs_json"] or "[]"))
    column = request.get("column") if request.get("op") == "sum" else None
    raw_key = None
    if column is not None:
        raw_key = (getattr(adapter.module, "REAGG_COLUMNS", {}) or {}).get(column)
        if raw_key is None:
            v.fail(
                "V3B_HEADER_UNRESOLVED",
                f"基礎集計 {calc_id} の列 {column} を原本で読み直す列の対応がない。",
                [calc_id],
            )
            return
    by_source: dict[str, list[int]] = {}
    for ref in refs:
        source_id, number = ref.split(":", 1)
        by_source.setdefault(source_id, []).append(int(number))
    total, null_rows, found = Decimal(0), 0, 0
    for source_id, numbers in sorted(by_source.items()):
        source = _one(conn, "SELECT * FROM sources WHERE source_id = ?", (source_id,))
        path = None if source is None else raw_path(root, source)
        if source is None or path is None or not source.get("logical_key"):
            v.fail(
                "V3B_REAGG_MISMATCH",
                f"基礎集計 {calc_id} の行の原本（{source_id}）を読めない。",
                [calc_id, source_id],
            )
            return
        _, year, family, _ = source["logical_key"].split("|")
        spec = adapter.file_spec(int(year), family)
        expected = {"no": str(spec.take["no"]["header"])}
        if raw_key is not None:
            expected[raw_key] = str(spec.take[raw_key]["header"])
        try:
            table = independent_reader.read_by_headers(
                path, spec.encoding, expected, stage=CHECK_STAGE
            )
        except KizashiError as exc:
            v.fail(
                exc.code if exc.code.startswith("V3B") else "V3B_REAGG_MISMATCH",
                exc.message,
                [calc_id, source_id],
            )
            return
        values = dict(table.records)
        for number in numbers:
            if number not in values:
                continue
            found += 1
            if raw_key is not None:
                amount = independent_reader.parse_amount_yen(values[number].get(raw_key))
                if amount is None:
                    null_rows += 1
                else:
                    total += Decimal(amount)
    if column is None:
        same = str(found) == comp["value"]
    else:
        same = (
            found == len(refs)
            and calc.fmt(total) == comp["value"]
            and null_rows == int(request.get("null_rows", 0))
        )
    if not same:
        v.fail(
            "V3B_REAGG_MISMATCH",
            f"基礎集計 {calc_id} を原本から別経路で数え直すと一致しない。",
            [calc_id],
        )


# ---------------------------------------------------------------------------
# V4 引用の照合
# ---------------------------------------------------------------------------


def _v4(
    conn: sqlite3.Connection,
    root: Path,
    run_id: str,
    adapter: Adapter,
    lead: dict,
    claims: list[dict],
    explanations: list[dict],
    matches: list[dict],
    v: Verification,
) -> None:
    targets: list[tuple[str, object, dict]] = []
    for claim in claims:
        for e in json.loads(claim["evidence_json"]):
            targets.append(
                (
                    claim["claim_id"],
                    e,
                    {"claim_type": claim["type"], "fiscal_year": claim["fiscal_year"]},
                )
            )
    for exp in explanations:
        if exp["context"] == "cli":
            continue
        for e in json.loads(exp["evidence_json"]):
            targets.append((f"{lead['lead_id']}・{exp['type']}", e, {}))
    for match in matches:
        for e in json.loads(match["evidence_json"]):
            targets.append((match["match_id"], e, {}))
    for calc_id in hashing.memo_calc_ids(conn, lead["lead_id"]):
        comp = _one(
            conn, "SELECT kind, request_json FROM computations WHERE calc_id = ?", (calc_id,)
        )
        if comp is not None and comp["kind"] == "doc_value":
            targets.append((calc_id, {**json.loads(comp["request_json"]), "role": "support"}, {}))
    for owner, evidence, kw in targets:
        try:
            quotes.verify_quote(conn, root, run_id, adapter, evidence, **kw)
        except KizashiError as exc:
            code = _QUOTE_CODES.get(exc.code, "V2_REF_MISSING")
            v.fail(code, f"{owner} の根拠：{exc.message}", [owner, *exc.affected_ids])


# ---------------------------------------------------------------------------
# V8 結論ごとの要件
# ---------------------------------------------------------------------------


def _v8(
    conn: sqlite3.Connection,
    adapter: Adapter,
    lead: dict,
    claims: list[dict],
    matches: list[dict],
    v: Verification,
) -> None:
    lead_id = lead["lead_id"]
    outcome = lead["proposed_outcome"]
    judged = {
        r["type"]: r
        for r in dict_rows(conn, "SELECT * FROM explanations WHERE lead_id = ?", (lead_id,))
    }
    questions = json.loads(lead["questions_json"])
    missing: list[str] = []
    if outcome is None:
        v.fail("V8_REQUIRED_MISSING", "結論の案（kind: conclusion）がない。", [lead_id])
        return
    absent = [f"{t} {TYPE_NAMES[t]}" for t in TYPES if t not in judged]
    if absent:
        missing.append("6つの型の判定（" + "、".join(absent) + "）")

    def j(t: str) -> str | None:
        return judged[t]["judgment"] if t in judged else None

    def has_evidence(t: str) -> bool:
        return t in judged and bool(json.loads(judged[t]["evidence_json"]))

    adjusted = lead["comparison_state"] == "adjusted" and bool(lead["adjustment_json"])
    if outcome == "change":
        if not lead["hypothesis"] or not lead["withdraw_if"]:
            missing.append("仮説と取り下げ条件")
        if not [c for c in claims if c["state"] != "stale"]:
            missing.append("照合済みの記載と計算（主張）")
        if not questions:
            missing.append("取材質問（1件以上）")
        if lead["hypothesis_result"] is None:
            v.fail(
                "V8_NO_WITHDRAW_RESULT",
                "比較成立なのに、取り下げ条件の結果（kept・withdrawn）がない。",
                [lead_id],
            )
        if not _has_purpose(questions, "verify_record"):
            v.fail(
                "V8_T6C_QUESTION",
                "比較成立なのに、元の記載を確かめる取材質問（purposes に verify_record）がない。",
                [lead_id],
            )
        _v8_matches(conn, lead, matches, v)
        coefs = [
            c["key"]
            for c in json.loads(lead["coefficients_json"])
            if c.get("status") in ("proposed", "accepted")
        ]
        if coefs:
            v.fail(
                "V8_COEFFICIENT_PENDING",
                f"比較成立なのに、係数の提案が残っている（{'・'.join(coefs)}）。記者が不要と判断したら declined にする。",
                [lead_id],
            )
        not_negated = []
        if j("T6a") != "refute":
            not_negated.append("T6a 取り込みの誤り")
        if j("T6b") not in ("refute", "na"):
            not_negated.append("T6b 公表形式間の食い違い")
        if j("T2") != "refute" and not adjusted:
            not_negated.append("T2 集計範囲・期間の違い（否定か、補正前後の値のある adjusted）")
        if (
            lead["change_category"] in getattr(adapter.module, "ENTITY_CATEGORIES", ())
            and j("T5") != "refute"
            and not adjusted
        ):
            not_negated.append("T5 主体の同一性")
        if not_negated:
            v.fail(
                "V8_CHANGE_NOT_NEGATED",
                "比較成立なのに否定されていない型がある：" + "、".join(not_negated),
                [lead_id],
            )
        _v8_explanation_status(conn, lead, judged, questions, v)
    elif outcome == "artifact":
        factors = [t for t in ("T2", "T5", "T6a", "T6b") if j(t) == "support" and has_evidence(t)]
        if not factors:
            v.fail(
                "V8_ARTIFACT_NO_FACTOR",
                "見かけの変化なのに、T2・T5・T6a・T6b のどれも根拠付きで支持されていない。",
                [lead_id],
            )
        if lead["comparison_state"] == "adjusted" and not lead["adjustment_json"]:
            missing.append("補正の方法と補正前後の値")
    else:  # hold
        if lead["hold_reason"] is None:
            v.fail("V8_HOLD_REASON", "保留なのに保留の理由（hold_reason）がない。", [lead_id])
        if not _has_purpose(questions, "missing_documents"):
            v.fail(
                "V8_HOLD_REASON",
                "保留なのに、足りない資料を照会する取材質問（purposes に missing_documents。照会先は to、"
                "情報公開請求の候補は foia）がない。",
                [lead_id],
            )
    if missing:
        v.fail("V8_REQUIRED_MISSING", "結論の必須の中身がない：" + "、".join(missing), [lead_id])


def _v8_matches(conn: sqlite3.Connection, lead: dict, matches: list[dict], v: Verification) -> None:
    """V8_MATCH_UNCONFIRMED：calc_from の行を confirmed の対応の left が、calc_to の行を right が覆うか。"""

    def refs(calc_id: str | None) -> set[str]:
        row = _one(conn, "SELECT row_refs_json FROM computations WHERE calc_id = ?", (calc_id,))
        return set(json.loads((row or {}).get("row_refs_json") or "[]"))

    confirmed = [m for m in matches if m["status"] == "confirmed"]
    left = (
        set().union(*(set(json.loads(m["left_json"])) for m in confirmed)) if confirmed else set()
    )
    right = (
        set().union(*(set(json.loads(m["right_json"])) for m in confirmed)) if confirmed else set()
    )
    uncovered = sorted((refs(lead["calc_from"]) - left) | (refs(lead["calc_to"]) - right))
    if uncovered:
        v.fail(
            "V8_MATCH_UNCONFIRMED",
            "比較成立なのに、比べる両年度の行が confirmed の同種業務の対応で覆われていない："
            + "・".join(uncovered)
            + "。同種と確かめられなければ保留（hold_reason: comparison）にする。",
            [lead["lead_id"], *uncovered],
        )


def _has_purpose(questions: list, purpose: str) -> bool:
    """目的（purposes）にその値を持つ取材質問があるか（D-041⑩。質問の中身の十分さは記者が確かめる）。"""
    return any(purpose in (q.get("purposes") or []) for q in questions if isinstance(q, dict))


def _residual_ok(conn: sqlite3.Connection, lead: dict, exp: dict) -> bool:
    """説明を反映した値（recalc_calc_id）と残る差（residual_calc_id）が計算IDでそろっているか（D-041⑩）。

    説明を反映した値は、候補の比べる2年度と系列の計算そのものではないこと。残る差は、説明を反映した値を引数に
    含む派生計算か、条件をそろえた後に残る行を数えた基礎集計の件数。"""
    recalc, residual = exp.get("recalc_calc_id"), exp.get("residual_calc_id")
    originals = {lead["calc_from"], lead["calc_to"], *json.loads(lead["calc_ids_json"])}
    if not recalc or not residual or recalc in originals:
        return False
    comp = _one(conn, "SELECT kind, request_json FROM computations WHERE calc_id = ?", (residual,))
    if comp is None:
        return False
    request = json.loads(comp["request_json"])
    if comp["kind"] == "derived":
        return recalc in (request.get("args") or [])
    return comp["kind"] == "base" and request.get("op") == "count"


def _v8_explanation_status(
    conn: sqlite3.Connection, lead: dict, judged: dict, questions: list, v: Verification
) -> None:
    status = lead["explanation_status"]
    supported = [
        t for t in ("T1", "T3", "T4") if t in judged and judged[t]["judgment"] == "support"
    ]
    if supported and status == "none":
        v.fail(
            "V8_EXPLANATION_STATUS",
            f"支持された説明（{'・'.join(supported)}）があるのに、説明の状況が「なし」。",
            [lead["lead_id"]],
        )
    scoped = [t for t in supported if judged[t]["scope"] in ("full", "part")]
    if status in ("full", "part") and not any(_residual_ok(conn, lead, judged[t]) for t in scoped):
        v.fail(
            "V8_EXPLANATION_STATUS",
            "説明の状況が「全部」「一部」なのに、支持の T1・T3・T4 に、説明を反映した値（recalc_calc_id。"
            "比べる年度の合計そのものは不可）と残る差（residual_calc_id）がない。",
            [lead["lead_id"]],
        )
    if status == "unknown" and not _has_purpose(questions, "reason_breakdown"):
        v.fail(
            "V8_EXPLANATION_STATUS",
            "説明の状況が「範囲不明」なのに、理由と内訳を尋ねる取材質問（purposes に reason_breakdown）がない。",
            [lead["lead_id"]],
        )


# ---------------------------------------------------------------------------
# 全体
# ---------------------------------------------------------------------------


def memo_claims(conn: sqlite3.Connection, lead_id: str) -> list[dict]:
    """メモに出る主張（取り下げたものを除く）。"""
    return dict_rows(
        conn,
        "SELECT * FROM claims WHERE lead_id = ? AND state <> 'withdrawn' ORDER BY claim_id",
        (lead_id,),
    )


def verify(
    conn: sqlite3.Connection, root: Path, run_id: str, adapter: Adapter, lead_id: str
) -> Verification:
    """V1〜V4・V8 を行い、不合格をすべて集める。T6a・T6b の判定を explanations に登録する（呼び出し側の
    トランザクションの中で）。"""
    lead = _one(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    if lead is None:
        raise KizashiError(
            "E_REF_NOT_FOUND",
            CHECK_STAGE,
            f"候補がない：{lead_id}",
            affected_ids=[lead_id],
            next_action=f"kizashi inspect {run_id} で候補IDを確かめる。",
        )
    v = Verification(lead_id=lead_id)
    rows = candidate_rows(conn, lead_id)
    v.candidate_rows = rows
    t6a, a_calcs, a_text = _t6a(conn, rows)
    t6b, b_calcs, b_text, _ = _t6b(conn, adapter, rows, lead["change_category"])
    _register_t6(conn, lead_id, "T6a", t6a, a_calcs, a_text)
    _register_t6(conn, lead_id, "T6b", t6b, b_calcs, b_text)
    v.t6 = {"T6a": t6a, "T6b": t6b}
    claims = memo_claims(conn, lead_id)
    explanations = dict_rows(
        conn, "SELECT * FROM explanations WHERE lead_id = ? ORDER BY type", (lead_id,)
    )
    matches = dict_rows(
        conn, "SELECT * FROM procurement_matches WHERE lead_id = ? ORDER BY match_id", (lead_id,)
    )
    v.memo_claims = [c["claim_id"] for c in claims]
    _v1(conn, adapter, lead, v)
    _v2(conn, root, lead, claims, explanations, matches, rows, v)
    _v3(conn, root, adapter, lead, claims, v)
    _v4(conn, root, run_id, adapter, lead, claims, explanations, matches, v)
    _v8(conn, adapter, lead, claims, matches, v)
    return v


def failure_error(v: Verification, stage: str) -> KizashiError:
    """不合格のすべてを1つのエラーにする（code は最初の不合格。failures に全件）。"""
    lines = [f"{f['code']}：{f['message']}" for f in v.failures]
    ids = [i for f in v.failures for i in f["affected_ids"]]
    return KizashiError(
        v.failures[0]["code"],
        stage,
        f"候補 {v.lead_id} の検証に不合格が{len(v.failures)}件ある：" + "／".join(lines),
        affected_ids=list(dict.fromkeys([v.lead_id, *ids])),
        next_action="failures を1件ずつ直す（record で足す・取り下げる）。直せないものは保留の結論の案にする。",
        failures=v.failures,
    )


def mark_checked(conn: sqlite3.Connection, claim_ids: Iterable[str]) -> list[str]:
    ids = list(claim_ids)
    conn.executemany(
        "UPDATE claims SET state = 'checked' WHERE claim_id = ? AND state IN ('draft', 'held', 'checked')",
        [(c,) for c in ids],
    )
    return ids


def run_check(root: Path, run_id: str, lead_id: str) -> dict:
    """kizashi check <lead_id> --run <run_id>（4.6）。"""
    conn = ledger.open_rw(root, run_id)
    try:
        check_rule_versions(conn, root, run_id, CHECK_STAGE)
        (domain,) = conn.execute("SELECT domain FROM runs").fetchone()
        adapter = load_adapter(root, domain)
        with ledger.transaction(conn):
            v = verify(conn, root, run_id, adapter, lead_id)
            lead = _one(
                conn, "SELECT proposed_outcome, rounds FROM leads WHERE lead_id = ?", (lead_id,)
            )
            assert lead is not None
            counted = lead["proposed_outcome"] in COUNTED_OUTCOMES
            rounds = lead["rounds"] + (1 if counted else 0)
            if counted:
                conn.execute("UPDATE leads SET rounds = ? WHERE lead_id = ?", (rounds, lead_id))
            checked = [] if v.failures else mark_checked(conn, v.memo_claims)
            digest = hashing.check_hash(conn, lead_id)
            ledger.add_event(
                conn,
                lead_id,
                "check",
                after={"rounds": rounds, "counted": counted, "t6": v.t6},
                result={"passed": not v.failures, "failures": [f["code"] for f in v.failures]},
                check_hash=digest,
            )
    finally:
        conn.close()
    if v.failures:
        raise failure_error(v, CHECK_STAGE)
    return {
        "run_id": run_id,
        "lead_id": lead_id,
        "passed": True,
        "t6": v.t6,
        "checked_claims": checked,
        "rounds": rounds,
        "check_hash": digest,
        "message": "合格した。メモに出る主張を checked にした。メモは kizashi render <lead_id> --run <run_id> で書く。",
    }
