"""正規化の経路の共通の関数（implementation_spec.md 2章の normalize.py。アダプタ仕様4.1・4.2・4.4・4.6・4.8）。

NFKC、空白の除去、和暦の日付、金額、法人番号、件名のまとめのキー。
元の値は変えない（呼び出し側が records.cells_json に元の値を残す）。
別経路の読み込み処理（independent_reader.py）は、この関数を呼ばない（アダプタ仕様6章）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import date

_WS_RE = re.compile(r"\s+")

# 4.1：値の欠け。NFKC にすると「－」（U+FF0D）は「-」になる。
MISSING_VALUES = frozenset({"", "-", "―"})


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def key_text(text: str | None) -> str:
    """比較やキーに使う形：NFKC にし、空白と改行をすべて除く（4.1）。"""
    if text is None:
        return ""
    return _WS_RE.sub("", nfkc(text))


def is_missing(raw: str | None) -> bool:
    """空欄、-、－、― は値の欠け（4.1）。"""
    return key_text(raw) in MISSING_VALUES


# ---------------------------------------------------------------------------
# 4.2 日付
# ---------------------------------------------------------------------------

REIWA_OFFSET = 2018
_R_DATE_RE = re.compile(r"R(\d+)\.(\d+)\.(\d+)")
_REIWA_DATE_RE = re.compile(r"令和(\d+)年(\d+)月(\d+)日")


def wareki_to_iso(raw: str | None) -> str | None:
    """`R6.4.1` と `令和6年4月1日` の形を ISO（2024-04-01）にする。どちらでもなければ None。

    暦にない日付（R6.2.30 など）も None にする。
    """
    text = key_text(raw)
    match = _R_DATE_RE.fullmatch(text) or _REIWA_DATE_RE.fullmatch(text)
    if match is None:
        return None
    year, month, day = (int(g) for g in match.groups())
    if year < 1:
        return None
    try:
        return date(year + REIWA_OFFSET, month, day).isoformat()
    except ValueError:
        return None


def fiscal_year_of(iso_date: str) -> int:
    """会計年度（4月1日〜翌年3月31日）。"""
    d = date.fromisoformat(iso_date)
    return d.year if d.month >= 4 else d.year - 1


# ---------------------------------------------------------------------------
# 4.4 金額
# ---------------------------------------------------------------------------

AMOUNT_RE = re.compile(r"\d{1,3}(,\d{3})*|\d+")
FOREIGN_CURRENCY_WORDS = ("ドル", "USD", "EUR", "ポンド", "ウォン", "ユーロ")


def parse_amount(raw: str | None) -> tuple[int | None, str | None]:
    """金額を (amount_yen, amount_note) にする（4.4）。

    `^\\d{1,3}(,\\d{3})*$` か `^\\d+$` なら整数の円。それ以外は None と種類：
    空欄・-・－ は missing、外貨は foreign_currency、`単価` を含むものは unit_price_text、
    それ以外は non_numeric。数値の判定は NFKC と前後の空白の除去の後に行う（中の空白は除かない）。
    """
    text = nfkc(raw or "").strip()
    if AMOUNT_RE.fullmatch(text):
        return int(text.replace(",", "")), None
    key = key_text(raw)
    if key in MISSING_VALUES:
        return None, "missing"
    if any(word in key for word in FOREIGN_CURRENCY_WORDS):
        return None, "foreign_currency"
    if "単価" in key:
        return None, "unit_price_text"
    return None, "non_numeric"


# ---------------------------------------------------------------------------
# 4.6 法人番号
# ---------------------------------------------------------------------------

_CORP13_RE = re.compile(r"\d{13}")
_EXPONENT_RE = re.compile(r"\d\.\d+E\+\d+")


def parse_corp_number(raw: str | None) -> tuple[str, list[str]]:
    """法人番号を (状態, 13桁の文字列の配列) にする（4.6）。

    digits13：13桁の数字がちょうど1つ。exponent：`\\d\\.\\d+E\\+\\d+` の形（復元しない）。
    missing：空欄・-・－。multiple：13桁の数字が2つ以上（改行などで区切られる）。invalid：それ以外。
    """
    if is_missing(raw):
        return "missing", []
    tokens = nfkc(raw or "").split()
    if len(tokens) == 1 and _EXPONENT_RE.fullmatch(tokens[0]):
        return "exponent", []
    if tokens and all(_CORP13_RE.fullmatch(t) for t in tokens):
        return ("digits13" if len(tokens) == 1 else "multiple"), tokens
    return "invalid", []


# ---------------------------------------------------------------------------
# 4.8 件名のまとめのキー
# ---------------------------------------------------------------------------


def group_key(title: str | None, remove_patterns: Iterable[str]) -> tuple[str, list[str]]:
    """件名を NFKC にし、空白を除き、決まった形を上から順に取り除く（4.8）。

    戻り値は (group_key, 取り除いた文字列の配列)。取り除いた部分は title_removed_parts に残す。
    """
    text = key_text(title)
    removed: list[str] = []
    for pattern in remove_patterns:
        regex = re.compile(pattern)
        removed.extend(m.group(0) for m in regex.finditer(text))
        text = regex.sub("", text)
    return text, removed
