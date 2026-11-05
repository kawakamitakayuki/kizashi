"""品質検査（アダプタ仕様5章の Q01〜Q16）と品質レポート（implementation_spec.md 2章の quality.py）。

- 「必須」の検査が1つでも実行できない、または不合格なら、品質検査の工程は失敗（実行は failed）。
- Q13 のファイル単位の突合は、CSV のレコードの数・台帳のレコードの数・数値の金額の行の数と合計（Decimal）を、
  別経路の読み込み処理（independent_reader.py）と正規化の経路で比べ、違えば不合格（実行は failed）。
- Q06 は所属の異なり数だけを書き、一覧は品質レポートに書かない（データの更新で氏名の断片が混じる
  おそれを避けるため。行ごとの所属は台帳の officer_units_json にある。D-028）。
- 金額の合計は Decimal で行い、SQLite の SUM を使わない。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from kizashi import independent_reader, paths, readers
from kizashi import normalize as nz
from kizashi.adapters import Adapter, FileSpec
from kizashi.adapters.cao_procurement import OFFICER_KEY, format_problems
from kizashi.errors import KizashiError
from kizashi.hashing import sha256_bytes

REPORT_NAME = "quality_report.md"
MAX_LISTED = 20


@dataclass(frozen=True)
class Check:
    qid: str
    title: str
    required: bool


CHECKS: tuple[Check, ...] = (
    Check("Q01", "文字コードで読めるか", True),
    Check("Q02", "見出しの文字列が期待と一致するか（取り込む列を一意に特定できるか）", True),
    Check("Q03", "列の数が期待どおりか", True),
    Check("Q04", "取り込まない列（氏名の入りうる列、内部用の列）を除いたか", True),
    Check("Q05", "担当官のセルから氏名を除けたか（分けられないセルの件数）", True),
    Check("Q06", "所属の一覧", False),
    Check("Q07", "法人番号の状態ごとの件数", False),
    Check("Q08", "NO以外が同じ行の組", True),
    Check("Q09", "複合キー（件名・契約日・金額・相手方）が一意でない行", False),
    Check("Q10", "単価契約の件数", False),
    Check("Q11", "数値にならない金額の件数（種類ごと）", False),
    Check("Q12", "日付を読めない行", True),
    Check("Q13", "ファイル単位の突合（行数と金額の合計）", True),
    Check("Q14", "NOの欠番・重複", False),
    Check("Q15", "予定価格の非公表・落札率の「-」の件数", False),
    Check("Q16", "年度途中のファイル", False),
)
CHECK_BY_ID = {c.qid: c for c in CHECKS}

# 状態：pass（合格）、fail（不合格）、report（報告）、needs_review（人が確かめる）、
# partial（一部だけ実施）、not_run（実行できなかった）。
STATE_LABELS = {
    "pass": "合格",
    "fail": "不合格",
    "report": "報告",
    "needs_review": "要確認",
    "partial": "一部実施",
    "not_run": "未実行",
}
FAILING = frozenset({"fail", "not_run"})


@dataclass
class SourceResult:
    source_id: str
    logical_key: str
    raw_path: str
    results: dict[str, dict] = field(default_factory=dict)

    def set(self, qid: str, state: str, detail: str, **values: object) -> None:
        self.results[qid] = {"state": state, "detail": detail, **values}


@dataclass
class QualityOutcome:
    report_markdown: str
    counts: dict
    # (品質検査のID。資料がないときは None, 状態, 内容)
    failures: list[tuple[str | None, str, str]]


def _yen(value: Decimal) -> str:
    return f"{value:,}"


def _list(items: list[str]) -> str:
    shown = "、".join(items[:MAX_LISTED])
    return shown + (f" ほか{len(items) - MAX_LISTED}件" if len(items) > MAX_LISTED else "")


def _source_rows(conn: sqlite3.Connection, source_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT r.record_id, r.logical_no, r.cells_json, r.status, r.status_reason, p.*"
        " FROM records r LEFT JOIN procurement_contracts p ON p.record_id = r.record_id"
        " WHERE r.source_id = ? ORDER BY r.logical_no",
        (source_id,),
    ).fetchall()


def _check_source(
    conn: sqlite3.Connection, root: Path, adapter: Adapter, source: sqlite3.Row
) -> SourceResult:
    source_id = source["source_id"]
    logical_key = source["logical_key"]
    raw_path = paths.raw_dir(root) / f"{source['sha256']}.csv"
    result = SourceResult(source_id, logical_key, f"data/raw/{raw_path.name}")
    _, year, family, _ = logical_key.split("|")
    spec = adapter.file_spec(int(year), family)

    def guarded(qid: str, func: Callable[[], None]) -> None:
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            result.set(qid, "not_run", f"実行できなかった（{type(exc).__name__}: {exc}）")

    table: readers.CsvTable | None = None

    def q01() -> None:
        nonlocal table
        if spec is None:
            result.set("Q01", "not_run", "dataset.yaml にこの年度・方式の設定がない")
            return
        if not raw_path.is_file():
            result.set("Q01", "not_run", "原本のファイルがない")
            return
        data = raw_path.read_bytes()
        if sha256_bytes(data) != source["sha256"]:
            result.set("Q01", "fail", "原本のハッシュが台帳と違う")
            return
        try:
            table = readers.read_table(data, spec.encoding)
        except readers.DecodeError as exc:
            result.set("Q01", "fail", str(exc))
            return
        result.set("Q01", "pass", f"{spec.encoding} で読めた", encoding=spec.encoding)

    guarded("Q01", q01)
    rows = _source_rows(conn, source_id)
    contracts = [r for r in rows if r["no"] is not None]

    if table is None or spec is None:
        for qid in ("Q02", "Q03", "Q04", "Q13"):
            result.set(qid, "not_run", "Q01 が通らなかったため実行できない")
    else:
        _format_checks(result, table, spec, rows)
        guarded("Q13", lambda: _q13(result, table, raw_path, spec, rows, contracts))

    guarded("Q05", lambda: _q05(result, contracts))
    guarded("Q06", lambda: _q06(result, contracts))
    guarded("Q07", lambda: _q07(result, contracts))
    guarded("Q08", lambda: _q08(result, contracts))
    guarded("Q09", lambda: _q09(result, rows))
    guarded("Q10", lambda: _q10(result, contracts))
    guarded("Q11", lambda: _q11(result, contracts))
    guarded("Q12", lambda: _q12(result, contracts))
    guarded("Q14", lambda: _q14(result, rows, contracts))
    guarded("Q15", lambda: _q15(result, contracts))
    guarded("Q16", lambda: _q16(result, spec, contracts))
    return result


def _format_checks(
    result: SourceResult, table: readers.CsvTable, spec: FileSpec, rows: list[sqlite3.Row]
) -> None:
    problems = format_problems(table, spec)
    count_problems = [m for q, m in problems if q == "Q03"]
    header_problems = [m for q, m in problems if q == "Q02"]
    mapping_problems = [m for q, m in problems if q == "Q04"]
    if count_problems:
        result.set("Q03", "fail", "／".join(count_problems))
    else:
        result.set("Q03", "pass", f"{spec.expected_columns}列", columns=spec.expected_columns)
    if count_problems:
        result.set("Q02", "not_run", "列の数が違うため見出しを照らせない")
    elif header_problems:
        result.set("Q02", "fail", "／".join(header_problems))
    else:
        result.set("Q02", "pass", f"取り込む{len(spec.take)}列の見出しが一意に一致した")

    expected_keys = {k for k in spec.take if k != OFFICER_KEY}
    wrong = []
    for r in rows:
        keys = set(json.loads(r["cells_json"]))
        want = set() if r["status_reason"] == "empty_record" else expected_keys
        if keys != want:
            wrong.append(r["record_id"])
    if mapping_problems or wrong:
        detail = "／".join(
            mapping_problems + ([f"元のセルの値のキーが違う行：{_list(wrong)}"] if wrong else [])
        )
        result.set("Q04", "fail", detail)
    else:
        result.set(
            "Q04",
            "pass",
            f"取り込まない{len(spec.drop)}列と担当官のセルを、元のセルの値に入れていない",
            dropped_columns=list(spec.drop),
        )


def _q05(result: SourceResult, contracts: list[sqlite3.Row]) -> None:
    bad = [r["record_id"] for r in contracts if None in json.loads(r["officer_units_json"])]
    if bad:
        result.set(
            "Q05", "needs_review", f"分けられないセルが{len(bad)}件：{_list(bad)}", count=len(bad)
        )
    else:
        result.set("Q05", "pass", "分けられないセルは0件", count=0)


def _q06(result: SourceResult, contracts: list[sqlite3.Row]) -> None:
    units = {u for r in contracts for u in json.loads(r["officer_units_json"]) if u is not None}
    result.set(
        "Q06",
        "report",
        f"所属の異なり数 {len(units)}（一覧は品質レポートに書かない。D-028）",
        distinct=len(units),
    )


def _q07(result: SourceResult, contracts: list[sqlite3.Row]) -> None:
    counts = Counter(r["corp_number_state"] for r in contracts)
    order = ("digits13", "exponent", "missing", "multiple", "invalid")
    detail = "、".join(f"{s} {counts.get(s, 0)}" for s in order)
    result.set("Q07", "report", detail, counts={s: counts.get(s, 0) for s in order})


def _q08(result: SourceResult, contracts: list[sqlite3.Row]) -> None:
    groups: dict[str, list[str]] = defaultdict(list)
    for r in contracts:
        if r["dup_group"]:
            groups[r["dup_group"]].append(r["record_id"])
    rows = sum(len(v) for v in groups.values())
    detail = f"{len(groups)}組（{rows}行）"
    if groups:
        detail += (
            "："
            + "／".join("・".join(v) for v in groups.values())
            + "。除かずに、比較できない理由 duplicate_rows を付けた"
        )
    result.set("Q08", "report", detail, groups=len(groups), rows=rows)


def _q09(result: SourceResult, rows: list[sqlite3.Row]) -> None:
    keys: dict[tuple, list[str]] = defaultdict(list)
    for r in rows:
        cells = json.loads(r["cells_json"])
        if not cells:
            continue
        key = tuple(
            cells.get(k)
            for k in ("title_raw", "contract_date_raw", "amount_raw", "counterparty_raw")
        )
        keys[key].append(r["record_id"])
    groups = [v for v in keys.values() if len(v) >= 2]
    n = sum(len(g) for g in groups)
    detail = f"{len(groups)}組（{n}行）"
    if groups:
        detail += "：" + "／".join("・".join(g) for g in groups[:MAX_LISTED])
    result.set("Q09", "report", detail, groups=len(groups), rows=n)


def _q10(result: SourceResult, contracts: list[sqlite3.Row]) -> None:
    n = sum(1 for r in contracts if r["unit_price"] == 1)
    result.set("Q10", "report", f"{n}件", count=n)


def _q11(result: SourceResult, contracts: list[sqlite3.Row]) -> None:
    counts = Counter(r["amount_note"] for r in contracts if r["amount_note"] is not None)
    order = ("missing", "foreign_currency", "unit_price_text", "non_numeric")
    detail = "、".join(f"{k} {counts.get(k, 0)}" for k in order)
    result.set("Q11", "report", detail, counts={k: counts.get(k, 0) for k in order})


def _q12(result: SourceResult, contracts: list[sqlite3.Row]) -> None:
    bad = [r["record_id"] for r in contracts if r["contract_date"] is None]
    detail = f"{len(bad)}行" + (
        f"：{_list(bad)}。比較できない理由 bad_date を付けた" if bad else ""
    )
    result.set("Q12", "report", detail, count=len(bad), row_refs=bad)


def _q13(
    result: SourceResult,
    table: readers.CsvTable,
    raw_path: Path,
    spec: FileSpec,
    rows: list[sqlite3.Row],
    contracts: list[sqlite3.Row],
) -> None:
    """ファイル単位の突合。正規化の経路（readers と分野の表の amount_yen）と、別経路の読み込み処理
    （見出しの文字列で金額の列を特定し、4.4 の規則を独立に書いたもの）で、件数と金額の合計を比べる。"""
    csv_records = len(table.records)
    total = sum(
        (Decimal(str(r["amount_yen"])) for r in contracts if r["amount_yen"] is not None),
        Decimal(0),
    )
    summed = sum(1 for r in contracts if r["amount_yen"] is not None)
    values: dict = {
        "csv_records": csv_records,
        "ledger_records": len(rows),
        "contracts": len(contracts),
        "amount_rows": summed,
        "amount_total": str(total),
    }
    base = (
        f"正規化の経路：CSVのレコード{csv_records}件、台帳のレコード{len(rows)}件（分野の表{len(contracts)}件）、"
        f"金額が数値の{summed}行の合計 {_yen(total)}円"
    )
    try:
        reread = independent_reader.read_by_headers(
            raw_path,
            spec.encoding,
            {"amount_raw": str(spec.take["amount_raw"]["header"])},
            stage="quality",
        )
    except KizashiError as exc:
        result.set("Q13", "fail", f"{base}。別経路の読み込み処理：{exc.message}", **values)
        return
    amounts = [independent_reader.parse_amount_yen(v["amount_raw"]) for _, v in reread.records]
    reader_total = sum((Decimal(a) for a in amounts if a is not None), Decimal(0))
    values |= {
        "reader_records": len(reread.records),
        "reader_amount_rows": sum(1 for a in amounts if a is not None),
        "reader_amount_total": str(reader_total),
    }
    base += (
        f"。別経路の読み込み処理：CSVのレコード{values['reader_records']}件、"
        f"金額が数値の{values['reader_amount_rows']}行の合計 {_yen(reader_total)}円（どちらも Decimal）"
    )
    differs = []
    if not csv_records == len(rows) == values["reader_records"]:
        differs.append("件数")
    if summed != values["reader_amount_rows"]:
        differs.append("金額が数値の行の数")
    if total != reader_total:
        differs.append("金額の合計")
    if differs:
        result.set("Q13", "fail", f"{base}。{'・'.join(differs)}が違う", **values)
    else:
        result.set("Q13", "pass", f"{base}。一致した", **values)


def _q14(result: SourceResult, rows: list[sqlite3.Row], contracts: list[sqlite3.Row]) -> None:
    numbers = [r["no"] for r in contracts]
    counts = Counter(numbers)
    dups = sorted(n for n, c in counts.items() if c > 1)
    missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers)) if numbers else []
    bad_no = [r["record_id"] for r in rows if r["status_reason"] == "bad_no"]
    parts = [f"欠番 {len(missing)}", f"重複 {len(dups)}"]
    if missing:
        parts.append("欠番のNO：" + _list([str(n) for n in missing]))
    if dups:
        parts.append("重複のNO：" + _list([str(n) for n in dups]))
    if bad_no:
        parts.append(f"NOが数でない行（保留） {len(bad_no)}：{_list(bad_no)}")
    result.set("Q14", "report", "、".join(parts), missing=missing, duplicates=dups, bad_no=bad_no)


def _q15(result: SourceResult, contracts: list[sqlite3.Row]) -> None:
    est_missing = sum(1 for r in contracts if nz.is_missing(r["estimate_raw"]))
    est_text = sum(
        1
        for r in contracts
        if not nz.is_missing(r["estimate_raw"]) and nz.parse_amount(r["estimate_raw"])[0] is None
    )
    rate_dash = sum(1 for r in contracts if nz.is_missing(r["rate_raw"]))
    result.set(
        "Q15",
        "report",
        f"予定価格が数値でない {est_missing + est_text}件（「-」・空欄 {est_missing}、文言 {est_text}）、"
        f"落札率が「-」・空欄 {rate_dash}件",
        estimate_missing=est_missing,
        estimate_text=est_text,
        rate_dash=rate_dash,
    )


def _q16(result: SourceResult, spec: FileSpec | None, contracts: list[sqlite3.Row]) -> None:
    if spec is not None and spec.partial_year:
        n = sum(1 for r in contracts if r["partial_year"] == 1)
        result.set(
            "Q16", "report", f"年度途中のファイル。{n}行に partial_year を付けた", partial_year=True
        )
    else:
        result.set("Q16", "report", "年度途中のファイルではない", partial_year=False)


# ---------------------------------------------------------------------------
# 実行全体
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "planned": "予定",
    "fetched": "取得済み",
    "not_published": "非公表（公表ページにリンクがない）",
    "failed": "取得失敗",
    "out_of_scope": "対象外",
}


def run_quality(
    conn: sqlite3.Connection, root: Path, run_id: str, adapter: Adapter
) -> QualityOutcome:
    """取得済みの CSV ごとに Q01〜Q16 を行い、品質レポートの本文と件数を返す。"""
    conn.row_factory = sqlite3.Row
    try:
        sources = conn.execute(
            "SELECT source_id, logical_key, kind, url, sha256, status, failure_reason FROM sources"
            " ORDER BY source_id"
        ).fetchall()
        csv_sources = [
            s
            for s in sources
            if s["kind"] == "dataset"
            and s["status"] == "fetched"
            and s["logical_key"].endswith("|csv")
        ]
        failures: list[tuple[str | None, str, str]] = []
        results = [_check_source(conn, root, adapter, s) for s in csv_sources]
    finally:
        conn.row_factory = None
    for res in results:
        for check in CHECKS:
            if check.qid not in res.results:
                res.set(check.qid, "not_run", "実行されなかった")
            state = res.results[check.qid]["state"]
            if check.required and state in FAILING:
                text = f"{res.source_id} {check.qid}（{check.title}）：{STATE_LABELS[state]}"
                failures.append((check.qid, state, text))
    if not csv_sources:
        failures.append((None, "not_run", "品質検査を行う資料（取得済みのCSV）がない"))

    counts = {res.source_id: {"logical_key": res.logical_key, **res.results} for res in results}
    return QualityOutcome(
        report_markdown=_render(run_id, adapter, sources, results, [t for _, _, t in failures]),
        counts=counts,
        failures=failures,
    )


def _render(
    run_id: str,
    adapter: Adapter,
    sources: list[sqlite3.Row],
    results: list[SourceResult],
    failures: list[str],
) -> str:
    lines = [
        "# 品質レポート",
        "",
        f"> 実行ID：{run_id}　分野：{adapter.domain}　読み込み処理の版：{adapter.parser_version}",
        "> 氏名は保存していない。担当官の所属は異なり数だけを書き、一覧は書かない（D-028）。",
        "",
        "## 結果",
        "",
    ]
    if failures:
        lines.append("必須の検査に、不合格または実行できなかったものがある（実行は failed）。")
        lines.extend(f"- {f}" for f in failures)
    else:
        lines.append("必須の検査はすべて実行し、不合格はなかった（要確認・報告の項目は下の表）。")
    lines += [
        "",
        "## 資料",
        "",
        "| 資料ID | 種類 | 論理資料 | 状態 | 理由 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in sources:
        reason = (s["failure_reason"] or "").replace("|", "／").replace("\n", " ")
        lines.append(
            f"| {s['source_id']} | {s['kind']} | {s['logical_key'] or '—'} | "
            f"{_STATUS_LABELS.get(s['status'], s['status'])} | {reason or '—'} |"
        )
    lines.append("")
    lines.append(
        "取り込みの照合（T6a）と PDF の原本照合（T6b）は、品質検査の後の工程（reconcile_import・"
        "reconcile_source）で行い、結果を台帳の照合の計算（computations の kind='reconcile'）と"
        " runs.counts_json に記録する。"
    )
    for res in results:
        lines += [
            "",
            f"## {res.source_id}（{res.logical_key}）",
            "",
            f"原本：`{res.raw_path}`",
            "",
            "| ID | 検査 | 必須 | 結果 | 内容 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for check in CHECKS:
            item = res.results[check.qid]
            detail = str(item["detail"]).replace("|", "／").replace("\n", " ")
            lines.append(
                f"| {check.qid} | {check.title} | {'必須' if check.required else '—'} | "
                f"{STATE_LABELS[item['state']]} | {detail} |"
            )
    lines.append("")
    return "\n".join(lines)


def write_report(root: Path, run_id: str, markdown: str) -> Path:
    path = paths.run_dir(root, run_id) / REPORT_NAME
    paths.ensure_in_data(root, path.parent)
    path.write_text(markdown, encoding="utf-8")
    return path


FORMAT_CHECKS = frozenset({"Q01", "Q02", "Q03", "Q04"})


def failure_error(run_id: str, failures: list[tuple[str | None, str, str]]) -> KizashiError:
    """品質検査の工程の失敗を、型付きのエラーにする。

    Q01〜Q04（文字コード・見出し・列）の不合格は E_FORMAT_CHANGED、取得済みの CSV がないときは
    E_FETCH_FAILED、それ以外（必須の検査が実行できない、件数の食い違い）は E_INTERNAL。
    """
    if any(q in FORMAT_CHECKS and state == "fail" for q, state, _ in failures):
        code = "E_FORMAT_CHANGED"
    elif any(q is None for q, _, _ in failures):
        code = "E_FETCH_FAILED"
    else:
        code = "E_INTERNAL"
    return KizashiError(
        code,
        "quality",
        "必須の品質検査が実行できない、または不合格：" + "／".join(t for _, _, t in failures),
        affected_ids=[run_id],
        next_action=f"data/runs/{run_id}/{REPORT_NAME} を確かめ、原因を直してから新しい実行を作る。",
    )
