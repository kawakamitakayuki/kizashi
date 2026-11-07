"""計算（implementation_spec.md 2章の calc.py、7.1〜7.3。設計書3.4の計算の依頼）。

- 基礎集計：条件（列・演算・値）から CLI が SQL を組み立てて行を選び、合計は列の値を Decimal(str(値))
  にして足す。SQLite の SUM は使わない。表と列は分野の許可リスト（アダプタの CALC_TABLES）だけ。
  条件に record_status = 'adopted' を必ず加える。NULL の行は合計に入れず、件数を request_json に残す。
- 派生計算：diff・ratio・rate・sum・product の5つだけ。引数は同じ実行の計算ID（取り下げていない、値の
  あるもの）だけ。数値の定数と、1つの依頼の中の同じ計算IDの重複は拒否する。精度28桁で計算し、丸めない。
  0での割り算と、基準が0以下の rate は E_DIV_ZERO。
- 文書値：引用の中の数値の並び（NFKC の後の、数字と桁区切り・小数点の連なり）の、桁区切りを除いた値の
  どれかと等しいことを確かめる。部分一致では照らさない。直前の -・−・+ は、その前が数字・英字でなければ
  符号として値に含める。直前に ▲・△ がある数値は、符号の意味が資料ごとの決まりなので文書値にしない（D-033）。
  符号・印と数字の間、符号の前の空白は飛ばして見る（引用の照合が空白を除いて比べるため。D-036）。

呼び出し側（record）がトランザクションを持つ。
"""

from __future__ import annotations

import decimal
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from kizashi import ledger
from kizashi.adapters import Adapter
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json, sha256_text
from kizashi.normalize import nfkc

STAGE = "record"
BASE_OPS = ("sum", "count")
FILTER_OPS = ("=", "!=", "in", "is_null", "not_null")
DERIVE_OPS = ("diff", "ratio", "rate", "sum", "product")
PRECISION = 28

_DOC_NUMBER_RE = re.compile(r"(?<![\d,.])[\d,.]*\d[\d,.]*(?![\d,.])")
# 文書値・係数の value の十進数の文字列。数字は ASCII の 0〜9 だけ（re.ASCII。全角などの数字を通さない。D-092①）
DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$", re.ASCII)
# 数値の並びの直前の符号（NFKC の後。全角の －・＋ は - と + になる）と、意味を決めない印（D-033）。
SIGNS = {"-": -1, "−": -1, "+": 1}
MARKS = ("▲", "△")
_SIGN_BLOCKER_RE = re.compile(r"[0-9A-Za-z]")  # 符号の前にあれば、範囲・日付・記号の「-」とみなす


def _err(
    code: str, message: str, next_action: str = "", affected: Sequence[str] = ()
) -> KizashiError:
    return KizashiError(code, STAGE, message, affected_ids=affected, next_action=next_action)


def fmt(value: Decimal) -> str:
    """十進数の文字列（指数表記にしない）。"""
    return format(value, "f")


# ---------------------------------------------------------------------------
# 7.1 基礎集計
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseResult:
    sql: str
    params: tuple
    row_refs: list[str]
    value: str
    null_rows: int


# 条件の値を整数に限る列（D-042②。年度の文字列で依存する資料が分からなくならないように）
INTEGER_COLUMNS = frozenset({"fiscal_year"})


# SQLite の整数の範囲。外の整数は SQLite に渡せないので、型の誤り（E_SCHEMA）にする（1-14）
SQLITE_INT_MIN, SQLITE_INT_MAX = -(2**63), 2**63 - 1


def _is_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and SQLITE_INT_MIN <= value <= SQLITE_INT_MAX
    )


def _scalar_ok(value: object) -> bool:
    return isinstance(value, str) or _is_int(value)


def _allowlist(adapter: Adapter) -> dict[str, dict[str, tuple[str, ...]]]:
    return getattr(adapter.module, "CALC_TABLES", {})


