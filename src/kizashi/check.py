"""検証（implementation_spec.md 9章の check.py。4.6 の kizashi check）。

- V7 の数値・氏名・強い語の検査の関数（P0-4）。record が Claude の書く文字列に対して使う（4.5）。
- V1〜V8 の検証と check の操作（P0-6・P0-8）。不合格をすべて集めて返す。T6a・T6b の判定は check のたびに
  照合の計算から機械で決め、explanations に登録する（context='cli'）。check の時点の V7 は、台帳にある Claude の
  書いた文字列を record と同じ欄の分け方で見直す（氏名の一覧と取り込まない列の値は検査のたびに原本から作る）。
- 巡回数（4.6）：結論の案（保留を除く）を登録した後の check を1巡と数える。上限（MAX_ROUNDS。保留にしたことの
  ある候補は1巡を足す）に達した候補は、保留以外の結論の案を E_ROUNDS_EXCEEDED で拒否する（record と check）。

- V7_RAW_NUMBER：NFKC にした後、照合済みの引用の中と、9章の表の対象外の形（{calc:ID}、ID、型、検出、
  品質検査、年度、日付、条文）を、この順に取り除いてから、残った数字を見る。
- V7_PERSONAL_NAME：氏名の一覧（呼び出し側が原本から作って渡す。保存しない）の、姓と名をつないだ全体で
  照らす。姓や名の1語だけでは照らさない。取り込まない列の値（担当官の所在地と相手方の住所。D-032）も同じコードで
  照らす（`?` は D-022 の規則）。見つけた語そのものは返さない（位置だけ）。
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

import yaml

from kizashi import calc, hashing, independent_reader, ledger, limits, paths, quotes, versions
from kizashi.adapters import Adapter, load_adapter
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


WILDCARD_MIN_CHARS = (
    3  # `?` 以外の文字がこれより少ない値は、`?` を任意の1文字として照らさない（D-022）
)


def wildcard_pattern(form: str) -> str | None:
    """`?` を空白以外の任意の1文字（\\S）にした正規表現。`?` がないか、`?` 以外の文字が3文字未満なら None
    （元の表記どおりにだけ照らす。実装仕様12.1・12.2。D-022）。"""
    if "?" not in form or sum(1 for ch in form if ch != "?") < WILDCARD_MIN_CHARS:
        return None
    return "".join(r"\S" if ch == "?" else re.escape(ch) for ch in form)


class TermMatcher:
    """多くの語（氏名、取り込まない列の値、受注者名、法人番号）を文字列の中から探す。

    語は元の表記と NFKC にした表記の両方で照らす。wildcard が真なら、ASCII の `?` を含む語は D-022 の規則
    （空白以外の任意の1文字。`?` 以外が3文字未満なら元の表記どおり）で照らす。大きな文字列（公開の検査の blob）
    でも速く探せるよう、語の先頭の文字で候補の位置を絞り、先頭の数文字で語を引く。
    """

    PREFIX = 4

    def __init__(self, terms: Iterable[tuple[str, str]], *, wildcard: bool = True) -> None:
        self._literals: dict[int, dict[str, list[tuple[str, str]]]] = {}
        self._regexes: list[tuple[re.Pattern[str], str]] = []
        firsts: set[str] = set()
        for value, key in terms:
            for form in dict.fromkeys([value, nfkc(value)]):
                if not form.strip():
                    continue
                pattern = wildcard_pattern(form) if wildcard else None
                if pattern is not None:
                    self._regexes.append((re.compile(pattern), key))
                    continue
                size = min(len(form), self.PREFIX)
                self._literals.setdefault(size, {}).setdefault(form[:size], []).append((form, key))
                firsts.add(form[0])
        self._first = (
            re.compile("[" + "".join(re.escape(c) for c in sorted(firsts)) + "]")
            if firsts
            else None
        )
        self._sizes = sorted(self._literals)

    def __bool__(self) -> bool:
        return bool(self._literals or self._regexes)

    def find(self, text: str) -> list[tuple[int, int, str]]:
        """当たった (始め, 終わり, key) を、始めの順・長い順に返す（重なりを含む）。"""
        hits: set[tuple[int, int, str]] = set()
        if self._first is not None:
            for match in self._first.finditer(text):
                i = match.start()
                for size in self._sizes:
                    for form, key in self._literals[size].get(text[i : i + size], ()):
                        if text.startswith(form, i):
                            hits.add((i, i + len(form), key))
        for pattern, key in self._regexes:
            start = 0
            while (match := pattern.search(text, start)) is not None:
                hits.add((match.start(), match.end(), key))
                start = match.start() + 1
        return sorted(hits, key=lambda h: (h[0], h[0] - h[1], h[2]))

    def find_folded(self, text: str) -> list[tuple[int, int, str]]:
        """元の文字列と、1文字ずつ NFKC にした文字列（全角の数字・英字など）の両方で探し、元の文字列の位置で返す。"""
        hits = set(self.find(text))
        parts: list[str] = []
        origin: list[int] = []
        for i, ch in enumerate(text):
            folded = nfkc(ch)
            parts.append(folded)
            origin.extend([i] * len(folded))
        folded_text = "".join(parts)
        if folded_text != text:
            for start, end, key in self.find(folded_text):
                hits.add((origin[start], origin[end - 1] + 1, key))
        return sorted(hits, key=lambda h: (h[0], h[0] - h[1], h[2]))

    def spans(self, text: str) -> list[tuple[int, int, str]]:
        """重ならない当たり（元の文字列の位置。左から、同じ位置では長いものを採る）。"""
        result: list[tuple[int, int, str]] = []
        for hit in self.find_folded(text):
            if not result or hit[0] >= result[-1][1]:
                result.append(hit)
        return result


NAME_MASK = "（氏名）"  # 追加資料のページ別テキストで、担当官の氏名の代わりに書く文字列（D-062）


def officer_name_matcher(
    conn: sqlite3.Connection, root: Path, adapter: Adapter, stage: str
) -> TermMatcher:
    """実行の担当官の氏名（V7 と同じ、姓名をつないだ3通り）を探す。氏名の一覧を作る原本の CSV が data/raw に
    なければ、伏せられないまま書かないよう E_V7 で止める（D-062）。様式が違えば E_FORMAT_CHANGED（D-059）。"""
    if fetched_csv_count(conn) == 0:
        # 取得済みの CSV が1本もなければ、氏名の一覧を作れず伏せられない（「氏名が0件」と区別する。D-092②）
        raise KizashiError(
            "E_V7",
            stage,
            "氏名の一覧を作る原本（公表一覧の CSV）を1本も取得していないため、ページ別テキストとリンクの文の"
            "氏名を伏せられない（取得しない）。",
            next_action="公表一覧の CSV を取得できる状態で、新しい実行を作る。",
        )
    missing = _unreadable_csv(conn, root)
    if missing:
        raise KizashiError(
            "E_V7",
            stage,
            f"氏名の一覧を作る原本（{'・'.join(missing)}）が data/raw にないため、ページ別テキストの氏名を"
            "伏せられない。",
            affected_ids=missing,
            next_action="原本の CSV がそろった状態で取得し直す。",
        )
    names = name_variants(adapter.module.name_list(conn, root, adapter))
    return TermMatcher([(n, "name") for n in names], wildcard=False)


def mask_names(text: str, matcher: TermMatcher) -> tuple[str, int]:
    """文の中の氏名を NAME_MASK に替える。(替えた文, 替えた数)。"""
    out: list[str] = []
    last = 0
    count = 0
    for start, end, _ in matcher.spans(text):
        out.append(text[last:start])
        out.append(NAME_MASK)
        last = end
        count += 1
    out.append(text[last:])
    return "".join(out), count


def excluded_matcher(values: Iterable[str]) -> TermMatcher:
    """取り込まない列の値（D-032）を照らす TermMatcher（`?` は D-022 の規則）。"""
    return TermMatcher(((v, "excluded_value") for v in values), wildcard=True)


EXCLUDED_NOTE = "（取り込まない列の値）"


@dataclass(frozen=True)
class SensitiveTerms:
    """V7_PERSONAL_NAME で照らすもの：氏名の照らす形と、取り込まない列の値（9章。D-032）。"""

    names: list[str]
    excluded: TermMatcher

    def hits(self, fields: Mapping[str, str]) -> list[str]:
        """欄ごとの当たりを「V7_PERSONAL_NAME：<欄> の <始め>〜<終わり> 文字目」の形で返す。語は書かない
        （D-037・D-040②）。取り込まない列の値の当たりには EXCLUDED_NOTE を添える（D-090①）。"""
        found = name_hits(fields, self.names)
        for field, text in fields.items():
            for start, end, _ in self.excluded.spans(text):
                found.append(
                    f"V7_PERSONAL_NAME：{field} の {start + 1}〜{end} 文字目{EXCLUDED_NOTE}"
                )
        return found


def fetched_csv_count(conn: sqlite3.Connection) -> int:
    """実行が取得した公表一覧の CSV の数（氏名の一覧と取り込まない列の値を作る原本）。"""
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE kind = 'dataset' AND status = 'fetched'"
        " AND logical_key LIKE '%|csv'"
    ).fetchone()
    return int(count)


def sensitive_terms(
    conn: sqlite3.Connection, root: Path, adapter: Adapter, stage: str
) -> SensitiveTerms:
    """氏名の一覧と取り込まない列の値を、実行の原本から作る（検査のたびに作り、保存しない。9章の V7）。

    取得済みの CSV が1本もない（原本がない）ときと、原本が data/raw にないときは、一覧を作れないので E_V7 に
    する（検査を飛ばして通さない。D-090②・1-6）。原本を検査した結果、氏名が0件なら空の一覧を返す（原本が
    ないことと区別する）。原本の様式が期待と違えば E_FORMAT_CHANGED（D-059）。"""
    if fetched_csv_count(conn) == 0:
        raise KizashiError(
            "E_V7",
            stage,
            "氏名の一覧と取り込まない列の値を作る原本（公表一覧の CSV）を1本も取得していないため、氏名の検査が"
            "できない（検査を飛ばして通さない）。",
            next_action="公表一覧の CSV を取得できる状態で、新しい実行を作る。",
        )
    missing = _unreadable_csv(conn, root)
    if missing:
        raise KizashiError(
            "E_V7",
            stage,
            f"氏名の一覧と取り込まない列の値を作る原本（{'・'.join(missing)}）が data/raw にないため、"
            "氏名の検査ができない（検査を飛ばして通さない）。",
            affected_ids=missing,
            next_action="data/raw の原本を元に戻すか、新しい実行を作る。",
        )
    names = name_variants(adapter.module.name_list(conn, root, adapter))
    excluded = excluded_matcher(adapter.module.excluded_values(conn, root, adapter))
    return SensitiveTerms(names, excluded)


def v7_hit_text(error: dict) -> str:
    """V7 の不合格の1件の説明（氏名・取り込まない列の値は位置だけで、語を書かない）。"""
    if error["code"] == "V7_PERSONAL_NAME":
        what = "（取り込まない列の値）" if error.get("what") == "excluded_value" else ""
        return f"{error['code']}：{error['field']} の {error['at'][0] + 1}〜{error['at'][1]} 文字目{what}"
    return f"{error['code']}：{error['field']} に「{error['hit']}」"


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
    excluded: TermMatcher | None = None,
) -> tuple[list[dict], list[dict]]:
    """欄（名前 → 文字列）ごとの V7 の不合格と警告。

    texts の欄には、数値・氏名・強い語のすべてを当てる。names_only の欄（数字を書いてよい欄や引用。
    比較の条件、単位、根拠の引用、条件の値など）には氏名の検査だけを、words_only の欄（比較の条件）には
    強い語の警告だけを当てる（D-035・D-036）。
    excluded（取り込まない列の値。D-032）は、氏名と同じ欄に当てる（D-035）。
    戻り値の不合格は {"code", "field", "hit"}（数値）か {"code", "field", "at", "what"}（氏名と取り込まない列の値。
    語は返さない。what は name か excluded_value）。警告は {"code": "W7_STRONG_WORD", "field", "hit"}。
    """
    names = list(names)
    quotes = list(quotes)
    errors: list[dict] = []
    warnings: list[dict] = []

    def sensitive(field: str, text: str) -> None:
        for start, end in personal_names(text, names):
            errors.append(
                {"code": "V7_PERSONAL_NAME", "field": field, "at": [start, end], "what": "name"}
            )
        if excluded:
            for start, end, _ in excluded.spans(text):
                errors.append(
                    {
                        "code": "V7_PERSONAL_NAME",
                        "field": field,
                        "at": [start, end],
                        "what": "excluded_value",
                    }
                )

    for field, text in texts.items():
        if not text:
            continue
        for hit in raw_numbers(text, quotes):
            errors.append({"code": "V7_RAW_NUMBER", "field": field, "hit": hit})
        sensitive(field, text)
        for word in strong_words(text, quotes):
            warnings.append({"code": "W7_STRONG_WORD", "field": field, "hit": word})
    for field, text in (names_only or {}).items():
        sensitive(field, text)
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
# 9章の表の順（3.2 の failures の並び。D-041①）
FAILURE_ORDER = (
    "V1_INPUT_EMPTY",
    "V1_QUALITY_NOT_RUN",
    "V1_RUN_FAILED",
    "V2_REF_MISSING",
    "V2_HASH_MISMATCH",
    "V2_STALE_OR_WITHDRAWN",
    "V2_BAD_EVIDENCE_SOURCE",
    "V3A_RECALC_MISMATCH",
    "V3B_REAGG_MISMATCH",
    "V3B_HEADER_UNRESOLVED",
    "V3D_DOC_VALUE_MISMATCH",
    "V3C_DERIVE_MISMATCH",
    "V3_UNCITED_NUMBER",
    "V3_NO_MEANING",
    "V4_QUOTE_NOT_FOUND",
    "V4_QUOTE_TRUNCATED",
    "V4_QUOTE_YEAR",
    "V4_PAGE_TEXT_MISMATCH",
    "V5_CONDITION_MISSING",
    "V5_UNRESOLVED_COMPARISON",
    "V5_ADJUSTED_NO_VALUES",
    "V5_BAD_RATE",
    "V5_CHANGE_NOT_COMPARABLE",
    "V5_PARTIAL_YEAR",
    "V6_TYPE_MISSING",
    "V6_NO_EVIDENCE",
    "V6_SELF_EVIDENCE",
    "V6_NO_SEARCH_SCOPE",
    "V7_RAW_NUMBER",
    "V7_PERSONAL_NAME",
    "V8_REQUIRED_MISSING",
    "V8_MATCH_UNCONFIRMED",
    "V8_COEFFICIENT_PENDING",
    "V8_CHANGE_NOT_NEGATED",
    "V8_ARTIFACT_NO_FACTOR",
    "V8_EXPLANATION_STATUS",
    "V8_NO_WITHDRAW_RESULT",
    "V8_HOLD_REASON",
    "V8_T6C_QUESTION",
)
_FAILURE_RANK = {code: i for i, code in enumerate(FAILURE_ORDER)}
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
    warnings: list[dict] = dc_field(default_factory=list)

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
    """規則の版を照らす（3.5）。違えば E_RULE_VERSION（終了コード3）。

    dataset.yaml がない・読めないときも、規則の版を照らせないので E_RULE_VERSION にする（D-091①）。"""
    row = conn.execute("SELECT domain, rule_versions_json FROM runs").fetchone()
    if row is None:
        # 台帳を作った直後に止まった実行など。想定外のエラーにせず、実行の記録がないとする（record・inspect と同じ）
        raise KizashiError(
            "E_REF_NOT_FOUND",
            stage,
            f"実行の記録（runs の行）がない：{run_id}",
            affected_ids=[run_id],
            next_action="実行IDを確かめる。台帳を作った直後に止まった実行なら、新しい実行を作る。",
        )
    domain, recorded = row
    yaml_path = paths.dataset_dir(root, domain) / "dataset.yaml"
    try:
        if yaml_path.is_symlink() or not yaml_path.is_file():
            raise FileNotFoundError(yaml_path)
        drift = versions.drift(json.loads(recorded), root, domain)
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        # ない・読めない・YAML として壊れている・parser_version がない dataset.yaml は、規則の版を照らせない
        # （D-091①・D-095④）
        raise KizashiError(
            "E_RULE_VERSION",
            stage,
            f"分野の規則のファイル datasets/{domain}/dataset.yaml がない、または読めないので、規則の版を"
            "照らせない。",
            affected_ids=[run_id],
            next_action=f"datasets/{domain}/dataset.yaml を元に戻す（git の作業フォルダーを確かめる）。"
            "リポジトリの直下で実行しているかも確かめる。",
        ) from None
    if drift:
        raise rule_version_error(run_id, drift, stage)


def _one(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    rows = dict_rows(conn, sql, params)
    return rows[0] if rows else None


def candidate_rows(conn: sqlite3.Connection, lead_id: str) -> list[str]:
    """候補の行（5.5・9章）：メモに出る基礎集計の row_refs のうち、率（ratio）の分母にしか使わない計算の行を
    除いたもの（D-090③）。

    除くのは、メモに直接出ず、メモに出る派生計算での使い方がすべて ratio の第2引数（分母）である基礎集計だけ。
    派生計算を通じて別の用途（diff・rate・sum・product の引数、ratio の第1引数）にも使う計算と、rate の基準の
    行は残す。除いた行も、依存する資料の検査と検証記録のハッシュには含める（hashing.memo_calc_ids による）。"""
    direct = set(hashing.direct_calc_ids(conn, lead_id))
    comps = {
        c: row
        for c in hashing.memo_calc_ids(conn, lead_id)
        if (row := _one(conn, "SELECT * FROM computations WHERE calc_id = ?", (c,))) is not None
    }
    uses: dict[str, list[tuple[object, int]]] = {}
    for row in comps.values():
        if row["kind"] == "derived":
            request = json.loads(row["request_json"])
            for position, arg in enumerate(request.get("args") or []):
                if isinstance(arg, str):
                    uses.setdefault(arg, []).append((request.get("op"), position))

    def denominator_only(calc_id: str) -> bool:
        found = uses.get(calc_id, [])
        return (
            calc_id not in direct
            and bool(found)
            and all(op == "ratio" and position == 1 for op, position in found)
        )

    refs: set[str] = set()
    for calc_id, row in comps.items():
        if row["kind"] != "base" or row["state"] != "active":
            continue
        if denominator_only(calc_id):
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


def _cites_columns(items: object, columns: set[str]) -> bool:
    return isinstance(items, list) and any(
        isinstance(e, dict)
        and isinstance(e.get("position"), dict)
        and e["position"].get("column") in columns
        for e in items
    )


def counterparty_cited(conn: sqlite3.Connection, adapter: Adapter, lead_id: str) -> bool:
    """受注先を扱う主張があるか（アダプタ仕様7.4。D-058・D-065）。メモに出る記載の根拠（主張・判定・対応・係数の
    手がかり・文書値）の位置の列が受注先の列（reconcile.fields_counterparty の take のキー）か、メモに出る基礎集計の
    条件に受注先の列（分野アダプタの COUNTERPARTY_FILTER_COLUMNS）がある場合。主張の文の意味は見ない（機械で見分けられる
    範囲。文で受注先を述べる主張は review-lead と記者が確かめる）。"""
    cfg = adapter.config.get("reconcile") or {}
    pdf_fields = cfg.get("pdf_fields") or {}
    columns = {pdf_fields.get(f, f) for f in cfg.get("fields_counterparty") or []}
    filters = set(getattr(adapter.module, "COUNTERPARTY_FILTER_COLUMNS", ()))
    sources = (
        "SELECT evidence_json FROM claims WHERE lead_id = ? AND state <> 'withdrawn'",
        "SELECT evidence_json FROM explanations WHERE lead_id = ? AND context <> 'cli'",
        "SELECT evidence_json FROM procurement_matches WHERE lead_id = ? AND status <> 'withdrawn'",
    )
    for sql in sources:
        for (evidence,) in conn.execute(sql, (lead_id,)):
            if _cites_columns(json.loads(evidence or "[]"), columns):
                return True
    (coefficients,) = conn.execute(
        "SELECT coefficients_json FROM leads WHERE lead_id = ?", (lead_id,)
    ).fetchone()
    for coef in json.loads(coefficients or "[]"):
        if isinstance(coef, dict) and _cites_columns(coef.get("basis"), columns):
            return True
    for calc_id in hashing.memo_calc_ids(conn, lead_id):
        row = conn.execute(
            "SELECT kind, request_json FROM computations WHERE calc_id = ?", (calc_id,)
        ).fetchone()
        if row is None:
            continue
        request = json.loads(row[1])
        if row[0] == "doc_value" and _cites_columns([request], columns):
            return True  # 文書値の位置が受注先の列を引く（D-065）
        if row[0] != "base":
            continue
        used = {f.get("col") for f in request.get("filter") or [] if isinstance(f, dict)}
        if used & filters:
            return True
    return False


def t6b_fields(conn: sqlite3.Connection, adapter: Adapter, lead: Mapping) -> list[str]:
    """T6b とメモの7節で照らす項目：区分の照合項目に、受注先を扱う主張があれば受注者名と法人番号を加える
    （アダプタ仕様7.4。D-058）。"""
    cfg = adapter.config.get("reconcile") or {}
    fields = list((cfg.get("fields_by_category") or {}).get(lead["change_category"]) or [])
    if counterparty_cited(conn, adapter, lead["lead_id"]):
        fields += [f for f in cfg.get("fields_counterparty") or [] if f not in fields]
    return fields


def _t6b(
    conn: sqlite3.Connection, adapter: Adapter, rows: list[str], fields: list[str]
) -> tuple[str, list[str], str, list[str]]:
    """T6b（公表形式間・原本との食い違い）。戻り値の最後は照合できなかった行（確認リストの先頭に出す）。
    fields は t6b_fields の照合項目。"""
    cfg = adapter.config.get("reconcile") or {}
    if cfg.get("source_check") != "pdf":
        return "na", [], "分野アダプタが原本照合を対象外とした（ほかの形式がない）。", []
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
        " searched = excluded.searched, recalc_calc_id = NULL, residual_calc_id = NULL,"
        " reflected_conditions_json = NULL, scope = NULL,"
        " no_quant_reason = NULL, context = 'cli', updated_at = excluded.updated_at"
        " WHERE explanations.judgment IS NOT excluded.judgment"
        " OR explanations.evidence_json IS NOT excluded.evidence_json"
        " OR explanations.searched IS NOT excluded.searched OR explanations.context IS NOT 'cli'"
        " OR explanations.recalc_calc_id IS NOT NULL OR explanations.residual_calc_id IS NOT NULL"
        " OR explanations.reflected_conditions_json IS NOT NULL"
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
    conn: sqlite3.Connection, adapter: Adapter, lead_id: str
) -> list[tuple[int, str]]:
    """候補が依存する資料（年度, 方式）。hashing.required_sources と同じ（検証記録のハッシュと共有する）。"""
    return hashing.required_sources(conn, adapter, lead_id)


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
    for year, family in required_sources(conn, adapter, lead["lead_id"]):
        key = adapter.logical_key(year, family, "csv")
        source = _one(
            conn,
            "SELECT source_id, status FROM sources WHERE logical_key = ? AND kind = 'dataset'"
            " AND supersedes IS NULL",
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
    adapter: Adapter,
    lead: dict,
    claims: list[dict],
    matches: list[dict],
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
    sources = hashing.dependency_sources(conn, lead_id, adapter)
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
        newer = hashing.newer_versions(conn, source_id)
        if newer:
            latest = newer[-1]["source_id"]
            if source["kind"] == "dataset":
                todo = (
                    "この実行の正規化と計算は古い版による（新しい版は正規化しない）。新しい版で調べ直すには、"
                    "同じ設定で新しい実行を作る"
                )
            else:
                todo = (
                    f"新しい版 {latest} を読み直し、古い版を根拠にした判定・対応・係数は新しい版を根拠に登録し"
                    "直し、主張は取り下げて登録し直す"
                )
            v.fail(
                "V2_STALE_OR_WITHDRAWN",
                f"検証が依存する資料 {source_id} に、中身の違う新しい版 {latest} がある"
                f"（fetch --recheck）。{todo}。",
                [source_id, *(n["source_id"] for n in newer)],
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
        elif comp["kind"] == "doc_value":
            # 文書値の値を、依頼に残した引用の中の数値と照らし直す（引用が資料にあるかは V4。D-060）
            try:
                calc.check_doc_value(str(request.get("quote") or ""), comp["value"])
            except KizashiError as exc:
                v.fail(
                    "V3D_DOC_VALUE_MISMATCH",
                    f"文書値 {calc_id} の値が、引用の中の数値と合わない：{exc.message}",
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
        if match["status"] == "withdrawn":  # 取り下げた対応の根拠は照らさない（D-072）
            continue
        for e in json.loads(match["evidence_json"]):
            targets.append((match["match_id"], e, {}))
    for calc_id in hashing.memo_calc_ids(conn, lead["lead_id"]):
        comp = _one(
            conn, "SELECT kind, request_json FROM computations WHERE calc_id = ?", (calc_id,)
        )
        if comp is not None and comp["kind"] == "doc_value":
            targets.append((calc_id, {**json.loads(comp["request_json"]), "role": "support"}, {}))
    # 係数の提案の手がかりのうち、取得した資料の根拠（メモの4節・7節に出る。状態にかかわらず。D-064）。
    # 知識ファイルの手がかり（file のあるもの）は record で照らしている（references/ は書き換えない）
    for coef in json.loads(lead["coefficients_json"] or "[]"):
        if not isinstance(coef, dict):
            continue
        for item in coef.get("basis") or []:
            if isinstance(item, dict) and "file" not in item:
                targets.append((f"{lead['lead_id']}・係数 {coef.get('key')}", item, {}))
    for owner, evidence, kw in targets:
        try:
            quotes.verify_quote(conn, root, run_id, adapter, evidence, **kw)
        except KizashiError as exc:
            code = _QUOTE_CODES.get(exc.code, "V2_REF_MISSING")
            v.fail(code, f"{owner} の根拠：{exc.message}", [owner, *exc.affected_ids])
    page_sources = {
        e["source_id"]
        for _, e, _ in targets
        if isinstance(e, dict)
        and isinstance(e.get("source_id"), str)
        and isinstance(e.get("position"), dict)
        and "page" in e["position"]
    }
    _v4_page_texts(conn, root, run_id, adapter, sorted(page_sources), v)


def _stored_page(path: Path, matcher: TermMatcher) -> str | None:
    """保存したページ別テキストを、いまの氏名の一覧で伏せて読む。UTF-8 として読めなければ None（抽出し直した
    テキストと違うものとして扱う。D-066）。"""
    try:
        return mask_names(path.read_text(encoding="utf-8"), matcher)[0]
    except UnicodeDecodeError:
        return None


def _v4_page_texts(
    conn: sqlite3.Connection,
    root: Path,
    run_id: str,
    adapter: Adapter,
    source_ids: list[str],
    v: Verification,
) -> None:
    """引用したページ別テキストを、原本（SHA-256 が台帳と同じもの）から抽出し直したテキストと比べる（D-061）。
    どちらも、いまの担当官の氏名の一覧で伏せてから比べる（取得の後に一覧が増えても、氏名の違いだけで不合格に
    しない。D-062）。原本がない・違う場合は V2_HASH_MISMATCH に任せる。氏名の一覧を作れなければ V7 が不合格に
    するので、ここでは比べない。"""
    from kizashi import fetcher  # 取得の処理は check を使うので、ここで読み込む

    if not source_ids:
        return
    try:
        matcher = officer_name_matcher(conn, root, adapter, CHECK_STAGE)
    except KizashiError:
        return
    for source_id in source_ids:
        source = _one(conn, "SELECT * FROM sources WHERE source_id = ?", (source_id,))
        if source is None or source["kind"] == "dataset":
            continue
        path = raw_path(root, source)
        if path is None or hashing.sha256_file(path) != source["sha256"]:
            continue
        fmt = "pdf" if path.suffix.lower() == ".pdf" else "html"
        try:
            pages, _, _ = fetcher.extract_page_texts(
                path, fmt, source["final_url"] or source["url"]
            )
        except Exception as exc:  # noqa: BLE001  PDF のライブラリの例外をまとめて扱う
            v.fail(
                "V4_PAGE_TEXT_MISMATCH",
                f"資料 {source_id} の原本からページ別テキストを抽出し直せない（{type(exc).__name__}）。",
                [source_id],
            )
            continue
        expected = [mask_names(p, matcher)[0] for p in pages]
        page_dir = paths.run_dir(root, run_id) / "evidence" / source_id / "pages"
        names = [str(n) for n in range(1, len(expected) + 1)]
        files = sorted(page_dir.glob("*.txt")) if page_dir.is_dir() else []
        stored_names = sorted((f.stem for f in files), key=lambda x: (len(x), x))
        if stored_names != names:
            v.fail(
                "V4_PAGE_TEXT_MISMATCH",
                f"資料 {source_id} のページ別テキストのページ数が、原本から抽出し直した数"
                f"（{len(expected)}ページ）と違う。",
                [source_id],
            )
            continue
        differing = [
            n
            for n, want in zip(names, expected, strict=True)
            if _stored_page(page_dir / f"{n}.txt", matcher) != want
        ]
        if differing:
            v.fail(
                "V4_PAGE_TEXT_MISMATCH",
                f"資料 {source_id} のページ別テキスト（{'・'.join(differing)}ページ）が、原本から抽出し直した"
                "テキストと違う。取得の後に書き換えたテキストは根拠にできない。",
                [source_id],
            )


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
        _v8_explanation_status(conn, adapter, lead, judged, questions, v)
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
    # 残りの件数を出す判定ごとに、空（NULL）の行を残りから外していないかを確かめる。結論の種類と、別の説明の合格に
    # かかわらず行う（D-048）
    _v8_null_exclusion(conn, adapter, lead, judged, v)
    if missing:
        v.fail("V8_REQUIRED_MISSING", "結論の必須の中身がない：" + "、".join(missing), [lead_id])


# 列が空（NULL）の行に当たらない演算（SQL の意味で、空の値はこれらの条件を満たさない）。is_null は空の行を残す
# 使い方なので含めない（D-048）
NULL_EXCLUDING_OPS = ("=", "!=", "in", "not_null")


def null_excluded_rows(
    conn: sqlite3.Connection,
    adapter: Adapter,
    lead: dict,
    residual: dict,
    reflected: object,
) -> list[tuple[str, str, str]]:
    """残りの件数（基礎集計）が、列が空の値で候補の行を残りから外していれば、(row_ref, 列, 演算) を返す。

    検査元は、残りの件数が参照する年度（条件の fiscal_year。限っていなければ比べる2年度）の候補の行
    （calc_from・calc_to の row_refs）。そのうち残りに入らなかった行で、=・!=・in・not_null の条件の列が空のものを、
    空の値で外れた行とする（D-048）。ただし、その判定の reflected_conditions に宣言し、説明用として認める条件
    （候補を選び直すだけの列でないもの）で、空でない値によって外れる行は含めない。宣言していない条件や、
    method_family などの選び直すだけの列の条件では免除しない（D-049）。
    """
    request = json.loads(residual["request_json"])
    table = request.get("table")
    filters = [f for f in request.get("filter") or [] if isinstance(f, dict)]
    allowed = getattr(adapter.module, "CALC_TABLES", {}).get(table, {}).get("filter", ())
    sensitive = [
        i
        for i, f in enumerate(filters)
        if f.get("op") in NULL_EXCLUDING_OPS and f.get("col") in allowed
    ]
    if not sensitive:
        return []
    periods = {int(lead["period_from"]): lead["calc_from"], int(lead["period_to"]): lead["calc_to"]}
    limited, years = hashing.filter_values(filters, "fiscal_year")
    sources = [periods[y] for y in years if y in periods] if limited else list(periods.values())
    rows: set[str] = set()
    for calc_id in sources:
        rows |= set(json.loads((_comp(conn, calc_id) or {}).get("row_refs_json") or "[]"))
    rows -= set(json.loads(residual["row_refs_json"] or "[]"))
    if not rows:
        return []
    ordered = sorted(rows)
    declared = normalized_conditions(reflected)
    # 免除の根拠にできる条件：宣言した説明用の条件（D-049）
    exempting = [
        i
        for i, f in enumerate(filters)
        if f.get("col") in allowed
        and f.get("col") not in IDENTITY_COLUMNS
        and normalized_condition(f) in declared
    ]
    columns = sorted({filters[i]["col"] for i in (*sensitive, *exempting)})
    marks = ", ".join("?" for _ in ordered)
    # 表と列は分野の許可リスト（CALC_TABLES）にあるものだけなので、SQL に埋め込んでよい
    empty = {
        r[0]: {col for col, value in zip(columns, r[1:], strict=True) if value is None}
        for r in conn.execute(
            f"SELECT record_id, {', '.join(columns)} FROM {table} WHERE record_id IN ({marks})",
            ordered,
        )
    }
    satisfied = {}  # 免除の根拠にできる条件ごとに、それを満たす行（SQL の意味で。adopted は calc が加える）
    for i in exempting:
        result = calc.base_aggregate(
            conn,
            adapter,
            {
                "op": "count",
                "table": table,
                "filter": [filters[i], {"col": "record_id", "op": "in", "value": ordered}],
            },
            allow_empty_in=True,
        )
        satisfied[i] = set(result.row_refs)
    found = []
    for row in ordered:
        nulls = empty.get(row, set())
        by_null = [i for i in sensitive if filters[i]["col"] in nulls]
        exempted = any(
            filters[i]["col"] not in nulls and row not in satisfied[i] for i in exempting
        )
        if by_null and not exempted:
            found += [(row, filters[i]["col"], filters[i]["op"]) for i in by_null]
    return found


def _v8_null_exclusion(
    conn: sqlite3.Connection, adapter: Adapter, lead: dict, judged: dict, v: Verification
) -> None:
    """メモの4節に残りの件数（基礎集計の残る差）を出す判定ごとに、空の値で外れた候補の行がないかを確かめる（D-048）。"""
    for etype, exp in sorted(judged.items()):
        residual_id = exp.get("residual_calc_id")
        residual = _comp(conn, residual_id)
        if residual is None or residual["kind"] != "base":
            continue
        reflected = json.loads(exp.get("reflected_conditions_json") or "null")
        found = null_excluded_rows(conn, adapter, lead, residual, reflected)
        if not found:
            continue
        rows = sorted({r for r, _, _ in found})
        columns = "・".join(sorted({c for _, c, _ in found}))
        ops = "・".join(sorted({o for _, _, o in found}))
        v.fail(
            "V8_EXPLANATION_STATUS",
            f"判定 {etype} の残る差（{residual_id}）で、候補の行のうち列 {columns} が空の行（{'・'.join(rows)}）が、"
            f"条件（{ops}）で残りから外れている。空の値は、根拠で説明できる行として除けない（reflected_conditions に"
            "宣言した説明用の条件で、空でない値によって外れる行だけを除ける。D-049）。その列で残りを選ばないか"
            "（空の行を残すなら is_null）、定量化できなければ残りの件数を使わず、scope を unknown にして no_quant_reason と"
            "理由・内訳を尋ねる取材質問（purposes に reason_breakdown）で扱う（D-048）。",
            [lead["lead_id"], residual_id, *rows],
        )


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


def _comp(conn: sqlite3.Connection, calc_id: str | None) -> dict | None:
    if not calc_id:
        return None
    return _one(conn, "SELECT * FROM computations WHERE calc_id = ?", (calc_id,))


# 候補を選び直すだけの列（説明によって足した条件にならない。集計の条件には使える。D-043②・D-044②）
IDENTITY_COLUMNS = frozenset({"fiscal_year", "record_id", "group_key", "method_family"})


def normalized_condition(item: object) -> str | None:
    """条件1つの単純な同値の表現をそろえた形（in の値を並べて重複を除き、値が1つの in を = にする）。"""
    if not isinstance(item, dict):
        return None
    op, value = item.get("op"), item.get("value")
    if op == "in" and isinstance(value, list):
        values = sorted(set(map(canonical_json, value)))
        if len(values) == 1:
            return canonical_json(
                {"col": item.get("col"), "op": "=", "value": json.loads(values[0])}
            )
        return canonical_json(
            {"col": item.get("col"), "op": "in", "value": [json.loads(v) for v in values]}
        )
    return canonical_json({k: item[k] for k in ("col", "op", "value") if k in item})


def normalized_conditions(filters: object) -> set[str]:
    """条件の集まり（重複した条件は1つ）。"""
    found = {normalized_condition(f) for f in (filters if isinstance(filters, list) else [])}
    return {f for f in found if f is not None}


def same_aggregation(conn: sqlite3.Connection, a_id: str, b_id: str) -> bool:
    """2つの計算が同じ集計か（値ではなく、基礎集計は表・演算・列・対象行、派生計算は演算と引数、文書値は資料・
    位置・引用で判定する。別のIDで作り直した同じ集計を見分けるため。D-042③）。"""
    a, b = _comp(conn, a_id), _comp(conn, b_id)
    if a is None or b is None or a["kind"] != b["kind"]:
        return False
    ra, rb = json.loads(a["request_json"]), json.loads(b["request_json"])
    if a["kind"] == "base":
        keys = ("table", "op", "column")
        return tuple(ra.get(k) for k in keys) == tuple(rb.get(k) for k in keys) and sorted(
            json.loads(a["row_refs_json"] or "[]")
        ) == sorted(json.loads(b["row_refs_json"] or "[]"))
    if a["kind"] == "derived":
        return ra.get("op") == rb.get("op") and ra.get("args") == rb.get("args")
    if a["kind"] == "doc_value":
        keys = ("source_id", "position", "quote")
        return all(canonical_json(ra.get(k)) == canonical_json(rb.get(k)) for k in keys)
    return False


def is_amount_lead(conn: sqlite3.Connection, lead: dict) -> bool:
    """金額の候補か（metric が `<列>_sum`、または calc_from が合計）。"""
    if (lead.get("metric") or "").endswith("_sum"):
        return True
    comp = _comp(conn, lead["calc_from"])
    return comp is not None and json.loads(comp["request_json"]).get("op") == "sum"


def residual_comparison(conn: sqlite3.Connection, lead: dict, exp: dict) -> str | None:
    """金額の候補の残る差（diff）の比較対象（calc_from か calc_to）。残る差が規則に合わなければ None。"""
    recalc, residual = (
        _comp(conn, exp.get("recalc_calc_id")),
        _comp(conn, exp.get("residual_calc_id")),
    )
    if recalc is None or residual is None or residual["kind"] != "derived":
        return None
    request = json.loads(residual["request_json"])
    args = request.get("args") or []
    if request.get("op") != "diff" or len(args) != 2 or recalc["calc_id"] not in args:
        return None
    (other,) = [a for a in args if a != recalc["calc_id"]] or [None]
    if other not in (lead["calc_from"], lead["calc_to"]):
        return None
    compared = _comp(conn, other)
    units = {recalc["unit"], residual["unit"], (compared or {}).get("unit")}
    return other if len(units) == 1 and None not in units and "" not in units else None


def _residual_ok(conn: sqlite3.Connection, adapter: Adapter, lead: dict, exp: dict) -> bool:
    """説明を反映した値と残る差が、実装仕様9章の「説明を反映した値と残る差」を満たすか（D-042③）。"""
    recalc_id, residual_id = exp.get("recalc_calc_id"), exp.get("residual_calc_id")
    if not recalc_id or not residual_id or not json.loads(exp.get("evidence_json") or "[]"):
        return False  # 説明を反映した条件の根拠がない
    originals = [lead["calc_from"], lead["calc_to"], *json.loads(lead["calc_ids_json"])]
    if is_amount_lead(conn, lead):
        if any(recalc_id == o or same_aggregation(conn, recalc_id, o) for o in originals):
            return False
        return residual_comparison(conn, lead, exp) is not None
    # 金額以外の候補：候補の行から根拠で説明できる行を除いた残りの件数を、説明を反映した値と残る差の両方にする
    # （D-043①・D-046）。check は形だけを確かめ、0件でも説明の状況を決めない（根拠との対応は記者が確かめる）
    if recalc_id != residual_id:
        return False
    residual = _comp(conn, residual_id)
    if residual is None or residual["kind"] != "base":
        return False
    request = json.loads(residual["request_json"])
    if request.get("op") != "count":
        return False
    limited, years = hashing.filter_values(request.get("filter") or [], "fiscal_year")
    periods = {int(lead["period_from"]): lead["calc_from"], int(lead["period_to"]): lead["calc_to"]}
    if not limited or len(years) != 1 or years[0] not in periods:
        return False
    base = _comp(conn, periods[years[0]])
    if base is None:
        return False
    base_request = json.loads(base["request_json"])
    rows = set(json.loads(residual["row_refs_json"] or "[]"))
    if not (
        request.get("table") == base_request.get("table")
        and residual["unit"] == base["unit"]
        and rows <= set(json.loads(base["row_refs_json"] or "[]"))
    ):
        return False
    # 説明によって足した・変えた条件（D-043②）：残る件数の条件にあり、候補の計算の条件になく、選び直すだけの列でない
    reflected = json.loads(exp.get("reflected_conditions_json") or "null")
    if not isinstance(reflected, list) or not reflected:
        return False
    mine = normalized_conditions(request.get("filter"))
    theirs = normalized_conditions(base_request.get("filter"))
    for item in reflected:
        norm = normalized_condition(item)
        if (
            norm is None
            or not isinstance(item, dict)
            or item.get("col") in IDENTITY_COLUMNS
            or norm not in mine
            or norm in theirs
        ):
            return False
    # その年度の候補の行に宣言した条件だけを当てた行の集まりが、残る件数の行と一致すること（宣言していない条件で
    # 行を除いた件数を通さない。D-044①）
    try:
        applied = calc.base_aggregate(
            conn,
            adapter,
            {
                "op": "count",
                "table": base_request.get("table"),
                "filter": [
                    *reflected,
                    {
                        "col": "record_id",
                        "op": "in",
                        "value": sorted(json.loads(base["row_refs_json"] or "[]")),
                    },
                ],
            },
            allow_empty_in=True,
        )
    except KizashiError:
        return False
    return set(applied.row_refs) == rows


def _v8_explanation_status(
    conn: sqlite3.Connection,
    adapter: Adapter,
    lead: dict,
    judged: dict,
    questions: list,
    v: Verification,
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
    if status in ("full", "part") and not any(
        _residual_ok(conn, adapter, lead, judged[t]) for t in scoped
    ):
        v.fail(
            "V8_EXPLANATION_STATUS",
            "説明の状況が「全部」「一部」なのに、支持の T1・T3・T4（根拠付き）に、説明を反映した値（recalc_calc_id。"
            "比べる年度・系列と同じ集計は不可）と、規則に合う残る差（residual_calc_id。金額の候補は比較対象との diff、"
            "金額以外の候補は、候補の行から根拠で説明できる行を除いた残りの件数）がない（実装仕様9章。D-046）。",
            [lead["lead_id"]],
        )
    if status == "unknown" and not _has_purpose(questions, "reason_breakdown"):
        v.fail(
            "V8_EXPLANATION_STATUS",
            "説明の状況が「範囲不明」なのに、理由と内訳を尋ねる取材質問（purposes に reason_breakdown）がない。",
            [lead["lead_id"]],
        )


# ---------------------------------------------------------------------------
# V5 比較の条件
# ---------------------------------------------------------------------------

COMPARISON_KEYS = ("population", "row_meaning", "period", "unit", "entity_mapping", "formula")
COMPARISON_NAMES = {
    "population": "母集団",
    "row_meaning": "1行の意味",
    "period": "期間",
    "unit": "単位",
    "entity_mapping": "主体の対応",
    "formula": "算式",
}
RATE_OPS = ("ratio", "rate")


def _calc_closure(conn: sqlite3.Connection, ids: Iterable[str]) -> list[dict]:
    """計算と、派生計算が参照する計算（入れ子を含む）の行。"""
    seen: dict[str, dict] = {}
    stack = list(ids)
    while stack:
        calc_id = stack.pop()
        if calc_id in seen:
            continue
        row = _one(conn, "SELECT * FROM computations WHERE calc_id = ?", (calc_id,))
        if row is None:
            continue
        seen[calc_id] = row
        stack.extend(json.loads(row["ref_calc_ids_json"] or "[]"))
    return list(seen.values())


def calc_years(conn: sqlite3.Connection, ids: Iterable[str]) -> set[int]:
    """計算（派生計算の入れ子を含む）の基礎集計が限る年度（条件の fiscal_year が1つのもの）。"""
    years: set[int] = set()
    for comp in _calc_closure(conn, ids):
        if comp["kind"] != "base":
            continue
        limited, values = hashing.filter_values(
            json.loads(comp["request_json"]).get("filter") or [], "fiscal_year"
        )
        if limited and len(values) == 1 and isinstance(values[0], int):
            years.add(values[0])
    return years


def comparing_claims(conn: sqlite3.Connection, claims: list[dict]) -> list[str]:
    """比較の主張：参照する計算（本文の {calc:ID} と派生計算の入れ子を含む）が2つ以上の年度にまたがる主張。"""
    found = []
    for claim in claims:
        refs = [*json.loads(claim["calc_ids_json"]), *hashing.CALC_REF_RE.findall(claim["text"])]
        if len(calc_years(conn, refs)) >= 2:
            found.append(claim["claim_id"])
    return found


def _v5(
    conn: sqlite3.Connection, adapter: Adapter, lead: dict, claims: list[dict], v: Verification
) -> None:
    lead_id = lead["lead_id"]
    comparison = json.loads(lead["comparison_json"] or "{}")
    blank = [
        COMPARISON_NAMES[k]
        for k in COMPARISON_KEYS
        if not isinstance(comparison.get(k), str) or not comparison[k].strip()
    ]
    if blank:
        v.fail(
            "V5_CONDITION_MISSING",
            "比較の条件に空欄がある：" + "・".join(blank),
            [lead_id],
        )
    state = lead["comparison_state"]
    if state == "unresolved":
        comparing = comparing_claims(conn, [c for c in claims if c["state"] != "stale"])
        if comparing or lead["proposed_outcome"] == "change":
            what = "、".join(comparing) if comparing else "比較成立の結論の案"
            v.fail(
                "V5_UNRESOLVED_COMPARISON",
                f"比較の条件が「確定できない」（unresolved）なのに、比較の主張がある（{what}）。"
                "各年度の値は別々の記載として残し、比較は保留にする。",
                [lead_id, *comparing],
            )
    if lead["proposed_outcome"] == "change" and state not in (
        "comparable",
        "adjusted",
        "unresolved",
    ):
        # 比較成立は「同じ条件でそろえた変化」（設計書2.1）。unresolved は V5_UNRESOLVED_COMPARISON（D-063）
        v.fail(
            "V5_CHANGE_NOT_COMPARABLE",
            f"比較成立の結論の案なのに、比較の条件が「{state or '未記録'}」。比較成立は比較の条件が"
            "そろっている（comparable）か補正した（adjusted）ときだけ。そろえられなければ保留にする。",
            [lead_id],
        )
    if state == "adjusted":
        adjustment = json.loads(lead["adjustment_json"] or "null")
        values_ok = isinstance(adjustment, dict) and bool(
            str(adjustment.get("method") or "").strip()
        )
        if values_ok:
            for key in ("before", "after"):
                row = _comp(conn, adjustment.get(key))
                values_ok = values_ok and row is not None and row["value"] is not None
        if not values_ok:
            v.fail(
                "V5_ADJUSTED_NO_VALUES",
                "比較の条件が「補正した」（adjusted）なのに、補正の方法と補正前後の値（計算）がない。",
                [lead_id],
            )
    for comp in _calc_closure(conn, hashing.memo_calc_ids(conn, lead_id)):
        if comp["kind"] != "derived":
            continue
        op = json.loads(comp["request_json"]).get("op")
        if op not in RATE_OPS:
            continue
        args = json.loads(comp["ref_calc_ids_json"] or "[]")
        base = _comp(conn, args[1]) if len(args) == 2 else None
        value = None if base is None or base["value"] is None else Decimal(base["value"])
        if value is None or (op == "ratio" and value == 0) or (op == "rate" and value <= 0):
            why = "分母のない率" if op == "ratio" else "0や負の値からの増加率"
            v.fail("V5_BAD_RATE", f"{why}：{comp['calc_id']}", [comp["calc_id"]])
    if lead["proposed_outcome"] == "change":
        years = {int(lead["period_from"]), int(lead["period_to"])}
        partial = {
            y
            for y in years
            for f in ("kyoso", "zuikei")
            if (spec := adapter.file_spec(y, f)) is not None and spec.partial_year
        }
        if partial and partial != years:
            v.fail(
                "V5_PARTIAL_YEAR",
                "比較成立なのに、年度途中（"
                + "・".join(f"{y}年度" for y in sorted(partial))
                + "）と年度全体を比べている。",
                [lead_id],
            )


# ---------------------------------------------------------------------------
# V6 説明の判定
# ---------------------------------------------------------------------------

EXPLAINING_TYPES = ("T1", "T2", "T3", "T4", "T5")


def _v6(conn: sqlite3.Connection, lead: dict, v: Verification) -> None:
    lead_id = lead["lead_id"]
    judged = {
        r["type"]: r
        for r in dict_rows(conn, "SELECT * FROM explanations WHERE lead_id = ?", (lead_id,))
    }
    absent = [f"{t} {TYPE_NAMES[t]}" for t in TYPES if t not in judged]
    if absent:
        v.fail("V6_TYPE_MISSING", "判定のない型がある：" + "、".join(absent), [lead_id])
    own_rows = set(v.candidate_rows)
    for etype in TYPES:
        row = judged.get(etype)
        if row is None:
            continue
        evidence = [e for e in json.loads(row["evidence_json"]) if isinstance(e, dict)]
        judgment = row["judgment"]
        if judgment in ("support", "refute") and not evidence:
            v.fail(
                "V6_NO_EVIDENCE",
                f"{etype} を{'支持' if judgment == 'support' else '否定'}としたのに、根拠の位置がない。",
                [lead_id],
            )
        if judgment == "unknown" and not (row["searched"] or "").strip():
            v.fail(
                "V6_NO_SEARCH_SCOPE",
                f"{etype} が未確認なのに、探した範囲（searched）がない。",
                [lead_id],
            )
        if (
            judgment == "support"
            and etype in EXPLAINING_TYPES
            and evidence
            and all((e.get("position") or {}).get("row_ref") in own_rows for e in evidence)
        ):
            v.fail(
                "V6_SELF_EVIDENCE",
                f"{etype} の支持の根拠が、説明する対象のレコード（候補の行）そのものだけである。"
                "公表元の説明・仕様書など、別の資料の位置を根拠にする。",
                [lead_id],
            )


# ---------------------------------------------------------------------------
# V7 数値と個人情報（check の時点。record と同じ欄の分け方で、台帳の文字列を見直す）
# ---------------------------------------------------------------------------


def _quotes_of(items: Iterable[object]) -> list[str]:
    return [e["quote"] for e in items if isinstance(e, dict) and isinstance(e.get("quote"), str)]


def ledger_texts(
    conn: sqlite3.Connection, lead_id: str
) -> tuple[dict[str, str], dict[str, str], dict[str, str], list[str]]:
    """台帳にある、候補の Claude が書いた文字列を欄ごとに分ける（record の分け方。D-035・D-036）。

    (数値・氏名・強い語を当てる欄, 氏名だけの欄, 氏名と強い語の欄（比較の条件）, 照合済みの引用)。
    CLI が書いた文字列（検出の候補の注記、T6a・T6b の判定、run の計算の意味・単位・依頼）は、数値と強い語の
    対象にしない（氏名と取り込まない列の値は当てる。1-2）。"""
    (lead,) = dict_rows(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    full: dict[str, str] = {}
    names_only: dict[str, str] = {}
    words: dict[str, str] = {}
    quotes_: list[str] = []

    def put(target: dict[str, str], key: str, value: object) -> None:
        if isinstance(value, str) and value:
            target[key] = value

    by_claude = lead["screen_id"] == "explore"
    put(names_only, "lead_key", lead["lead_key"])
    put(full if by_claude else names_only, "note", lead["note"])
    for key in ("hypothesis", "withdraw_if", "reopen_condition"):
        put(full, key, lead[key])
    for i, question in enumerate(json.loads(lead["questions_json"])):
        for key, value in question.items():
            put(full, f"questions[{i}].{key}", value)
    for key, value in json.loads(lead["comparison_json"]).items():
        put(words, f"comparison.{key}", value)
        put(names_only, f"comparison.{key}", value)
    if lead["adjustment_json"]:
        put(full, "adjustment.method", json.loads(lead["adjustment_json"]).get("method"))
    for coef in json.loads(lead["coefficients_json"]):
        key = coef.get("key")
        put(names_only, f"coefficients.{key}.key", key)
        for name in ("meaning", "why_needed", "apply_to", "reporter_note"):
            put(full, f"coefficients.{key}.{name}", coef.get(name))
        basis = coef.get("basis") or []
        quotes_ += _quotes_of(basis)
        for leaf_key, leaf in string_leaves(basis, f"coefficients.{key}.basis"):
            put(names_only, leaf_key, leaf)
    for claim in memo_claims(conn, lead_id):
        put(full, f"{claim['claim_id']}.text", claim["text"])
        evidence = json.loads(claim["evidence_json"])
        quotes_ += _quotes_of(evidence)
        for leaf_key, leaf in string_leaves(evidence, f"{claim['claim_id']}.evidence"):
            put(names_only, leaf_key, leaf)
    for exp in dict_rows(conn, "SELECT * FROM explanations WHERE lead_id = ?", (lead_id,)):
        evidence = json.loads(exp["evidence_json"])
        quotes_ += _quotes_of(evidence)
        # CLI が書いた T6a・T6b の判定は、数値と強い語を当てず、氏名と取り込まない列の値だけを当てる（1-2）
        text_target = names_only if exp["context"] == "cli" else full
        put(text_target, f"{exp['type']}.searched", exp["searched"])
        put(text_target, f"{exp['type']}.no_quant_reason", exp["no_quant_reason"])
        for leaf_key, leaf in string_leaves(evidence, f"{exp['type']}.evidence"):
            put(names_only, leaf_key, leaf)
        for leaf_key, leaf in string_leaves(
            json.loads(exp["reflected_conditions_json"] or "[]"),
            f"{exp['type']}.reflected_conditions",
        ):
            put(names_only, leaf_key, leaf)
    for match in dict_rows(
        conn,
        "SELECT match_id, reason, evidence_json FROM procurement_matches WHERE lead_id = ?",
        (lead_id,),
    ):
        put(full, f"{match['match_id']}.reason", match["reason"])
        evidence = json.loads(match["evidence_json"])
        quotes_ += _quotes_of(evidence)
        for leaf_key, leaf in string_leaves(evidence, f"{match['match_id']}.evidence"):
            put(names_only, leaf_key, leaf)
    for comp in _calc_closure(conn, hashing.memo_calc_ids(conn, lead_id)):
        # run が作った計算（CLI が書いた意味）は、数値と強い語を当てず、氏名と取り込まない列の値だけを当てる
        # （9章の V7。D-053⑪。1-2）
        put(
            full if comp["origin"] == "record" else names_only,
            f"{comp['calc_id']}.meaning",
            comp["meaning"],
        )
        put(names_only, f"{comp['calc_id']}.unit", comp["unit"])
        request = json.loads(comp["request_json"])
        if comp["kind"] == "doc_value" and isinstance(request.get("quote"), str):
            quotes_.append(request["quote"])
        for leaf_key, leaf in string_leaves(request, f"{comp['calc_id']}.request"):
            put(names_only, leaf_key, leaf)
    names_only = {k: v for k, v in names_only.items() if k not in full}
    return full, names_only, words, quotes_


def _unreadable_csv(conn: sqlite3.Connection, root: Path) -> list[str]:
    """氏名の一覧と取り込まない列の値を作る原本（取得済みの公表一覧の CSV）のうち、data/raw にないもの。"""
    return [
        source_id
        for source_id, sha in conn.execute(
            "SELECT source_id, sha256 FROM sources WHERE kind = 'dataset' AND status = 'fetched'"
            " AND logical_key LIKE '%|csv' ORDER BY source_id"
        )
        if not (paths.raw_dir(root) / f"{sha}.csv").is_file()
    ]


def _v7(
    conn: sqlite3.Connection, root: Path, adapter: Adapter, lead: dict, v: Verification
) -> None:
    full, names_only, words, quotes_ = ledger_texts(conn, lead["lead_id"])
    missing = _unreadable_csv(conn, root)
    if missing:
        # 一覧を作れないまま氏名の検査を飛ばして合格にしない
        v.fail(
            "V7_PERSONAL_NAME",
            f"氏名の一覧と取り込まない列の値を作る原本（{'・'.join(missing)}）が data/raw にないため、"
            "氏名の検査ができない。",
            [lead["lead_id"], *missing],
        )
        names: list[str] = []
        excluded: TermMatcher | None = None
    else:
        try:
            names = name_variants(adapter.module.name_list(conn, root, adapter))
            excluded = excluded_matcher(adapter.module.excluded_values(conn, root, adapter))
        except KizashiError as exc:
            if exc.code != "E_FORMAT_CHANGED":
                raise
            # 様式の違う原本から氏名の一覧を作れたとみなさない（飛ばして合格にしない。D-059）
            v.fail(
                "V7_PERSONAL_NAME",
                f"氏名の一覧と取り込まない列の値を作る原本の様式が期待と違うため、氏名の検査ができない：{exc.message}",
                [lead["lead_id"], *exc.affected_ids],
            )
            names, excluded = [], None
    errors, warnings = v7_findings(
        full,
        names=names,
        quotes=quotes_,
        names_only=names_only,
        words_only=words,
        excluded=excluded,
    )
    for error in errors:
        v.fail(error["code"], v7_hit_text(error), [lead["lead_id"]])
    v.warnings += [
        {
            "code": w["code"],
            "message": f"{w['field']} に強い語「{w['hit']}」がある。表現を見直す（不合格にはしない）。",
            "affected_ids": [lead["lead_id"]],
        }
        for w in warnings
    ]


# ---------------------------------------------------------------------------
# 巡回数（4.6。設計書4.3）
# ---------------------------------------------------------------------------


def round_limit(conn: sqlite3.Connection, lead: dict) -> int:
    """候補の巡回数の上限。保留にした候補（保留のメモを書いて state が hold になった候補）を調べ直す場合は、
    1巡を足す（実装仕様3.6、設計書4.3）。保留の結論の案を登録しただけでは足さない（D-056②）。"""
    held = (
        lead["state"] == "hold"
        or conn.execute(
            "SELECT 1 FROM events WHERE kind = 'state_change' AND target_id = ?"
            " AND json_extract(after_json, '$.state') = 'hold' LIMIT 1",
            (lead["lead_id"],),
        ).fetchone()
        is not None
    )
    return limits.MAX_ROUNDS + (1 if held else 0)


def rounds_exceeded(
    lead_id: str, rounds: int, limit: int, stage: str, detail: str = ""
) -> KizashiError:
    return KizashiError(
        "E_ROUNDS_EXCEEDED",
        stage,
        f"候補 {lead_id} の修正の巡回数が上限（{limit}巡）に達している（{rounds}巡）。{detail}"
        "保留以外の結論の案は受け付けない。",
        affected_ids=[lead_id],
        next_action="結論を強めず、保留の結論の案（outcome: hold、hold_reason: limit）を登録して render する。",
    )


def passed_contents_at_limit(conn: sqlite3.Connection, lead: dict) -> set[str]:
    """上限に達した候補で、保留以外のメモに書いてよい中身（検証記録のハッシュ）：最後に数えた check に合格した
    中身と、最後に書いたメモの中身（同じ中身を書き直すため）。最後に数えた check が不合格なら、その後に直した
    中身は数えた check を通っていない（D-056②）。"""
    allowed: set[str] = set()
    row = conn.execute(
        "SELECT result_json, check_hash FROM events WHERE kind = 'check' AND target_id = ?"
        " AND json_extract(after_json, '$.counted') = 1 ORDER BY event_id DESC LIMIT 1",
        (lead["lead_id"],),
    ).fetchone()
    if row is not None and json.loads(row[0] or "{}").get("passed") is True and row[1]:
        allowed.add(row[1])
    if lead["memo_hash"]:
        allowed.add(lead["memo_hash"])
    return allowed


def enforce_rounds_at_render(
    conn: sqlite3.Connection, adapter: Adapter, lead_id: str, stage: str
) -> None:
    """render の中の検査は数えないので、上限に達した候補では、数えた check を通った中身のときだけ保留以外の
    メモを書く（上限の後に直して render で回避させない。設計書4.3。D-056②）。主張を checked にした後、状態を
    結論の案にする前に呼ぶ（check が記録したハッシュと同じ時点の台帳で比べる）。"""
    lead = _one(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    if lead is None or lead["proposed_outcome"] not in COUNTED_OUTCOMES:
        return
    limit = round_limit(conn, lead)
    if lead["rounds"] < limit:
        return
    if hashing.check_hash(conn, lead_id, adapter) not in passed_contents_at_limit(conn, lead):
        raise rounds_exceeded(
            lead_id,
            lead["rounds"],
            limit,
            stage,
            "最後に数えた check に合格した中身と台帳が違う（上限に達した後に直した中身は、数えた check を"
            "通っていない）。",
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
    """V1〜V8 を行い、不合格をすべて集める（強い語は warnings）。T6a・T6b の判定を explanations に登録する
    （呼び出し側のトランザクションの中で）。"""
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
    t6b, b_calcs, b_text, _ = _t6b(conn, adapter, rows, t6b_fields(conn, adapter, lead))
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
    _v2(conn, root, adapter, lead, claims, matches, v)
    _v3(conn, root, adapter, lead, claims, v)
    _v4(conn, root, run_id, adapter, lead, claims, explanations, matches, v)
    _v5(conn, adapter, lead, claims, v)
    _v6(conn, lead, v)
    _v7(conn, root, adapter, lead, v)
    _v8(conn, adapter, lead, claims, matches, v)
    # failures は9章の表の順に並べる（同じコードの中は見つけた順。3.2。D-041①。1-1）
    v.failures.sort(key=lambda f: _FAILURE_RANK.get(f["code"], len(FAILURE_ORDER)))
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
        warnings=v.warnings,  # 不合格でも、得られた強い語の警告を返す（D-090④）
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
        current = _one(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
        if current is not None and current["proposed_outcome"] in COUNTED_OUTCOMES:
            limit = round_limit(conn, current)
            if current["rounds"] >= limit:  # この check で上限を超える。数えず、検査もしない
                raise rounds_exceeded(lead_id, current["rounds"], limit, CHECK_STAGE)
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
            digest = hashing.check_hash(conn, lead_id, adapter)
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
    result = {
        "run_id": run_id,
        "lead_id": lead_id,
        "passed": True,
        "t6": v.t6,
        "checked_claims": checked,
        "rounds": rounds,
        "check_hash": digest,
        "message": "合格した。メモに出る主張を checked にした。メモは kizashi render <lead_id> --run <run_id> で書く。",
    }
    if v.warnings:
        result["warnings"] = v.warnings
    return result
