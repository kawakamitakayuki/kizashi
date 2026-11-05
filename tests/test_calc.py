"""計算（test_plan.md の CA-1〜CA-3。implementation_spec.md 7.1〜7.3、アダプタ仕様8章の許可リスト）。

期待値は合成の CSV（tests/fixtures/make_cao_fixtures.py）から手で計算した：
- 翻訳業務：令和6年度 S001:000012 の 650,000円、令和7年度 S005:000007 の 750,000円。
- 差 100,000。比 75/65 = 15/13、増減率 10/65 = 2/13（精度28桁で丸める）。
- 令和7年度の競争入札は12行（すべて採用）。
- 物品の購入：令和7年度の競争入札 S005:000005・S005:000006、令和6年度の随意契約 S003:000003（外貨で整数にならない）。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from kizashi import calc, ledger
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError


@pytest.fixture
def env(repo: Path, synthetic_run: str):
    conn = ledger.open_rw(repo, synthetic_run)
    yield conn, load_adapter(repo, "cao-procurement")
    conn.close()


def f(col: str, op: str, value: object = None) -> dict:
    item = {"col": col, "op": op}
    if op not in ("is_null", "not_null"):
        item["value"] = value
    return item


def base(env, op: str, filters: list[dict], column: str | None = "amount_yen") -> str:
    conn, adapter = env
    request = {"op": op, "table": "procurement_contracts", "filter": filters}
    if op == "sum":
        request["column"] = column
    with ledger.transaction(conn):
        return calc.register_base(conn, adapter, request, meaning="テストの集計", unit="円")


def row(conn: sqlite3.Connection, calc_id: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM computations WHERE calc_id = ?", (calc_id,)).fetchone()
    finally:
        conn.row_factory = None


# ---------------------------------------------------------------------------
# CA-1 基礎集計
# ---------------------------------------------------------------------------


def test_ca1_sum_and_count(env) -> None:
    """CA-1：SQL で行を選び、合計は Decimal、件数は行の数。row_refs は並べ替えて件数とハッシュを残す。"""
    conn, _ = env
    k1 = base(env, "sum", [f("group_key", "=", "翻訳業務"), f("fiscal_year", "=", 2024)])
    k2 = base(env, "count", [f("fiscal_year", "=", 2025), f("method_family", "=", "kyoso")], None)
    r1, r2 = row(conn, k1), row(conn, k2)
    assert (r1["kind"], r1["value"], r1["unit"], r1["meaning"]) == (
        "base",
        "650000",
        "円",
        "テストの集計",
    )
    assert r1["row_refs_json"] == '["S001:000012"]' and r1["row_count"] == 1
    assert len(r1["row_refs_sha256"]) == 64 and r1["origin"] == "record"
    assert "record_status = 'adopted'" in r1["sql_text"] and len(r1["sql_sha256"]) == 64
    assert r2["value"] == "12" and r2["row_count"] == 12
    assert r2["row_refs_json"].startswith('["S005:000001","S005:000002"')


def test_ca1_adopted_is_always_added(env) -> None:
    """CA-1：条件に書かなくても record_status = 'adopted' が加わり、excluded の行は数えない。"""
    conn, _ = env
    conn.execute(
        "UPDATE procurement_contracts SET record_status = 'excluded' WHERE record_id = 'S005:000007'"
    )
    k1 = base(env, "count", [f("fiscal_year", "=", 2025), f("method_family", "=", "kyoso")], None)
    k2 = base(env, "sum", [f("group_key", "=", "翻訳業務"), f("fiscal_year", "=", 2025)])
    assert row(conn, k1)["value"] == "11"
    assert (row(conn, k2)["value"], row(conn, k2)["row_count"]) == ("0", 0)


def test_ca1_decimal_not_float(env) -> None:
    """CA-1：十進数の文字列を Decimal で足す（SQLite の SUM の 0.30000000000000004 にならない）。"""
    conn, _ = env
    conn.execute(
        "UPDATE procurement_contracts SET amount_yen = 0.1 WHERE record_id = 'S005:000005'"
    )
    conn.execute(
        "UPDATE procurement_contracts SET amount_yen = 0.2 WHERE record_id = 'S005:000006'"
    )
    filters = [f("group_key", "=", "物品の購入"), f("fiscal_year", "=", 2025)]
    k = base(env, "sum", filters)
    assert row(conn, k)["value"] == "0.3"
    (sqlite_sum,) = conn.execute(
        "SELECT SUM(amount_yen) FROM procurement_contracts WHERE group_key = '物品の購入'"
        " AND fiscal_year = 2025"
    ).fetchone()
    assert sqlite_sum != 0.3  # SQLite の SUM は浮動小数点で足す


def test_ca1_null_rows_are_counted_in_request(env) -> None:
    """CA-1：NULL の行は合計に入れず、件数を request_json に残す（row_refs には入る）。"""
    conn, _ = env
    k = base(env, "sum", [f("group_key", "=", "物品の購入"), f("fiscal_year", "=", 2024)])
    r = row(conn, k)
    assert (r["value"], r["row_refs_json"]) == ("0", '["S003:000003"]')
    assert '"null_rows":1' in r["request_json"]


def test_ca1_filter_ops(env) -> None:
    """CA-1：条件の演算 =、!=、in、is_null、not_null。"""
    conn, _ = env
    k_in = base(env, "count", [f("group_key", "in", ["翻訳業務", "研修会場借上げ"])], None)
    k_ne = base(
        env, "count", [f("fiscal_year", "=", 2025), f("method_family", "!=", "kyoso")], None
    )
    k_null = base(env, "count", [f("dup_group", "not_null")], None)
    k_isnull = base(env, "count", [f("fiscal_year", "=", 2024), f("dup_group", "is_null")], None)
    assert row(conn, k_in)["value"] == "4"  # 翻訳業務2行＋研修会場借上げ2行
    assert row(conn, k_ne)["value"] == "7"  # 令和7年度の随意契約
    assert row(conn, k_null)["value"] == "2"  # 令和7年度の物品の購入（NO 以外が同じ2行）
    assert row(conn, k_isnull)["value"] == "21"  # 令和6年度の12行＋9行


@pytest.mark.parametrize(
    ("request_", "code"),
    [
        (
            {"op": "sum", "table": "procurement_contracts", "column": "estimate_raw", "filter": []},
            "E_NOT_ALLOWED_COLUMN",
        ),
        ({"op": "count", "table": "records", "filter": []}, "E_NOT_ALLOWED_COLUMN"),
        (
            {
                "op": "count",
                "table": "procurement_contracts",
                "filter": [{"col": "title_raw", "op": "=", "value": "x"}],
            },
            "E_NOT_ALLOWED_COLUMN",
        ),
        (
            {
                "op": "count",
                "table": "procurement_contracts",
                "filter": [{"col": "record_status", "op": "=", "value": "held"}],
            },
            "E_NOT_ALLOWED_COLUMN",
        ),
        (
            {
                "op": "count",
                "table": "procurement_contracts",
                "filter": [{"col": "group_key", "op": "like", "value": "%"}],
            },
            "E_SCHEMA",
        ),
        (
            {
                "op": "count",
                "table": "procurement_contracts",
                "filter": [{"col": "group_key", "op": "in", "value": []}],
            },
            "E_SCHEMA",
        ),
        (
            {
                "op": "count",
                "table": "procurement_contracts",
                "filter": [{"col": "group_key", "op": "=", "value": True}],
            },
            "E_SCHEMA",
        ),
        (
            {
                "op": "count",
                "table": "procurement_contracts",
                "filter": [{"col": "dup_group", "op": "is_null", "value": 1}],
            },
            "E_SCHEMA",
        ),
        ({"op": "avg", "table": "procurement_contracts", "filter": []}, "E_SCHEMA"),
        ({"op": "sum", "table": "procurement_contracts", "filter": []}, "E_SCHEMA"),
        (
            {"op": "count", "table": "procurement_contracts", "column": "amount_yen", "filter": []},
            "E_SCHEMA",
        ),
        (
            {"op": "count", "table": "procurement_contracts", "filter": {"col": "fiscal_year"}},
            "E_SCHEMA",
        ),
    ],
)
def test_ca1_rejects_unallowed_requests(env, request_: dict, code: str) -> None:
    """CA-1：許可リスト（アダプタ仕様8章）にない表・列は E_NOT_ALLOWED_COLUMN、形の誤りは E_SCHEMA。"""
    conn, adapter = env
    with pytest.raises(KizashiError) as info:
        calc.base_aggregate(conn, adapter, request_)
    assert info.value.code == code


# ---------------------------------------------------------------------------
# CA-2 派生計算
# ---------------------------------------------------------------------------


@pytest.fixture
def pair(env) -> tuple[str, str]:
    a = base(env, "sum", [f("group_key", "=", "翻訳業務"), f("fiscal_year", "=", 2025)])  # 750000
    b = base(env, "sum", [f("group_key", "=", "翻訳業務"), f("fiscal_year", "=", 2024)])  # 650000
    return a, b


def derive(env, op: str, args: list) -> str:
    conn, _ = env
    with ledger.transaction(conn):
        return calc.register_derived(conn, op, args, meaning="テストの派生計算", unit="円")


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ("diff", "100000"),
        ("ratio", "1.153846153846153846153846154"),
        ("rate", "0.1538461538461538461538461538"),
        ("sum", "1400000"),
        ("product", "487500000000"),
    ],
)
def test_ca2_five_ops(env, pair, op: str, expected: str) -> None:
    """CA-2：5つの演算の値が手計算と合う（精度28桁、丸めない）。"""
    conn, _ = env
    a, b = pair
    k = derive(env, op, [a, b])
    r = row(conn, k)
    assert r["value"] == expected
    assert (r["kind"], r["formula"], r["ref_calc_ids_json"]) == (
        "derived",
        f"{op}({a}, {b})",
        f'["{a}","{b}"]',
    )
    assert Decimal(r["value"]) == Decimal(expected)


def test_ca2_derived_of_derived_and_sum_of_three(env, pair) -> None:
    """CA-2：派生計算の計算IDも引数に使える。sum は2つ以上。"""
    conn, _ = env
    a, b = pair
    d = derive(env, "diff", [a, b])
    assert row(conn, derive(env, "ratio", [d, b]))["value"] == "0.1538461538461538461538461538"
    assert row(conn, derive(env, "sum", [a, b, d]))["value"] == "1500000"


@pytest.mark.parametrize(
    "args_of",
    [
        lambda a, b: ("mul", [a, b]),
        lambda a, b: ("diff", [a, 1200000]),
        lambda a, b: ("diff", [a, "1200000"]),
        lambda a, b: ("ratio", [a, a]),
        lambda a, b: ("sum", [a, b, a]),
        lambda a, b: ("diff", [a, b, b]),
        lambda a, b: ("sum", [a]),
        lambda a, b: ("rate", a),
        lambda a, b: ("diff", [a, "K0001"]),  # 照合の計算（値がない）
    ],
)
def test_ca2_rejects_bad_expressions(env, pair, args_of) -> None:
    """CA-2：知らない演算、数値の定数、同じ計算IDの重複、引数の数の誤り、値のない計算は E_BAD_EXPR。"""
    conn, _ = env
    op, args = args_of(*pair)
    with pytest.raises(KizashiError) as info:
        calc.derive_value(conn, op, args)
    assert info.value.code == "E_BAD_EXPR"


def test_ca2_rejects_missing_or_withdrawn(env, pair) -> None:
    """CA-2：存在しない計算ID、取り下げた計算IDは E_REF_NOT_FOUND。"""
    conn, _ = env
    a, b = pair
    with pytest.raises(KizashiError) as info:
        calc.derive_value(conn, "diff", [a, "K0999"])
    assert info.value.code == "E_REF_NOT_FOUND"
    conn.execute("UPDATE computations SET state = 'withdrawn' WHERE calc_id = ?", (b,))
    with pytest.raises(KizashiError) as info:
        calc.derive_value(conn, "diff", [a, b])
    assert info.value.code == "E_REF_NOT_FOUND"


def test_ca2_division_by_zero_and_nonpositive_rate_base(env, pair) -> None:
    """CA-2：0での割り算と、基準が0以下の rate は E_DIV_ZERO。"""
    conn, _ = env
    a, _ = pair
    zero = base(env, "sum", [f("group_key", "=", "翻訳業務"), f("fiscal_year", "=", 2023)])
    negative = derive(env, "diff", [zero, a])  # -750000
    for op, args in (("ratio", [a, zero]), ("rate", [a, zero]), ("rate", [a, negative])):
        with pytest.raises(KizashiError) as info:
            calc.derive_value(conn, op, args)
        assert info.value.code == "E_DIV_ZERO"


# ---------------------------------------------------------------------------
# CA-3 文書値
# ---------------------------------------------------------------------------


def test_ca3_doc_value_is_a_whole_number_sequence() -> None:
    """CA-3：引用の中の数値の並び（桁区切りを除く）と等しい値だけを受け付ける。部分一致にしない。"""
    quote = "配送予定数量は年間1,200件とする。"
    assert calc.check_doc_value(quote, "1200") == Decimal(1200)
    assert calc.check_doc_value(quote, "1200.0") == Decimal(1200)
    for value in ("200", "120", "1", "12"):
        with pytest.raises(KizashiError) as info:
            calc.check_doc_value(quote, value)
        assert info.value.code == "E_DOC_VALUE_MISMATCH"
    two = "令和7年度は１，２００件、前年度は1,000件とし、税率は0.1とする。"
    assert calc.check_doc_value(two, "1000") == Decimal(1000)
    assert calc.check_doc_value(two, "1200") == Decimal(1200)
    assert calc.check_doc_value(two, "0.1") == Decimal("0.1")
    assert calc.check_doc_value(two, "7") == Decimal(7)  # 「令和7年度」の7も数値の並び
    for bad in ("1,200", "", "abc", "1e3", 1200):
        with pytest.raises(KizashiError) as info:
            calc.check_doc_value(quote, bad)
        assert info.value.code == "E_SCHEMA"


def mismatch(quote: str, value: str) -> KizashiError:
    with pytest.raises(KizashiError) as info:
        calc.check_doc_value(quote, value)
    assert info.value.code == "E_DOC_VALUE_MISMATCH", (quote, value)
    return info.value


def test_ca3_sign_is_part_of_the_number() -> None:
    """CA-3（D-033）：数値の直前の -・－・−・+ は、その前が数字・英字でなければ符号として値に含める。"""
    for quote in ("増減は-12件。", "増減は－12件。", "増減は−12件。", "増減は(-12)件。"):
        assert calc.check_doc_value(quote, "-12") == Decimal(-12)
        mismatch(quote, "12")
    assert calc.check_doc_value("増減は+5件。", "5") == Decimal(5)
    assert calc.check_doc_value("増減は＋5件。", "5") == Decimal(5)
    mismatch("増減は+5件。", "-5")
    # 数字・英字の後の「-」は符号にしない（範囲、日付、記号）
    quote = "期間は2024-03-01から、番号はA-12、範囲は10-15件とする。"
    for value in ("12", "3", "1", "15", "2024", "10"):
        assert calc.check_doc_value(quote, value) == Decimal(value)
    for value in ("-12", "-3", "-1", "-15"):
        mismatch(quote, value)


def test_ca3_triangle_marks_are_not_interpreted() -> None:
    """CA-3（D-033）：▲・△ が直前にある数値は、符号の意味が資料ごとの決まりなので文書値にしない。"""
    for quote, magnitude in (("増減は▲12件。", "12"), ("差は△1,200円とする。", "1200")):
        for value in (magnitude, f"-{magnitude}"):
            err = mismatch(quote, value)
            assert "▲" in err.message and "△" in err.message
            assert "記者" in err.next_action
    # 同じ引用の中に印のない同じ数値があれば、それと照らす
    assert calc.check_doc_value("前年度は12件、今年度は▲12件。", "12") == Decimal(12)


def test_ca3_sign_and_mark_across_whitespace() -> None:
    """CA-3（D-036）：引用の照合は空白を除いて比べるので、符号・印と数字の間の空白は飛ばして見る。"""
    assert calc.check_doc_value("増減は - 12件。", "-12") == Decimal(-12)
    mismatch("増減は - 12件。", "12")
    err = mismatch("増減は▲ 12件。", "12")
    assert "▲" in err.message
    assert calc.check_doc_value("範囲は10 - 15件。", "15") == Decimal(15)  # 前が数字なら符号でない
    assert calc.check_doc_value("比率は.5とする。", "0.5") == Decimal(
        "0.5"
    )  # 先頭の「.」も値に含む
    mismatch("比率は.5とする。", "5")
