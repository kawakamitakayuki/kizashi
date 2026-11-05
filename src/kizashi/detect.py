"""検出と自動の基礎集計（implementation_spec.md 2章の detect.py、4.8。アダプタ仕様9章。設計書5.2）。

- `datasets/<分野>/screens/*.sql`（名前の順）を、名前付きのパラメーター :year_from・:year_to
  （run_config の compare_years）で実行する。screens/ の .sql だけが検出SQL。比べた組の数（compared）と
  比較できなかった組の数（incomparable）は、同じ名前の `populations/<名前>.sql` で数える。
- 実行の前に、dataset.yaml の `screens.<名前>.requires` の資料（方式）が compare_years の両年度で取得・
  品質検査を通っているかを確かめる。1つでも欠ければ、検出SQLも母集団のSQLも実行せず、
  `{"state": "not_run", "missing": ["kyoso:2025", ...]}` を返す（compared: 0 とは書かない）。
- 候補を (screen_id, lead_key, change_category, period_from, period_to) で一意に登録する。
- 候補ごとに、row_refs_* から excluded_* を除いた行で基礎集計を Decimal で計算し直して登録する
  （origin='run'。行の選び方と合計は calc.py）。件数は必ず登録する。metric が `<列>_sum` なら合計も登録し、
  calc_from・calc_to を合計にして、差額と増減率（基準が0以下なら作らない）を派生計算にする。片方の年度に
  合計できる行がなければ（amount_incomparable）件数だけを登録し、calc_from・calc_to を件数にする。
  除いた行と理由と件数は、基礎集計の request_json に残す。どの行を除くかは検出SQLだけが決める。
- 比較できない理由：検出SQLの理由（null を除く）、候補の行の分野の表の理由（重複、読めない日付など）、
  dataset.yaml の年度途中（partial_year）を合わせて並べる。
- 比較の条件は dataset.yaml の comparison_defaults。母集団の絞り込みがあれば、母集団に指示と条件を書き足す
  （実装仕様6.3）。
- 同じ change_category のほかの候補の数を peer_count に入れる（実装仕様4.3「同じ区分のほかの件数」）。
- series_years の各年度の基礎集計を系列として calc_ids_json に入れる。比べる2年度は calc_from・calc_to と
  同じ計算。ほかの年度は分野アダプタの series_filter の条件で選ぶ。資料のそろわない年度は計算を作らず
  （0にしない）、候補の note に書く。

呼び出し側（runner）がトランザクションを持つ。検出SQL・dataset.yaml の誤りは E_INTERNAL（検出を確定しない）。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from kizashi import calc, ledger
from kizashi.adapters import FAMILIES, Adapter
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json

STAGE = "detect"
SCREEN_COLUMNS = (
    "lead_key",
    "change_category",
    "metric",
    "period_from",
    "period_to",
    "row_refs_from",
    "row_refs_to",
    "excluded_from",
    "excluded_to",
    "incomparable_reasons",
)
POPULATION_COLUMNS = ("compared", "incomparable")
COMPARISON_KEYS = ("population", "row_meaning", "period", "unit", "entity_mapping", "formula")
PARTIAL_YEAR = "partial_year"
COUNT_UNIT = "件"
RATE_UNIT = "比率"
SCOPE_OPS_JA = {
    "contains": "を含む",
    "not_contains": "を含まない",
    "equals": "と等しい",
    "not_equals": "と等しくない",
}
_CHUNK = 500


def _internal(message: str) -> KizashiError:
    return KizashiError(
        "E_INTERNAL",
        STAGE,
        message,
        next_action="調査のセッションでは直さず、記者に伝える。開発側で検出SQLか dataset.yaml を直す。",
    )


@dataclass
class DetectOutcome:
    """検出の結果。counts は runs.counts_json に検出SQLの名前で入れる。"""

    counts: dict[str, dict] = field(default_factory=dict)
    not_run: list[dict] = field(default_factory=list)
    leads: int = 0


@dataclass(frozen=True)
class _Screen:
    name: str
    path: Path
    population: Path
    requires: tuple[str, ...]
    comparison: dict


@dataclass(frozen=True)
class _Row:
    lead_key: str
    category: str
    metric: str
    refs: tuple[list[str], list[str]]
    excluded: tuple[list[dict], list[dict]]
    reasons: tuple[str, ...]


def execute_sql(conn: sqlite3.Connection, path: Path, params: dict) -> sqlite3.Cursor:
    """検出SQL・母集団のSQLを1つ実行する（テストでは、実行したファイルを数えるために差し替える）。"""
    return conn.execute(path.read_text(encoding="utf-8"), params)


def screen_paths(adapter: Adapter) -> list[Path]:
    """screens/ の .sql（名前の順）。"""
    return sorted((adapter.directory / "screens").glob("*.sql"), key=lambda p: p.name)


# ---------------------------------------------------------------------------
# 設定（dataset.yaml の screens・comparison_defaults）
# ---------------------------------------------------------------------------


def _screens(adapter: Adapter, scope: dict | None) -> list[_Screen]:
    paths = screen_paths(adapter)
    declared = adapter.config.get("screens") or {}
    names = [p.stem for p in paths]
    if set(names) != set(declared):
        raise _internal(
            "screens/ の検出SQLと dataset.yaml の screens が合わない："
            f"SQL だけにある {sorted(set(names) - set(declared))}、"
            f"dataset.yaml だけにある {sorted(set(declared) - set(names))}"
        )
    screens = []
    for path in paths:
        name = path.stem
        requires = (declared.get(name) or {}).get("requires")
        if not (isinstance(requires, list) and requires and all(f in FAMILIES for f in requires)):
            raise _internal(
                f"dataset.yaml の screens.{name}.requires が {'・'.join(FAMILIES)} の配列でない。"
            )
        population = adapter.directory / "populations" / f"{name}.sql"
        if not population.is_file():
            raise _internal(f"母集団のSQL populations/{name}.sql がない。")
        screens.append(
            _Screen(
                name=name,
                path=path,
                population=population,
                requires=tuple(requires),
                comparison=_comparison(adapter, name, scope),
            )
        )
    return screens


def _comparison(adapter: Adapter, name: str, scope: dict | None) -> dict:
    defaults = (adapter.config.get("comparison_defaults") or {}).get(name)
    if not (
        isinstance(defaults, dict)
        and set(defaults) == set(COMPARISON_KEYS)
        and all(isinstance(defaults[k], str) for k in COMPARISON_KEYS)
    ):
        raise _internal(
            f"dataset.yaml の comparison_defaults.{name} が {'・'.join(COMPARISON_KEYS)} の6項目でない。"
        )
    comparison = dict(defaults)
    if scope is not None:
        conditions = "、".join(
            f"{f['field']} が「{f['value']}」{SCOPE_OPS_JA[f['op']]}" for f in scope["filters"]
        )
        comparison["population"] += (
            f"（記者の指示で絞り込み：{scope['instruction']}。条件：{conditions}）"
        )
    return comparison


def missing_sources(
    conn: sqlite3.Connection,
    adapter: Adapter,
    families: Sequence[str],
    years: Sequence[int],
    checked_sources: set[str],
) -> list[str]:
    """取得と品質検査を通っていない資料（`<方式>:<年度>`）。"""
    missing = []
    for year in years:
        for family in families:
            rows = conn.execute(
                "SELECT source_id, status FROM sources WHERE kind = 'dataset' AND logical_key = ?",
                (adapter.logical_key(year, family, "csv"),),
            ).fetchall()
            if not any(r[1] == "fetched" and r[0] in checked_sources for r in rows):
                missing.append(f"{family}:{year}")
    return missing


def _partial_years(adapter: Adapter, years: Sequence[int]) -> set[int]:
    return {
        y
        for y in years
        if any((s := adapter.file_spec(y, f)) is not None and s.partial_year for f in FAMILIES)
    }


# ---------------------------------------------------------------------------
# 検出SQLの出力の検査
# ---------------------------------------------------------------------------


def _json_list(value: object, column: str, name: str, key: str) -> list:
    try:
        data = json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, list):
        raise _internal(f"{name} の {column} が JSON の配列でない（lead_key {key!r}）。")
    return data


def _parse(name: str, raw: Sequence, years: Sequence[int], categories: Sequence[str]) -> _Row:
    lead_key, category, metric, period_from, period_to = raw[:5]
    if not (isinstance(lead_key, str) and lead_key):
        raise _internal(f"{name} の lead_key が空でない文字列でない。")
    if category not in categories:
        raise _internal(
            f"{name} の change_category {category!r} が dataset.yaml の reconcile.fields_by_category にない。"
        )
    if not (isinstance(metric, str) and metric):
        raise _internal(f"{name} の metric が空でない文字列でない（lead_key {lead_key!r}）。")
    if [period_from, period_to] != list(years):
        raise _internal(
            f"{name} の period_from・period_to が compare_years と違う（lead_key {lead_key!r}）。"
        )
    refs: list[list[str]] = []
    excluded: list[list[dict]] = []
    for side, (ref_col, ex_col) in enumerate(
        (("row_refs_from", "excluded_from"), ("row_refs_to", "excluded_to"))
    ):
        side_refs = _json_list(raw[5 + side], ref_col, name, lead_key)
        if not all(isinstance(r, str) and r for r in side_refs) or len(set(side_refs)) != len(
            side_refs
        ):
            raise _internal(f"{name} の {ref_col} の行に、文字列でないものか重複がある。")
        side_excluded = []
        for item in _json_list(raw[7 + side], ex_col, name, lead_key):
            if not (
                isinstance(item, dict)
                and isinstance(item.get("ref"), str)
                and isinstance(item.get("reason"), str)
                and item["reason"].strip()
            ):
                raise _internal(f"{name} の {ex_col} の要素に、ref か空でない reason がない。")
            if item["ref"] not in side_refs:
                raise _internal(
                    f"{name} の {ex_col} の行 {item['ref']} が {ref_col} にない（lead_key {lead_key!r}）。"
                )
            side_excluded.append({"ref": item["ref"], "reason": item["reason"]})
        if len({e["ref"] for e in side_excluded}) != len(side_excluded):
            raise _internal(f"{name} の {ex_col} に同じ行が2回ある（lead_key {lead_key!r}）。")
        refs.append(side_refs)
        excluded.append(sorted(side_excluded, key=lambda e: e["ref"]))
    reasons = []
    for reason in _json_list(raw[9], "incomparable_reasons", name, lead_key):
        if reason is None:
            continue  # json_array の null は CLI が除く（アダプタ仕様9章）
        if not (isinstance(reason, str) and reason):
            raise _internal(f"{name} の incomparable_reasons に空でない文字列でない値がある。")
        reasons.append(reason)
    return _Row(
        lead_key=lead_key,
        category=category,
        metric=metric,
        refs=(refs[0], refs[1]),
        excluded=(excluded[0], excluded[1]),
        reasons=tuple(reasons),
    )


def _population(conn: sqlite3.Connection, screen: _Screen, params: dict) -> tuple[int, int]:
    cur = execute_sql(conn, screen.population, params)
    columns = tuple(d[0] for d in cur.description or ())
    rows = cur.fetchall()
    ok = (
        columns == POPULATION_COLUMNS
        and len(rows) == 1
        and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in rows[0])
    )
    if not ok:
        raise _internal(
            f"母集団のSQL populations/{screen.name}.sql が compared・incomparable（0以上の整数）の1行を返さない。"
        )
    return int(rows[0][0]), int(rows[0][1])


# ---------------------------------------------------------------------------
# 候補と自動の基礎集計
# ---------------------------------------------------------------------------


def _sum_column(adapter: Adapter, name: str, metric: str) -> str | None:
    """metric が `<列>_sum` なら、合計する列。"""
    if not metric.endswith("_sum"):
        return None
    column = metric[: -len("_sum")]
    if column not in getattr(adapter.module, "SUM_LABELS", {}):
        raise _internal(f"{name} の metric {metric!r} の列が、分野アダプタの SUM_LABELS にない。")
    return column


def _row_reasons(conn: sqlite3.Connection, adapter: Adapter, refs: Sequence[str]) -> set[str]:
    """候補の行の、分野の表の比較できない理由（重複、読めない日付など）。"""
    table = adapter.module.RECORD_TABLE  # 分野アダプタの定数（利用者の入力を埋め込まない）
    found: set[str] = set()
    refs = list(refs)
    for start in range(0, len(refs), _CHUNK):
        chunk = refs[start : start + _CHUNK]
        marks = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT incomparable_reasons_json FROM {table} WHERE record_id IN ({marks})", chunk
        ):
            found.update(json.loads(row[0] or "[]"))
    return found


class _LeadCalcs:
    """1つの候補の自動の基礎集計と派生計算を登録する。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        adapter: Adapter,
        screen: _Screen,
        row: _Row,
        lead_id: str,
        partial: set[int],
    ) -> None:
        self.conn = conn
        self.adapter = adapter
        self.screen = screen
        self.row = row
        self.lead_id = lead_id
        self.partial = partial
        self.table = adapter.module.RECORD_TABLE
        self.where = f"{screen.name}・{row.lead_key}"

    def _mark(self, year: int) -> str:
        return "（年度途中）" if year in self.partial else ""

    def side(self, year: int, side: int, op: str, column: str | None) -> str:
        excluded = self.row.excluded[side]
        dropped = {e["ref"] for e in excluded}
        used = sorted(r for r in self.row.refs[side] if r not in dropped)
        request: dict = {
            "op": op,
            "table": self.table,
            "filter": [
                {"col": "fiscal_year", "op": "=", "value": year},
                {"col": "record_id", "op": "in", "value": used},
            ],
        }
        if op == "sum":
            request["column"] = column
            label, unit = self.adapter.module.SUM_LABELS[column]
            meaning = f"{year}年度の{label}の合計（{self.where}。検出SQLが除いた行を除く）"
        else:
            unit = COUNT_UNIT
            meaning = f"{year}年度の件数（{self.where}。検出SQLが除いた行を除く）"
        extra = {
            "screen_id": self.screen.name,
            "excluded": excluded,
            "excluded_count": len(excluded),
            "excluded_by_reason": dict(sorted(Counter(e["reason"] for e in excluded).items())),
        }
        return calc.register_base(
            self.conn,
            self.adapter,
            request,
            meaning=meaning + self._mark(year),
            unit=unit,
            lead_id=self.lead_id,
            origin="run",
            extra=extra,
            allow_empty_in=True,
            expect_rows=len(used),
        )

    def derived(self, op: str, args: list[str], years: Sequence[int], column: str) -> str:
        label, unit = self.adapter.module.SUM_LABELS[column]
        if op == "diff":
            meaning = f"{label}の合計の差（{years[1]}年度−{years[0]}年度。{self.where}）"
        else:
            meaning = f"{label}の合計の増減率（{years[0]}年度を基準。{self.where}）"
            unit = RATE_UNIT
        return calc.register_derived(
            self.conn, op, args, meaning=meaning, unit=unit, lead_id=self.lead_id, origin="run"
        )

    def series(self, year: int, op: str, column: str | None) -> str:
        filters = self.adapter.module.series_filter(self.screen.name, self.row.lead_key)
        if filters is None:
            raise _internal(
                f"分野アダプタに {self.screen.name} の系列の条件（SERIES_FILTERS）がない。"
            )
        request: dict = {
            "op": op,
            "table": self.table,
            "filter": [*filters, {"col": "fiscal_year", "op": "=", "value": year}],
        }
        if op == "sum":
            request["column"] = column
            label, unit = self.adapter.module.SUM_LABELS[column]
            meaning = f"{year}年度の{label}の合計（系列。{self.where}）"
        else:
            unit = COUNT_UNIT
            meaning = f"{year}年度の件数（系列。{self.where}）"
        return calc.register_base(
            self.conn,
            self.adapter,
            request,
            meaning=meaning + self._mark(year),
            unit=unit,
            lead_id=self.lead_id,
            origin="run",
            extra={
                "screen_id": self.screen.name,
                "series": True,
                "partial_year": year in self.partial,
            },
        )