def build_query(
    adapter: Adapter, request: dict, *, allow_empty_in: bool = False
) -> tuple[str, tuple, str, str | None]:
    """基礎集計の依頼を検査し、(SQL, パラメーター, op, 合計の列) を返す。

    allow_empty_in は、run の検出（detect.py）が row_refs の行を `record_id in` で選ぶときだけ真にする
    （合計できる行のない年度の件数0を、同じ組み立てで登録するため）。提案の calc では空の in を拒否する。
    """
    op = request.get("op")
    if op not in BASE_OPS:
        raise _err("E_SCHEMA", f"基礎集計の op は {'・'.join(BASE_OPS)} のどれか：{op!r}")
    table = request.get("table")
    allowed = _allowlist(adapter)
    if not isinstance(table, str):
        raise _err("E_SCHEMA", f"table は表の名前の文字列で書く：{table!r}")
    if table not in allowed:
        raise _err(
            "E_NOT_ALLOWED_COLUMN",
            f"基礎集計に使えない表：{table!r}",
            next_action=f"使える表：{'・'.join(allowed)}",
        )
    spec = allowed[table]
    column = request.get("column")
    if op == "sum":
        if column is None:
            raise _err("E_SCHEMA", "op が sum のときは column（合計する列）が要る。")
        if column not in spec["sum"]:
            raise _err(
                "E_NOT_ALLOWED_COLUMN",
                f"合計に使えない列：{column!r}",
                next_action=f"合計に使える列：{'・'.join(spec['sum'])}",
            )
    elif column is not None:
        raise _err("E_SCHEMA", "op が count のときは column を書かない。")
    filters = request.get("filter")
    if not isinstance(filters, list):
        raise _err("E_SCHEMA", 'filter は [{"col", "op", "value"}] の配列で書く。')

    where = ["record_status = 'adopted'"]
    params: list[object] = []
    for item in filters:
        if not isinstance(item, dict) or "col" not in item or "op" not in item:
            raise _err("E_SCHEMA", 'filter の要素は {"col", "op", "value"} の形で書く。')
        col, fop = item["col"], item["op"]
        if col not in spec["filter"]:
            raise _err(
                "E_NOT_ALLOWED_COLUMN",
                f"条件に使えない列：{col!r}（record_status は CLI が必ず加える）",
                next_action=f"条件に使える列：{'・'.join(spec['filter'])}",
            )
        if fop not in FILTER_OPS:
            raise _err("E_SCHEMA", f"条件の op は {'・'.join(FILTER_OPS)} のどれか：{fop!r}")
        keys = set(item)
        if fop in ("is_null", "not_null"):
            if keys != {"col", "op"}:
                raise _err("E_SCHEMA", f"{fop} の条件には value を書かない。")
            where.append(f"{col} IS {'NOT ' if fop == 'not_null' else ''}NULL")
            continue
        if keys != {"col", "op", "value"}:
            raise _err("E_SCHEMA", 'filter の要素は {"col", "op", "value"} の形で書く。')
        value = item["value"]
        if col in INTEGER_COLUMNS and not (
            all(_is_int(v) for v in value) if isinstance(value, list) else _is_int(value)
        ):
            raise _err(
                "E_SCHEMA",
                f'{col} の value は整数（in は整数の配列）で書く：{value!r}（"2024" のような文字列は使えない）',
                next_action="年度は西暦の整数で書く（例：2024）。",
            )
        if fop == "in":
            if (
                not isinstance(value, list)
                or (not value and not allow_empty_in)
                or not all(_scalar_ok(v) for v in value)
            ):
                raise _err(
                    "E_SCHEMA",
                    "in の value は、文字列か整数（−2^63〜2^63−1）の空でない配列で書く。",
                )
            where.append(f"{col} IN ({', '.join('?' for _ in value)})")
            params.extend(value)
        else:
            if not _scalar_ok(value):
                raise _err(
                    "E_SCHEMA",
                    f"{fop} の value は文字列か整数（−2^63〜2^63−1）で書く：{value!r}",
                )
            where.append(f"{col} {fop} ?")
            params.append(value)
    selected = "record_id" + (f", {column}" if op == "sum" else "")
    # 表と列の名前は許可リストから取ったもの（利用者の入力をそのまま埋め込まない）。
    sql = f"SELECT {selected} FROM {table} WHERE {' AND '.join(where)} ORDER BY record_id"
    return sql, tuple(params), op, column if op == "sum" else None


def base_aggregate(
    conn: sqlite3.Connection, adapter: Adapter, request: dict, *, allow_empty_in: bool = False
) -> BaseResult:
    sql, params, op, _ = build_query(adapter, request, allow_empty_in=allow_empty_in)
    rows = conn.execute(sql, params).fetchall()
    row_refs = sorted(r[0] for r in rows)
    null_rows = 0
    if op == "sum":
        total = Decimal(0)
        for _, value in rows:
            if value is None:
                null_rows += 1
            else:
                total += Decimal(str(value))
        result = fmt(total)
    else:
        result = str(len(rows))
    return BaseResult(sql=sql, params=params, row_refs=row_refs, value=result, null_rows=null_rows)


