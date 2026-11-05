"""検出と自動の基礎集計（implementation_spec.md 2章の detect.py、4.8。アダプタ仕様9章。設計書5.2）。

- `datasets/<分野>/screens/*.sql`（名前の順）を、名前付きのパラメーター :year_from・:year_to
  （run_config の compare_years）で実行する。screens/ の .sql だけが検出SQL。比べた組の数（compared）と
  比較できなかった組の数（incomparable）は、同じ名前の `populations/<名前>.sql` で数える。
- 実行の前に、dataset.yaml の `screens.<名前>.requires` の資料（方式）が compare_years の両年度で取得・
  品質検査を通っているかを確かめる。1つでも欠ければ、検出SQLも母集団のSQLも実行せず、
  `{"state": "not_run", "missing": ["kyoso:2025", ...]}` を返す（compared: 0 とは書かない）。
- 候補を (screen_id, lead_key, change_category, period_from, period_to) で一意に登録する。
- 候補ごとに、row_refs_* から excluded_* を除いた行で基礎集計を Decimal で計算し直して登録する
  （origin='run'。行の選び方と合計は calc.py）。件数は必ず登録する（metric が `<列>_sum` の検出SQLでは
  「合計対象行の件数」）。metric が `<列>_sum` で両年度に合計できる行があれば合計も登録し、calc_from・
  calc_to を合計にして、差額と増減率（基準が0以下なら作らない）を派生計算にする。片方の年度に合計できる
  行がなければ（amount_incomparable）件数だけを登録し、calc_from・calc_to を件数にする。
  除いた行と理由と件数は、基礎集計の request_json に残す。どの行を除くかは分野のSQLだけが決める。
- 比較できない理由：検出SQLの理由（null を除く）、候補の行の分野の表の理由（重複、読めない日付など）、
  dataset.yaml の年度途中（partial_year）を合わせて並べる。
- 比較の条件は dataset.yaml の comparison_defaults。母集団の絞り込みがあれば、母集団に指示と条件を書き足す
  （実装仕様6.3。scope_population。探索の候補の登録と結論も同じ関数を使う）。
- 同じ実行の、同じ change_category の検出の候補の数から自分を除いた数を peer_count に入れる（探索の候補は
  数えない。実装仕様4.3「同じ区分のほかの件数」）。
- 年度の系列：同じ名前の `series/<名前>.sql`（:year の年度の、まとめごとの行と合計から除く行）を
  series_years の年度ごとに実行し、比べる2年度と同じ数え方で基礎集計を登録する。比べる2年度では、系列のSQL
  の行と除く行が検出SQLと同じかを照らし（違えば E_INTERNAL）、calc_from・calc_to を系列に使う。ほかの
  年度も件数を必ず登録し、合計の候補で合計できる行があれば合計を系列に入れる。合計できる行がない年度と、
  資料のそろわない年度は系列に入れず（0にしない）、候補の note に書く。

呼び出し側（runner）がトランザクションを持つ。分野のSQL・dataset.yaml の誤りは E_INTERNAL（検出を確定
しない）。
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
SERIES_COLUMNS = ("lead_key", "fiscal_year", "row_refs", "excluded")
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
    series: Path
    requires: tuple[str, ...]
    comparison: dict


@dataclass(frozen=True)
class _Side:
    """1つの年度の、候補に属する行すべてと、合計から除く行（理由付き。ref の順）。"""

    refs: tuple[str, ...]
    excluded: tuple[dict, ...]

    @property
    def used(self) -> list[str]:
        """合計の対象の行（refs から excluded を除いたもの。順に並べる）。"""
        dropped = {e["ref"] for e in self.excluded}
        return sorted(r for r in self.refs if r not in dropped)

    def same_rows(self, other: _Side) -> bool:
        return sorted(self.refs) == sorted(other.refs) and self.excluded == other.excluded


NO_ROWS = _Side(refs=(), excluded=())


@dataclass(frozen=True)
class _Row:
    lead_key: str
    category: str
    metric: str
    sides: tuple[_Side, _Side]
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
        series = adapter.directory / "series" / f"{name}.sql"
        if not series.is_file():
            raise _internal(f"系列のSQL series/{name}.sql がない。")
        screens.append(
            _Screen(
                name=name,
                path=path,
                population=population,
                series=series,
                requires=tuple(requires),
                comparison=_comparison(adapter, name, scope),
            )
        )
    return screens


def scope_population(population: str, scope: dict | None) -> str:
    """比較の条件の母集団に、母集団の絞り込み（記者の指示と条件）を書き足す（実装仕様6.3）。

    検出の候補、探索の候補の登録、結論での比較の条件の変更で同じものを使う。すでに同じ書き足しがあれば
    そのまま返す（結論で変えた母集団にも、実行時の絞り込みを残す）。"""
    if scope is None:
        return population
    conditions = "、".join(
        f"{f['field']} が「{f['value']}」{SCOPE_OPS_JA[f['op']]}" for f in scope["filters"]
    )
    note = f"（記者の指示で絞り込み：{scope['instruction']}。条件：{conditions}）"
    return population if note in population else population + note


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
    comparison["population"] = scope_population(comparison["population"], scope)
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


def _side(
    name: str, refs_raw: object, ex_raw: object, ref_col: str, ex_col: str, key: str
) -> _Side:
    """行の一覧と、合計から除く行（理由付き）の検査。除く行は行の一覧の中になければならない。"""
    refs = _json_list(refs_raw, ref_col, name, key)
    if not all(isinstance(r, str) and r for r in refs) or len(set(refs)) != len(refs):
        raise _internal(f"{name} の {ref_col} の行に、文字列でないものか重複がある。")
    excluded = []
    for item in _json_list(ex_raw, ex_col, name, key):
        if not (
            isinstance(item, dict)
            and isinstance(item.get("ref"), str)
            and isinstance(item.get("reason"), str)
            and item["reason"].strip()
        ):
            raise _internal(f"{name} の {ex_col} の要素に、ref か空でない reason がない。")
        if item["ref"] not in refs:
            raise _internal(
                f"{name} の {ex_col} の行 {item['ref']} が {ref_col} にない（lead_key {key!r}）。"
            )
        excluded.append({"ref": item["ref"], "reason": item["reason"]})
    if len({e["ref"] for e in excluded}) != len(excluded):
        raise _internal(f"{name} の {ex_col} に同じ行が2回ある（lead_key {key!r}）。")
    return _Side(refs=tuple(refs), excluded=tuple(sorted(excluded, key=lambda e: e["ref"])))


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
    sides = (
        _side(name, raw[5], raw[7], "row_refs_from", "excluded_from", lead_key),
        _side(name, raw[6], raw[8], "row_refs_to", "excluded_to", lead_key),
    )
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
        sides=sides,
        reasons=tuple(reasons),
    )


def _series_rows(conn: sqlite3.Connection, screen: _Screen, year: int) -> dict[str, _Side]:
    """系列のSQL（series/<名前>.sql）の、:year の年度のまとめごとの行と除く行。"""
    where = f"系列のSQL series/{screen.name}.sql"
    cur = execute_sql(conn, screen.series, {"year": year})
    columns = tuple(d[0] for d in cur.description or ())
    if columns != SERIES_COLUMNS:
        raise _internal(f"{where} の列が {'・'.join(SERIES_COLUMNS)} でない：" + "・".join(columns))
    found: dict[str, _Side] = {}
    for lead_key, fiscal_year, refs, excluded in cur.fetchall():
        if not (isinstance(lead_key, str) and lead_key):
            raise _internal(f"{where} の lead_key が空でない文字列でない。")
        if fiscal_year != year:
            raise _internal(f"{where} の fiscal_year が {year} でない（lead_key {lead_key!r}）。")
        if lead_key in found:
            raise _internal(f"{where} が同じ lead_key {lead_key!r} を2回返した（{year}年度）。")
        found[lead_key] = _side(where, refs, excluded, "row_refs", "excluded", lead_key)
    return found


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
        column: str | None,
    ) -> None:
        self.conn = conn
        self.adapter = adapter
        self.screen = screen
        self.row = row
        self.lead_id = lead_id
        self.partial = partial
        self.column = column  # 合計する列（metric が `<列>_sum` の検出SQL）。なければ None
        self.table = adapter.module.RECORD_TABLE
        self.where = f"{screen.name}・{row.lead_key}"

    def base(self, year: int, side: _Side, op: str, *, series: bool) -> str:
        """年度の基礎集計。行は side の refs から excluded を除いたもの（どの行を除くかは分野のSQLが決める）。"""
        used = side.used
        request: dict = {
            "op": op,
            "table": self.table,
            "filter": [
                {"col": "fiscal_year", "op": "=", "value": year},
                {"col": "record_id", "op": "in", "value": used},
            ],
        }
        where = f"系列。{self.where}" if series else self.where
        if op == "sum":
            assert self.column is not None
            request["column"] = self.column
            label, unit = self.adapter.module.SUM_LABELS[self.column]
            meaning = f"{year}年度の{label}の合計（{where}。合計から除いた行を除く）"
        elif self.column is not None:
            # 金額の検出の件数は、合計の対象の行の数（金額の合計ではない。D-038②）
            unit = COUNT_UNIT
            meaning = f"{year}年度の合計対象行の件数（{where}。合計から除いた行を除く）"
        else:
            unit = COUNT_UNIT
            sql = "系列のSQL" if series else "検出SQL"
            dropped = f"。{sql}が除いた行を除く" if side.excluded else ""
            meaning = f"{year}年度の件数（{where}{dropped}）"
        if year in self.partial:
            meaning += "（年度途中）"
        extra = {
            "screen_id": self.screen.name,
            "rows_sql": f"{'series' if series else 'screens'}/{self.screen.name}.sql",
            "series": series,
            "partial_year": year in self.partial,
            "excluded": list(side.excluded),
            "excluded_count": len(side.excluded),
            "excluded_by_reason": dict(sorted(Counter(e["reason"] for e in side.excluded).items())),
        }
        return calc.register_base(
            self.conn,
            self.adapter,
            request,
            meaning=meaning,
            unit=unit,
            lead_id=self.lead_id,
            origin="run",
            extra=extra,
            allow_empty_in=True,
            expect_rows=len(used),
        )

    def derived(self, op: str, args: list[str], years: Sequence[int]) -> str:
        assert self.column is not None
        label, unit = self.adapter.module.SUM_LABELS[self.column]
        if op == "diff":
            meaning = f"{label}の合計の差（{years[1]}年度−{years[0]}年度。{self.where}）"
        else:
            meaning = f"{label}の合計の増減率（{years[0]}年度を基準。{self.where}）"
            unit = RATE_UNIT
        return calc.register_derived(
            self.conn, op, args, meaning=meaning, unit=unit, lead_id=self.lead_id, origin="run"
        )


@dataclass
class _SeriesData:
    """検出SQL1本ぶんの系列：年度ごとの系列のSQLの出力と、資料がそろわず実行しなかった年度の欠けた資料。"""

    rows: dict[int, dict[str, _Side]] = field(default_factory=dict)
    lacking: dict[int, list[str]] = field(default_factory=dict)


def _register(
    conn: sqlite3.Connection,
    adapter: Adapter,
    screen: _Screen,
    row: _Row,
    config: dict,
    series_data: _SeriesData,
) -> str:
    years = list(config["compare_years"])
    series_years = sorted(config["series_years"])
    partial = _partial_years(adapter, sorted({*years, *series_years}))
    all_refs = [r for side in row.sides for r in side.refs]
    reasons = set(row.reasons) | _row_reasons(conn, adapter, all_refs)
    if partial & set(years):
        reasons.add(PARTIAL_YEAR)
    for year, side in zip(years, row.sides, strict=True):
        if not series_data.rows[year].get(row.lead_key, NO_ROWS).same_rows(side):
            raise _internal(
                f"系列のSQL series/{screen.name}.sql の {year}年度の行か合計から除く行が、検出SQLと違う"
                f"（lead_key {row.lead_key!r}。比べる年度と系列の年度は同じ数え方にする）。"
            )
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

    column = _sum_column(adapter, screen.name, row.metric)
    calcs = _LeadCalcs(conn, adapter, screen, row, lead_id, partial, column)
    counts = [
        calcs.base(y, side, "count", series=False) for y, side in zip(years, row.sides, strict=True)
    ]
    summable = column is not None and all(side.used for side in row.sides)
    if summable:
        calc_from, calc_to = (
            calcs.base(y, side, "sum", series=False)
            for y, side in zip(years, row.sides, strict=True)
        )
        calcs.derived("diff", [calc_to, calc_from], years)
        base_value = conn.execute(
            "SELECT value FROM computations WHERE calc_id = ?", (calc_from,)
        ).fetchone()[0]
        if Decimal(base_value) > 0:  # 基準値が0以下なら増減率を作らない（設計書5.2）
            calcs.derived("rate", [calc_to, calc_from], years)
    else:
        calc_from, calc_to = counts

    series: list[str] = []
    lacking: list[str] = []
    no_sum: list[str] = []
    for year in series_years:
        if year == years[0]:
            series.append(calc_from)
        elif year == years[1]:
            series.append(calc_to)
        elif year in series_data.lacking:
            lacking.append(f"{year}年度（{'・'.join(series_data.lacking[year])}）")
        else:
            side = series_data.rows[year].get(row.lead_key, NO_ROWS)
            count = calcs.base(year, side, "count", series=True)
            if not summable:
                series.append(count)
            elif side.used:
                series.append(calcs.base(year, side, "sum", series=True))
            else:
                no_sum.append(f"{year}年度（合計対象行の件数 {count}）")
    notes = []
    if lacking:
        notes.append(
            "系列：資料がそろわないため計算していない年度がある（0件・0円とは扱わない）："
            + "、".join(lacking)
        )
    if no_sum:
        label = adapter.module.SUM_LABELS[column][0] if column else ""
        notes.append(
            f"系列：合計できる行がないため{label}の合計を計算していない年度がある（0円とは扱わない）："
            + "、".join(no_sum)
        )
    conn.execute(
        "UPDATE leads SET calc_from = ?, calc_to = ?, calc_ids_json = ?, note = ? WHERE lead_id = ?",
        (calc_from, calc_to, canonical_json(series), "。".join(notes) or None, lead_id),
    )
    return lead_id


def _series_data(
    conn: sqlite3.Connection,
    adapter: Adapter,
    screen: _Screen,
    config: dict,
    checked_sources: set[str],
) -> _SeriesData:
    """系列の年度ごとに系列のSQLを実行する。比べる2年度は検出SQLと照らすため必ず実行する（資料は検出SQLの
    実行の前に確かめてある）。ほかの年度は、資料（requires の方式）がそろわなければ実行しない。"""
    years = list(config["compare_years"])
    data = _SeriesData()
    for year in sorted({*years, *config["series_years"]}):
        missing = (
            []
            if year in years
            else missing_sources(conn, adapter, screen.requires, [year], checked_sources)
        )
        if missing:
            data.lacking[year] = missing
        else:
            data.rows[year] = _series_rows(conn, screen, year)
    return data


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
        if rows:
            series_data = _series_data(conn, adapter, screen, config, checked_sources)
            for row in rows:
                lead_id = _register(conn, adapter, screen, row, config, series_data)
                registered.append((lead_id, row.category))
        outcome.counts[screen.name] = {
            "state": "done",
            "compared": compared,
            "incomparable": incomparable,
            "leads": len(rows),
        }
    # 同じ実行の、同じ区分の検出の候補の数から自分を除いた数（探索の候補はここでは作らず、数えない）
    per_category = Counter(category for _, category in registered)
    conn.executemany(
        "UPDATE leads SET peer_count = ? WHERE lead_id = ?",
        [(per_category[category] - 1, lead_id) for lead_id, category in registered],
    )
    outcome.leads = len(registered)
    return outcome