def _register(
    conn: sqlite3.Connection,
    adapter: Adapter,
    screen: _Screen,
    row: _Row,
    config: dict,
    checked_sources: set[str],
) -> str:
    years = list(config["compare_years"])
    series_years = sorted(config["series_years"])
    partial = _partial_years(adapter, sorted({*years, *series_years}))
    reasons = set(row.reasons) | _row_reasons(conn, adapter, [*row.refs[0], *row.refs[1]])
    if partial & set(years):
        reasons.add(PARTIAL_YEAR)
    lead_id = ledger.next_id(conn, "L")
    try:
        conn.execute(
            "INSERT INTO leads (lead_id, screen_id, lead_key, change_category, metric, period_from,"
            " period_to, comparison_json, incomparable_reasons_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lead_id,
                screen.name,
                row.lead_key,
                row.category,
                row.metric,
                str(years[0]),
                str(years[1]),
                canonical_json(screen.comparison),
                canonical_json(sorted(reasons)),
            ),
        )
    except sqlite3.IntegrityError:
        raise _internal(
            f"{screen.name} が同じ候補（lead_key {row.lead_key!r}、区分 {row.category}）を2回返した。"
        ) from None

    calcs = _LeadCalcs(conn, adapter, screen, row, lead_id, partial)
    column = _sum_column(adapter, screen.name, row.metric)
    counts = [calcs.side(years[i], i, "count", None) for i in (0, 1)]
    dropped = [{e["ref"] for e in row.excluded[i]} for i in (0, 1)]
    summable = column is not None and all(
        any(r not in dropped[i] for r in row.refs[i]) for i in (0, 1)
    )
    if summable:
        calc_from, calc_to = (calcs.side(years[i], i, "sum", column) for i in (0, 1))
        assert column is not None
        calcs.derived("diff", [calc_to, calc_from], years, column)
        base_value = conn.execute(
            "SELECT value FROM computations WHERE calc_id = ?", (calc_from,)
        ).fetchone()[0]
        if Decimal(base_value) > 0:  # 基準値が0以下なら増減率を作らない（設計書5.2）
            calcs.derived("rate", [calc_to, calc_from], years, column)
    else:
        calc_from, calc_to = counts

    op = "sum" if summable else "count"
    series: list[str] = []
    lacking: list[str] = []
    for year in series_years:
        if year == years[0]:
            series.append(calc_from)
        elif year == years[1]:
            series.append(calc_to)
        else:
            missing = missing_sources(conn, adapter, screen.requires, [year], checked_sources)
            if missing:
                lacking.append(f"{year}年度（{'・'.join(missing)}）")
            else:
                series.append(calcs.series(year, op, column if summable else None))
    note = (
        "系列：資料がそろわないため計算していない年度がある（0件・0円とは扱わない）："
        + "、".join(lacking)
        if lacking
        else None
    )
    conn.execute(
        "UPDATE leads SET calc_from = ?, calc_to = ?, calc_ids_json = ?, note = ? WHERE lead_id = ?",
        (calc_from, calc_to, canonical_json(series), note, lead_id),
    )
    return lead_id