def _insert(
    conn: sqlite3.Connection,
    *,
    kind: str,
    request: dict,
    value: str,
    unit: str,
    meaning: str,
    lead_id: str | None,
    origin: str,
    sql: str | None = None,
    formula: str | None = None,
    ref_calc_ids: Sequence[str] | None = None,
    row_refs: Sequence[str] | None = None,
    result: dict | None = None,
) -> str:
    calc_id = ledger.next_id(conn, "K")
    refs_json = None if row_refs is None else canonical_json(sorted(row_refs))
    conn.execute(
        "INSERT INTO computations (calc_id, kind, lead_id, request_json, sql_text, sql_sha256, formula,"
        " ref_calc_ids_json, value, unit, meaning, row_refs_json, row_refs_sha256, row_count,"
        " result_json, origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            calc_id,
            kind,
            lead_id,
            canonical_json(request),
            sql,
            None if sql is None else sha256_text(sql),
            formula,
            None if ref_calc_ids is None else canonical_json(list(ref_calc_ids)),
            value,
            unit,
            meaning,
            refs_json,
            None if refs_json is None else sha256_text(refs_json),
            None if row_refs is None else len(row_refs),
            None if result is None else canonical_json(result),
            origin,
            ledger.now_iso(),
        ),
    )
    return calc_id


def register_base(
    conn: sqlite3.Connection,
    adapter: Adapter,
    request: dict,
    *,
    meaning: str,
    unit: str,
    lead_id: str | None = None,
    origin: str = "record",
    extra: dict | None = None,
    allow_empty_in: bool = False,
    expect_rows: int | None = None,
) -> str:
    """基礎集計を計算して登録し、計算IDを返す。

    extra は request_json に足す項目（run の検出が、除いた行と理由と件数を残すのに使う。実装仕様4.8）。
    expect_rows を渡すと、選ばれた行の数がそれと違えば E_INTERNAL にする（検出SQLの row_refs が、採用した行と
    合わないとき）。
    """
    res = base_aggregate(conn, adapter, request, allow_empty_in=allow_empty_in)
    if expect_rows is not None and len(res.row_refs) != expect_rows:
        raise KizashiError(
            "E_INTERNAL",
            "run",
            f"検出SQLの row_refs（{expect_rows}行）と、分野の表で選ばれた採用の行（{len(res.row_refs)}行）が"
            "合わない。",
            next_action="開発側で検出SQLを直す。",
        )
    stored = {k: request[k] for k in ("op", "table", "column", "filter") if k in request}
    stored["null_rows"] = res.null_rows
    stored.update(extra or {})
    return _insert(
        conn,
        kind="base",
        request=stored,
        value=res.value,
        unit=unit,
        meaning=meaning,
        lead_id=lead_id,
        origin=origin,
        sql=res.sql,
        row_refs=res.row_refs,
        result={"params": list(res.params)},
    )


# ---------------------------------------------------------------------------
# 7.3 派生計算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Derived:
    value: str
    formula: str


def _bad_expr(message: str) -> KizashiError:
    return _err(
        "E_BAD_EXPR",
        message,
        next_action="演算は diff・ratio・rate・sum・product、引数は計算IDだけ。資料の数値は doc_value で"
        "登録してから使う。",
    )


def derive_value(conn: sqlite3.Connection, op: object, args: object) -> Derived:
    """派生計算の値を計算する（登録はしない）。"""
    if op not in DERIVE_OPS:
        raise _bad_expr(f"知らない演算：{op!r}")
    if not isinstance(args, list):
        raise _bad_expr("args は計算IDの配列で書く。")
    for arg in args:
        if not isinstance(arg, str) or ledger.id_kind(arg) != "calc":
            raise _bad_expr(f"計算ID以外の引数：{arg!r}（数値の定数は使えない）")
    if len(set(args)) != len(args):
        raise _bad_expr("1つの依頼の中で同じ計算IDを2回使っている。")
    if op == "sum" and len(args) < 2:
        raise _bad_expr("sum の引数は2つ以上。")
    if op != "sum" and len(args) != 2:
        raise _bad_expr(f"{op} の引数は2つ。")
    values: list[Decimal] = []
    for arg in args:
        row = conn.execute(
            "SELECT value, state FROM computations WHERE calc_id = ?", (arg,)
        ).fetchone()
        if row is None:
            raise _err("E_REF_NOT_FOUND", f"計算がない：{arg}", affected=(arg,))
        value, state = row
        if state != "active":
            raise _err("E_REF_NOT_FOUND", f"取り下げた計算は使えない：{arg}", affected=(arg,))
        if value is None:
            raise _bad_expr(f"値のない計算（照合の計算など）は引数にできない：{arg}")
        values.append(Decimal(value))
    with decimal.localcontext() as ctx:
        ctx.prec = PRECISION
        if op == "sum":
            result = sum(values, Decimal(0))
        else:
            a, b = values
            if op == "diff":
                result = a - b
            elif op == "product":
                result = a * b
            elif op == "ratio":
                if b == 0:
                    raise _err("E_DIV_ZERO", f"0での割り算（{args[1]} が0）。", affected=(args[1],))
                result = a / b
            else:  # rate
                if b <= 0:
                    raise _err(
                        "E_DIV_ZERO",
                        f"増減率の基準（{args[1]}）が0以下。",
                        next_action="基準が0以下の増減率は作らない（V5_BAD_RATE と同じ考え方）。",
                        affected=(args[1],),
                    )
                result = (a - b) / b
        result = +result  # 精度28桁にそろえる
    return Derived(value=fmt(result), formula=f"{op}({', '.join(args)})")


