"""検証（implementation_spec.md 9章の check.py）。

P0-4 では、V7 の数値・氏名・強い語の検査の関数だけを置く。record が Claude の書く文字列に対して使う
（4.5）。V1〜V8 の検証と check の操作は P0-6・P0-8 で足す。

- V7_RAW_NUMBER：NFKC にした後、照合済みの引用の中と、9章の表の対象外の形（{calc:ID}、ID、型、検出、
  品質検査、年度、日付、条文）を、この順に取り除いてから、残った数字を見る。
- V7_PERSONAL_NAME：氏名の一覧（呼び出し側が原本から作って渡す。保存しない）の、姓と名をつないだ全体で
  照らす。姓や名の1語だけでは照らさない。見つけた語そのものは返さない（位置だけ）。
- W7_STRONG_WORD：強い語は警告。例外語と照合済みの引用の中は対象外。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

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


def strong_words(text: str, quotes: Iterable[str] = ()) -> list[str]:
    """W7_STRONG_WORD：強い語（現れた順）。例外語と照合済みの引用の中は除く。"""
    rest = _without_quotes(text, quotes)
    for word in STRONG_WORD_EXCEPTIONS:
        rest = rest.replace(word, " ")
    found = [(rest.find(w), w) for w in STRONG_WORDS if w in rest]
    return [w for _, w in sorted(found)]


def v7_findings(
    texts: Mapping[str, str],
    *,
    names: Iterable[str] = (),
    quotes: Iterable[str] = (),
) -> tuple[list[dict], list[dict]]:
    """欄（名前 → 文字列）ごとの V7 の不合格と警告。

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
    return errors, warnings
