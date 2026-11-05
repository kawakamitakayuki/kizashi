"""内閣府の公共調達の正規化と分野の表への書き込み（アダプタ仕様4章・4.8a・8章）。

- 担当官のセル（officer_raw）から氏名を除き、所属だけを officer_units_json に入れる（4.3）。
  氏名はどこにも保存しない。records.cells_json にも担当官のセルを入れない（実装仕様5章）。
- 取り込まない列（drop）は records.cells_json に入れない。
- 見出しの文字列と列の数を、行を書く前に確かめる。違えば1行も書かずに E_FORMAT_CHANGED（D-027）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

from kizashi import normalize as nz
from kizashi import paths, readers
from kizashi.adapters import Adapter, FileSpec
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json, sha256_text
from kizashi.ledger import record_id

# 担当官のセルに入らない列のキー（records.cells_json に入れない。実装仕様5章）。
OFFICER_KEY = "officer_raw"
OTHER_ENCODINGS = ("cp932", "utf-8-sig")
MAX_LISTED = 10

# 取り込みの照合（T6a）で、別経路の読み込み処理の値と比べる元の値（アダプタ仕様6章）。方式は競争入札の
# method_raw、随意契約の reason_raw（その年度・方式の take にあるものだけを比べる）。整数にした金額
# （amount_yen）も比べる（reconcile.py）。担当官のセルは読まない。
IMPORT_FIELDS = (
    "no",
    "title_raw",
    "contract_date_raw",
    "counterparty_raw",
    "corp_number_raw",
    "method_raw",
    "reason_raw",
    "amount_raw",
)

# 基礎集計に使える表と列（アダプタ仕様8章の許可リスト）。合計は amount_yen だけ。条件に使える列。
# record_status = 'adopted' は calc.py が必ず加える（実装仕様7.1）ので、条件には書かせない。
CALC_TABLES: dict[str, dict[str, tuple[str, ...]]] = {
    "procurement_contracts": {
        "sum": ("amount_yen",),
        "filter": (
            "fiscal_year",
            "method_family",
            "group_key",
            "unit_price",
            "reason_category",
            "corp_number_state",
            "dup_group",
            "record_id",
        ),
    },
}

# ---------------------------------------------------------------------------
# 4.3 契約担当官のセル
# ---------------------------------------------------------------------------

ROLE_RE = re.compile(r"(分任)?支出負担行為担当官(代理)?")
_WORDS_RE = re.compile(r"[\s　]+")


def split_officers(cell: str | None) -> tuple[list[str | None], int]:
    """担当官のセルを分け、(所属の配列, 担当官の数) を返す。氏名は返さない（4.3）。

    1. `(分任)?支出負担行為担当官(代理)?` が現れるたびに、担当官の区切りとする。
    2. 役職の後の文字列を、空白（全角を含む）と改行で語に分ける。
    3. 最後の2語を氏名とみなして捨て、残りを所属とする（語が2つなら最後の1語を捨てる）。
       1語以下なら分けられないとして、その担当官の所属を None にする（品質検査 Q05）。
    役職が1つもないセルと、最初の役職より前に文字があるセルも、分けられない部分を None で示す。
    所属の語は半角の空白1つでつなぐ。
    """
    text = cell or ""
    matches = list(ROLE_RE.finditer(text))
    if not matches:
        return [None], 0
    units: list[str | None] = []
    if text[: matches[0].start()].strip():
        units.append(None)
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        words = [w for w in _WORDS_RE.split(text[match.end() : end]) if w]
        if len(words) >= 3:
            units.append(" ".join(words[:-2]))
        elif len(words) == 2:
            units.append(words[0])
        else:
            units.append(None)
    return units, len(matches)


def officer_names(cell: str | None) -> list[tuple[str, ...]]:
    """担当官のセルから、4.3 の規則で捨てる氏名の語を返す（V7 と公開の検査の氏名の一覧。保存しない）。

    役職の後の語が3つ以上なら最後の2語（姓, 名）、2つなら最後の1語（分けられない氏名）。
    """
    text = cell or ""
    matches = list(ROLE_RE.finditer(text))
    names: list[tuple[str, ...]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        words = [w for w in _WORDS_RE.split(text[match.end() : end]) if w]
        if len(words) >= 3:
            names.append((words[-2], words[-1]))
        elif len(words) == 2:
            names.append((words[-1],))
    return names


def name_list(conn: sqlite3.Connection, root: Path, adapter: Adapter) -> list[tuple[str, ...]]:
    """実行の取得済みの CSV の原本（担当官の列）から、氏名の一覧を作る。検査のたびに作り、保存しない
    （実装仕様9章の V7_PERSONAL_NAME）。"""
    rows = conn.execute(
        "SELECT logical_key, sha256 FROM sources WHERE kind = 'dataset' AND status = 'fetched'"
        " AND logical_key LIKE '%|csv' ORDER BY source_id"
    ).fetchall()
    names: set[tuple[str, ...]] = set()
    for logical_key, sha in rows:
        _, year, family, _ = logical_key.split("|")
        spec = adapter.file_spec(int(year), family)
        index = None if spec is None else spec.index_of(OFFICER_KEY)
        if spec is None or index is None:
            continue
        table = readers.read_rows(paths.raw_dir(root) / f"{sha}.csv", spec.encoding)
        for _, cells in table.records:
            if index < len(cells):
                names.update(officer_names(cells[index]))
    return sorted(names)


# ---------------------------------------------------------------------------
# 4.5 単価契約、4.7 方式と理由
# ---------------------------------------------------------------------------


def is_unit_price(method_raw: str | None, amount_raw: str | None) -> bool:
    """方式の列（競争入札）または金額の列に `単価` を含めば単価契約（4.5）。"""
    return "単価" in nz.key_text(method_raw) or "単価" in nz.key_text(amount_raw)


def method_detail(method_raw: str | None) -> dict:
    """競争入札の方式の、総合評価・単価の有無（4.7）。"""
    key = nz.key_text(method_raw)
    return {"comprehensive_evaluation": "総合評価" in key, "unit_price": "単価" in key}


REASON_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("企画競争", "planning_competition"),
    ("不調", "failed_tender"),
    ("公募", "open_call"),
    ("競争を許さない", "nature_no_competition"),
    ("慈善", "charity"),
)

ARTICLE_RE = re.compile(
    r"(会計法令?|予算決算及び会計令|予決令)第?\d+条(の\d+)*(第\d+項)?(第\d+号)?"
)
_LAW_RE = re.compile(r"^(会計法令?|予算決算及び会計令|予決令)(第)?")


def reason_category(reason_raw: str | None) -> str:
    """NFKC にした理由の文字列に含まれる語を、上から順に見て分類する（4.7）。"""
    text = nz.nfkc(reason_raw or "")
    for word, category in REASON_CATEGORIES:
        if word in text:
            return category
    return "other"


def reason_articles(reason_raw: str | None) -> list[str]:
    """NFKC にした理由の文字列から根拠条文を取り出す（4.7）。

    `予算決算及び会計令` と `予決令` は `予決令` にそろえ、法令名の後の `第` の有無をそろえて（付ける）、
    重複を除いて並べ替えた配列にする。
    """
    text = nz.nfkc(reason_raw or "")
    found: set[str] = set()
    for match in ARTICLE_RE.finditer(text):
        article = match.group(0)
        law = _LAW_RE.match(article)
        assert law is not None
        name = "予決令" if law.group(1) in ("予算決算及び会計令", "予決令") else law.group(1)
        found.add(f"{name}第{article[law.end() :]}")
    return sorted(found)


# ---------------------------------------------------------------------------
# 見出しと列の数（品質検査 Q02〜Q04 と同じ規則）
# ---------------------------------------------------------------------------


def format_problems(table: readers.CsvTable, spec: FileSpec) -> list[tuple[str, str]]:
    """見出しの文字列・列の数・列の対応の食い違いを (品質検査のID, 内容) の配列で返す。

    Q03：列の数（見出しの2行と、空でないすべてのレコード）。Q02：取り込む列の見出しの文字列が、
    決めた列にあり、ほかの列にないこと。Q04：take と drop で全列を重ならずに決めていること。
    空の配列なら期待どおり。
    """
    problems: list[tuple[str, str]] = []
    taken = {int(item["index"]) for item in spec.take.values()}
    if taken & set(spec.drop):
        problems.append(("Q04", "take と drop に同じ列がある"))
    if taken | set(spec.drop) != set(range(spec.expected_columns)):
        problems.append(("Q04", "take と drop で決めていない列がある"))
    if len(table.header_rows) < readers.HEADER_ROWS:
        problems.append(("Q03", "見出しの2行がない"))
        return problems
    widths = {len(r) for r in table.header_rows}
    widths |= {len(cells) for _, cells in table.records if any(c.strip() for c in cells)}
    if widths != {spec.expected_columns}:
        problems.append(("Q03", f"列の数が {sorted(widths)} で、期待は {spec.expected_columns}"))
        return problems
    headers = table.header_strings()
    for key, item in spec.take.items():
        expected = nz.key_text(str(item["header"]))
        index = int(item["index"])
        if headers[index] != expected:
            problems.append(("Q02", f"{index}列目（{key}）の見出しが期待と違う"))
        elif headers.count(expected) != 1:
            problems.append(("Q02", f"{key} の見出しが2つ以上の列にある"))
    return problems


def _format_error(
    source_id: str, why: Sequence[tuple[str, str] | str], logical_key: str
) -> KizashiError:
    why = [w if isinstance(w, str) else f"{w[0]} {w[1]}" for w in why]
    return KizashiError(
        "E_FORMAT_CHANGED",
        "normalize",
        f"{source_id}（{logical_key}）が期待する様式と違う：" + "／".join(why),
        affected_ids=[source_id],
        next_action="開発側で原本（data/raw）の見出しと列を確かめ、dataset.yaml を直してから新しい実行を作る。",
    )


# ---------------------------------------------------------------------------
# 取得したファイルの検査（実装仕様4.2。D-025・D-027）
# ---------------------------------------------------------------------------


def _invalid(why: str) -> KizashiError:
    return KizashiError(
        "E_FETCH_INVALID",
        "fetch",
        f"取得したファイルが期待と違う：{why}",
        next_action="公表ページと取得したファイルを記者と確かめる。",
    )


def _decode_any(data: bytes, encoding: str) -> tuple[str, str] | None:
    for enc in (encoding, *[e for e in OTHER_ENCODINGS if e != encoding]):
        try:
            return readers.decode(data, enc), enc
        except readers.DecodeError:
            continue
    return None


def _has_no_header(rows: Iterable[Sequence[str]]) -> bool:
    return any(row and nz.key_text(row[0]) == "NO" for row in rows)


def validate_csv(data: bytes, spec: FileSpec) -> None:
    """取得した CSV が、期待する年度の公表データの表であることを確かめる。

    - 決めた文字コード（読めなければ他の既知の文字コード）で読め、最初の10行に「NO」の見出しの行がある。
    - 見出しの文字列と列の数が期待どおりなら、読める契約日がすべて期待の年度（4月1日〜翌年3月31日）に
      あり、読める契約日が1つ以上ある（D-025）。
    - 見出しや文字コードが期待と違うときは、ここでは止めない。原本を残し、正規化で E_FORMAT_CHANGED に
      する（D-027）。
    """
    decoded = _decode_any(data, spec.encoding)
    if decoded is None:
        raise _invalid(f"{spec.encoding} などの文字コードで読めない")
    text, encoding = decoded
    rows = readers.parse_text(text)
    if not _has_no_header(rows[:10]):
        raise _invalid("「NO」の見出しの行がない（公表データの表でない）")
    if encoding != spec.encoding:
        return
    table = readers.read_table(data, spec.encoding)
    if format_problems(table, spec):
        return
    index = spec.index_of("contract_date_raw")
    no_index = spec.index_of("no")
    assert index is not None and no_index is not None
    readable = 0
    outside: list[str] = []
    for _, cells in table.records:
        if not any(c.strip() for c in cells):
            continue
        iso = nz.wareki_to_iso(cells[index])
        if iso is None:
            continue
        readable += 1
        if nz.fiscal_year_of(iso) != spec.fiscal_year:
            outside.append(cells[no_index].strip())
    if readable == 0:
        raise _invalid(f"読める契約日がなく、{spec.fiscal_year}年度のファイルか確かめられない")
    if outside:
        listed = "、".join(outside[:MAX_LISTED]) + ("…" if len(outside) > MAX_LISTED else "")
        raise _invalid(
            f"{spec.fiscal_year}年度（4月1日〜翌年3月31日）でない契約日の行がある"
            f"（{len(outside)}行。NO：{listed}）"
        )


# ---------------------------------------------------------------------------
# 正規化（実装仕様4.1 の normalize）
# ---------------------------------------------------------------------------


def _dup_groups(source_id: str, records: Sequence[tuple[int, tuple[str, ...]]]) -> dict[int, str]:
    """NO 以外のすべての列が同じ行の組に、同じ dup_group（組の最初の行の row_ref）を付ける（4.9）。"""
    groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for no, cells in records:
        if any(c.strip() for c in cells):
            groups[tuple(cells[1:])].append(no)
    result: dict[int, str] = {}
    for numbers in groups.values():
        if len(numbers) >= 2:
            label = record_id(source_id, min(numbers))
            for n in numbers:
                result[n] = label
    return result


def normalize_source(
    conn: sqlite3.Connection,
    adapter: Adapter,
    *,
    source_id: str,
    logical_key: str,
    data: bytes,
    spec: FileSpec,
) -> dict[str, int]:
    """1つの CSV を正規化の経路で読み、records と procurement_contracts に書く。

    見出しの文字列・列の数が期待と違えば、1行も書かずに E_FORMAT_CHANGED。
    戻り値は件数（records、adopted、held、excluded）。
    """
    try:
        table = readers.read_table(data, spec.encoding)
    except readers.DecodeError as exc:
        raise _format_error(source_id, [str(exc)], logical_key) from exc
    problems = format_problems(table, spec)
    if problems:
        raise _format_error(source_id, problems, logical_key)

    patterns = list(adapter.config["group_key"]["remove_patterns"])
    keys = [k for k in spec.take if k != OFFICER_KEY]
    officer_index = spec.index_of(OFFICER_KEY)
    dups = _dup_groups(source_id, table.records)
    counts = {"records": 0, "adopted": 0, "held": 0, "excluded": 0}

    for no, cells in table.records:
        rid = record_id(source_id, no)
        counts["records"] += 1
        if not any(c.strip() for c in cells):
            _insert_record(conn, rid, source_id, no, {}, "excluded", "empty_record", adapter)
            counts["excluded"] += 1
            continue
        values = {k: cells[spec.index_of(k)].strip() for k in keys}  # type: ignore[index]
        no_text = nz.key_text(values["no"])
        if not no_text.isascii() or not no_text.isdigit():
            _insert_record(conn, rid, source_id, no, values, "held", "bad_no", adapter)
            counts["held"] += 1
            continue
        _insert_record(conn, rid, source_id, no, values, "adopted", None, adapter)
        counts["adopted"] += 1
        conn.execute(
            _CONTRACT_INSERT,
            _contract_row(
                rid, int(no_text), values, cells, officer_index, spec, patterns, dups.get(no)
            ),
        )
    return counts


def _insert_record(
    conn: sqlite3.Connection,
    rid: str,
    source_id: str,
    no: int,
    values: dict[str, str],
    status: str,
    reason: str | None,
    adapter: Adapter,
) -> None:
    cells_json = canonical_json(values)
    conn.execute(
        "INSERT INTO records (record_id, source_id, logical_no, cells_json, cells_sha256, status,"
        " status_reason, parser_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid,
            source_id,
            no,
            cells_json,
            sha256_text(cells_json),
            status,
            reason,
            adapter.parser_version,
        ),
    )


_CONTRACT_COLUMNS = (
    "record_id",
    "record_status",
    "fiscal_year",
    "partial_year",
    "method_family",
    "no",
    "title_raw",
    "group_key",
    "title_removed_parts",
    "officer_units_json",
    "officer_count",
    "contract_date",
    "counterparty_raw",
    "corp_numbers_json",
    "corp_number_state",
    "method_raw",
    "method_detail_json",
    "unit_price",
    "reason_raw",
    "reason_category",
    "reason_articles_json",
    "amount_raw",
    "amount_yen",
    "amount_note",
    "estimate_raw",
    "rate_raw",
    "note_raw",
    "dup_group",
    "incomparable_reasons_json",
)
_CONTRACT_INSERT = (
    f"INSERT INTO procurement_contracts ({', '.join(_CONTRACT_COLUMNS)})"
    f" VALUES ({', '.join('?' for _ in _CONTRACT_COLUMNS)})"
)


def _contract_row(
    rid: str,
    no: int,
    values: dict[str, str],
    cells: Sequence[str],
    officer_index: int | None,
    spec: FileSpec,
    patterns: list[str],
    dup_group: str | None,
) -> tuple:
    title = values["title_raw"]
    gkey, removed = nz.group_key(title, patterns)
    units, officer_count = split_officers(cells[officer_index] if officer_index is not None else "")
    contract_date = nz.wareki_to_iso(values["contract_date_raw"])
    corp_state, corp_numbers = nz.parse_corp_number(values["corp_number_raw"])
    amount_yen, amount_note = nz.parse_amount(values["amount_raw"])
    method_raw = values.get("method_raw")
    reason_raw = values.get("reason_raw")
    kyoso = spec.family == "kyoso"
    reasons = []
    if contract_date is None:
        reasons.append("bad_date")
    if dup_group is not None:
        reasons.append("duplicate_rows")
    row = {
        "record_id": rid,
        "record_status": "adopted",
        "fiscal_year": spec.fiscal_year,
        "partial_year": int(spec.partial_year),
        "method_family": spec.family,
        "no": no,
        "title_raw": title,
        "group_key": gkey,
        "title_removed_parts": canonical_json(removed),
        "officer_units_json": canonical_json(units),
        "officer_count": officer_count,
        "contract_date": contract_date,
        "counterparty_raw": values["counterparty_raw"],
        "corp_numbers_json": canonical_json(corp_numbers),
        "corp_number_state": corp_state,
        "method_raw": method_raw,
        "method_detail_json": canonical_json(method_detail(method_raw)) if kyoso else None,
        "unit_price": int(is_unit_price(method_raw, values["amount_raw"])),
        "reason_raw": reason_raw,
        "reason_category": None if kyoso else reason_category(reason_raw),
        "reason_articles_json": None if kyoso else canonical_json(reason_articles(reason_raw)),
        "amount_raw": values["amount_raw"],
        "amount_yen": amount_yen,
        "amount_note": amount_note,
        "estimate_raw": values.get("estimate_raw"),
        "rate_raw": values.get("rate_raw"),
        "note_raw": values.get("note_raw"),
        "dup_group": dup_group,
        "incomparable_reasons_json": canonical_json(sorted(reasons)),
    }
    return tuple(row[c] for c in _CONTRACT_COLUMNS)


# ---------------------------------------------------------------------------
# 母集団の絞り込み（実装仕様6.3。アダプタ仕様4.8a）
# ---------------------------------------------------------------------------

SCOPE_OPS = ("contains", "not_contains", "equals", "not_equals")


def _hit(value: str, op: str, target: str) -> bool:
    return target in value if op == "contains" else value == target


def filter_matches(raw: str | None, kind: str | None, op: str, target: str) -> bool:
    """1つの条件を満たすか。比べる前に両方を NFKC にし空白を除く。

    json_array の項目は、どれかの要素が contains・equals を満たせば満たす。not_contains・not_equals は、
    どの要素も（否定する前の条件を）満たさないときに満たす。値のない項目は空の文字列として比べる。
    """
    positive = op.removeprefix("not_")
    want = nz.key_text(target)
    if kind == "json_array":
        items = json.loads(raw) if raw else []
        values = [nz.key_text(v) if isinstance(v, str) else "" for v in items]
        found = any(_hit(v, positive, want) for v in values)
    else:
        found = _hit(nz.key_text(raw), positive, want)
    return found if op == positive else not found


def apply_scope(conn: sqlite3.Connection, adapter: Adapter, scope: dict) -> dict[str, int]:
    """条件に合わない行を excluded（out_of_scope）にする。行は消さない。資料ごとの除いた行の数を返す。"""
    fields = adapter.config["scope_fields"]
    filters = scope["filters"]
    columns = sorted({fields[f["field"]]["column"] for f in filters})
    # 列の名前は dataset.yaml の scope_fields から取る（提案の文字列を埋め込まない）。
    rows = conn.execute(
        f"SELECT record_id, {', '.join(columns)} FROM procurement_contracts ORDER BY record_id"
    ).fetchall()
    excluded: dict[str, int] = defaultdict(int)
    for row in rows:
        values = dict(zip(columns, row[1:], strict=True))
        keep = all(
            filter_matches(
                values[fields[f["field"]]["column"]],
                fields[f["field"]].get("kind"),
                f["op"],
                f["value"],
            )
            for f in filters
        )
        if keep:
            continue
        rid = row[0]
        conn.execute(
            "UPDATE records SET status = 'excluded', status_reason = 'out_of_scope' WHERE record_id = ?",
            (rid,),
        )
        conn.execute(
            "UPDATE procurement_contracts SET record_status = 'excluded' WHERE record_id = ?",
            (rid,),
        )
        excluded[rid.split(":")[0]] += 1
    return dict(excluded)