def run_screens(
    conn: sqlite3.Connection,
    root: Path,
    adapter: Adapter,
    config: dict,
    *,
    checked_sources: set[str],
) -> DetectOutcome:
    """検出SQLを順に実行し、候補・自動の基礎集計・同じ区分の件数・年度の系列を登録する。"""
    years = list(config["compare_years"])
    params = {"year_from": years[0], "year_to": years[1]}
    categories = tuple((adapter.config.get("reconcile") or {}).get("fields_by_category") or {})
    outcome = DetectOutcome()
    registered: list[tuple[str, str]] = []
    for screen in _screens(adapter, config.get("scope")):
        missing = missing_sources(conn, adapter, screen.requires, years, checked_sources)
        if missing:
            outcome.counts[screen.name] = {"state": "not_run", "missing": missing}
            outcome.not_run.append({"screen": screen.name, "missing": missing})
            continue
        cur = execute_sql(conn, screen.path, params)
        columns = tuple(d[0] for d in cur.description or ())
        if columns != SCREEN_COLUMNS:
            raise _internal(
                f"検出SQL screens/{screen.name}.sql の列が {'・'.join(SCREEN_COLUMNS)} でない："
                + "・".join(columns)
            )
        rows = sorted(
            (_parse(screen.name, r, years, categories) for r in cur.fetchall()),
            key=lambda r: (r.category, r.lead_key),
        )
        compared, incomparable = _population(conn, screen, params)
        for row in rows:
            lead_id = _register(conn, adapter, screen, row, config, checked_sources)
            registered.append((lead_id, row.category))
        outcome.counts[screen.name] = {
            "state": "done",
            "compared": compared,
            "incomparable": incomparable,
            "leads": len(rows),
        }
    per_category = Counter(category for _, category in registered)
    conn.executemany(
        "UPDATE leads SET peer_count = ? WHERE lead_id = ?",
        [(per_category[category] - 1, lead_id) for lead_id, category in registered],
    )
    outcome.leads = len(registered)
    return outcome