def register_derived(
    conn: sqlite3.Connection,
    op: object,
    args: object,
    *,
    meaning: str,
    unit: str,
    lead_id: str | None = None,
    origin: str = "record",
) -> str:
    derived = derive_value(conn, op, args)
    assert isinstance(args, list)
    return _insert(
        conn,
        kind="derived",
        request={"op": op, "args": args},
        value=derived.value,
        unit=unit,
        meaning=meaning,
        lead_id=lead_id,
        origin=origin,
        formula=derived.formula,
        ref_calc_ids=args,
    )


# ---------------------------------------------------------------------------
# 7.2 文書値
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteNumber:
    value: Decimal
    marked: bool  # 直前に ▲・△ がある（文書値にしない）


def _char_before(text: str, index: int) -> tuple[str, int]:
    """index より前の、空白でない最も近い文字とその位置。なければ ("", -1)。"""
    i = index - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    return (text[i], i) if i >= 0 else ("", -1)


def numbers_in_quote(quote: str) -> list[QuoteNumber]:
    """引用の中の数値の並び（桁区切りを除いて Decimal にできるもの）。符号と印を見る。"""
    text = nfkc(quote)
    found: list[QuoteNumber] = []
    for m in _DOC_NUMBER_RE.finditer(text):
        # 並びの末尾の「.」（NFKC の後。全角の「．」を含む）は、直後が数字でないので小数点ではなく句点（8章。
        # D-090⑪）。除いても数値にならない並び（1.2.3 など）は、これまでどおり取り出さない
        digits = m.group().rstrip(".")
        try:
            value = Decimal(digits.replace(",", ""))
        except decimal.InvalidOperation:
            continue
        prev, at = _char_before(text, m.start())
        before, _ = _char_before(text, at)
        if prev in SIGNS and not _SIGN_BLOCKER_RE.fullmatch(before):
            value = value.copy_negate() if SIGNS[prev] < 0 else value
        found.append(QuoteNumber(value=value, marked=prev in MARKS))
    return found


def check_doc_value(quote: str, value: object) -> Decimal:
    """value（十進数の文字列）が、引用の中の数値の並びのどれかと等しいことを確かめる。"""
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise _err(
            "E_SCHEMA",
            f"文書値の value は十進数の文字列（桁区切りなし）で書く：{value!r}",
            next_action='例："1200"、"0.1"、"-12"',
        )
    target = Decimal(value)
    numbers = numbers_in_quote(quote)
    if any(not n.marked and n.value == target for n in numbers):
        return target
    if any(n.marked and abs(n.value) == abs(target) for n in numbers):
        raise _err(
            "E_DOC_VALUE_MISMATCH",
            f"文書値 {value} に当たる引用の中の数値の直前に ▲・△ がある。負を示すかは資料ごとの決まりなので、"
            "文書値にしない（D-033）。",
            next_action="▲・△ の意味を記者に確かめる（資料に凡例があれば、その箇所を示す）。",
        )
    raise _err(
        "E_DOC_VALUE_MISMATCH",
        f"文書値 {value} が、引用の中の数値の並びのどれとも等しくない（部分一致では照らさない。"
        "符号も照らす）。",
        next_action="引用の中の数値をそのまま（桁区切りを除き、符号を付けて）value に書く。",
    )


def register_doc_value(
    conn: sqlite3.Connection,
    *,
    lead_id: str,
    evidence: dict,
    value: str,
    meaning: str,
    unit: str,
) -> str:
    check_doc_value(evidence["quote"], value)
    return _insert(
        conn,
        kind="doc_value",
        request={
            "source_id": evidence["source_id"],
            "position": evidence["position"],
            "quote": evidence["quote"],
        },
        value=value,
        unit=unit,
        meaning=meaning,
        lead_id=lead_id,
        origin="record",
    )


def request_of(conn: sqlite3.Connection, calc_id: str) -> tuple[str, dict] | None:
    """計算の (種類, 依頼)。なければ None。"""
    row = conn.execute(
        "SELECT kind, request_json FROM computations WHERE calc_id = ?", (calc_id,)
    ).fetchone()
    return None if row is None else (row[0], json.loads(row[1]))
