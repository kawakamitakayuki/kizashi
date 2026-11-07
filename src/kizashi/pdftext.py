"""公表一覧の PDF の表のセルの切り出し、行の対応、項目の照合（アダプタ仕様7.1〜7.3。D-002 の方法(a)）。

- pdfplumber を呼ぶのは load_pages と extract_pages だけ。テストではこの2つを monkeypatch し、合成の罫線と
  文字の座標、合成のページのテキストを返す。
- NO の列と照合に使う列以外の文字は、セルに振り分ける段で捨て、文字列にしない（担当官の列の氏名を含む）。
- 公表一覧の PDF のページ別テキストは作らない（D-021）。このモジュールはファイルに何も書かない。
- 追加資料の PDF のページ別テキストは extract_pages で取り出す（書き出しは fetcher.fetch_evidence。実装仕様4.2）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kizashi.adapters import FileSpec
from kizashi.normalize import (
    corp_check_digit_ok,
    exponent_matches,
    key_text,
    nfkc,
    parse_corp_number,
)

# 7.1 の数値
THIN = 2.0  # 罫線とみなす四角形の短い辺（pt 未満）
LONG = 3.0  # 罫線とみなす四角形の長い辺（pt を超える）
CLUSTER = 1.5  # 罫線の座標をまとめる差（pt 以内）
GAP_RATIO = 0.8  # 文字の大きさのこの倍を超える間に空白を入れる

# 7.3：数値の並び（数字と桁区切りの連なりで、前後が数字・`,`・`.` でないもの）と、R の日付
NUMBER_RE = re.compile(r"(?<![\d,.])[\d,]*\d[\d,]*(?![\d,.])")
R_DATE_RE = re.compile(r"(?<![\d.])R\d+\.\d+\.\d+(?![\d.])")
R_DATE_FULL_RE = re.compile(r"R\d+\.\d+\.\d+")

MATCH, MISMATCH, UNVERIFIABLE = "match", "mismatch", "unverifiable"


# ---------------------------------------------------------------------------
# ページの読み込み（pdfplumber を呼ぶのはここだけ）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(frozen=True)
class Char:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    size: float


@dataclass(frozen=True)
class Page:
    number: int  # ファイルの中のページ番号（1から）
    rects: tuple[Rect, ...]
    chars: tuple[Char, ...]


def extract_pages(path: Path) -> list[str]:
    """追加資料の PDF のページ別テキスト（pdfplumber 0.11.10 の extract_text。実装仕様4.2）。

    公表一覧の PDF（kind='dataset'）には使わない（D-021。担当官の氏名を書き出さないため）。
    """
    import pdfplumber

    texts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
            page.flush_cache()
    return texts


def load_pages(path: Path) -> list[Page]:
    """PDF の各ページの細い四角形（rects）と文字（chars）を読む（pdfplumber 0.11.10）。"""
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            rects = tuple(
                Rect(float(r["x0"]), float(r["top"]), float(r["x1"]), float(r["bottom"]))
                for r in page.rects
            )
            chars = tuple(
                Char(
                    str(c["text"]),
                    float(c["x0"]),
                    float(c["top"]),
                    float(c["x1"]),
                    float(c["bottom"]),
                    float(c["size"]),
                )
                for c in page.chars
            )
            pages.append(Page(number, rects, chars))
            page.flush_cache()
    return pages


# ---------------------------------------------------------------------------
# 7.1 セルの切り出し
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    columns: int  # dataset.yaml の pdf_columns
    no_column: int
    date_column: int
    keep: dict[int, str]  # 照合に使う列 → 期待する見出しの文字列（NFKC・空白除去）
    field_columns: dict[str, int]  # 照合の記録のキー → 列


def table_spec(spec: FileSpec, pdf_fields: Mapping[str, str]) -> TableSpec:
    """dataset.yaml の take と reconcile.pdf_fields から、切り出しの設定を作る。

    PDF の列の番号は take の index と同じ（7.3。D-002）。この年度・方式の take にないキー（競争入札の
    reason_raw、随意契約の method_raw）は照合しない。
    """
    field_columns: dict[str, int] = {}
    keep: dict[int, str] = {}
    for field_key, take_key in pdf_fields.items():
        item = spec.take.get(take_key)
        if item is None:
            continue
        index = int(item["index"])
        field_columns[field_key] = index
        keep[index] = key_text(str(item["header"]))
    no_index = spec.index_of("no")
    date_index = spec.index_of("contract_date_raw")
    assert no_index is not None and date_index is not None
    return TableSpec(
        columns=spec.pdf_columns,
        no_column=no_index,
        date_column=date_index,
        keep=keep,
        field_columns=field_columns,
    )


@dataclass
class PdfRow:
    page: int
    no: str  # NO のセル（NFKC・空白除去）
    cells: dict[int, str]  # NO の列と照合に使う列だけ
    split_affected: bool = False


@dataclass
class PdfTable:
    columns: int
    page_count: int
    date_column: int
    rows: list[PdfRow] = field(default_factory=list)  # データの行（NO が数字の行）
    skipped_pages: list[dict] = field(default_factory=list)  # 列の数が違うページ
    split_pages: list[int] = field(default_factory=list)  # 分かれた行のあるページ


def cluster(values: Iterable[float], tol: float = CLUSTER) -> list[float]:
    """差が tol 以内の値をまとめ、まとまりの平均を返す。"""
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def cell_text(chars: Sequence[Char]) -> str:
    """セルの文字列。文字の上下の中心の順に見て、その行の最初の文字の上端と下端の間にあれば同じ行にまとめる。
    行ごとに x の順に並べ、前の文字との間が文字の大きさの0.8倍を超えるところと、行の区切りに半角の空白を入れる。"""
    lines: list[list[Char]] = []
    for c in sorted(chars, key=lambda c: ((c.top + c.bottom) / 2, c.x0)):
        middle = (c.top + c.bottom) / 2
        if lines and lines[-1][0].top <= middle <= lines[-1][0].bottom:
            lines[-1].append(c)
        else:
            lines.append([c])
    parts: list[str] = []
    for line in lines:
        line.sort(key=lambda c: c.x0)
        text = ""
        prev: Char | None = None
        for c in line:
            if prev is not None and c.x0 - prev.x1 > c.size * GAP_RATIO:
                text += " "
            text += c.text
            prev = c
        parts.append(text)
    return " ".join(parts)


def _index(bounds: Sequence[float], value: float) -> int | None:
    """bounds[i] <= value < bounds[i + 1] となる i。"""
    for i in range(len(bounds) - 1):
        if bounds[i] <= value < bounds[i + 1]:
            return i
    return None


def _page_rows(page: Page, xs: list[float], spec: TableSpec) -> list[dict[int, str]]:
    """1ページの罫線の行ごとに、NO の列と照合に使う列のセルの文字列を返す（上から順）。"""
    no_mid = (xs[spec.no_column] + xs[spec.no_column + 1]) / 2
    ys = cluster(
        (r.top + r.bottom) / 2
        for r in page.rects
        if (r.bottom - r.top) < THIN and (r.x1 - r.x0) > LONG and r.x0 <= no_mid <= r.x1
    )
    wanted = {spec.no_column, *spec.keep}
    buckets: list[dict[int, list[Char]]] = [{} for _ in range(max(len(ys) - 1, 0))]
    for c in page.chars:
        if not c.text.strip():
            continue
        col = _index(xs, (c.x0 + c.x1) / 2)
        if col not in wanted:
            continue  # NO の列と照合に使う列以外の文字は捨てる（担当官の列の氏名を含む）
        row = _index(ys, (c.top + c.bottom) / 2)
        if row is None:
            continue
        buckets[row].setdefault(col, []).append(c)
    return [{col: cell_text(chs) for col, chs in b.items()} for b in buckets]


def _is_header(cells: dict[int, str], spec: TableSpec, seen_no: bool) -> bool:
    no = key_text(cells.get(spec.no_column, ""))
    if no == "NO":
        return True
    if no or not seen_no:
        return False
    return all(key_text(cells.get(col, "")) in header for col, header in spec.keep.items())


def extract_cells(pages: Sequence[Page], spec: TableSpec) -> PdfTable:
    """各ページの罫線でセルを切り出し、データの行を返す（7.1）。

    - 列の境界：幅が2pt未満で高さが3ptを超える四角形の中心の x をまとめたもの。列の数が pdf_columns と
      違うページは使わない（skipped_pages）。
    - 行の境界：高さが2pt未満で幅が3ptを超える四角形のうち、NO の列の中央の x を横切るものの中心の y。
    - 見出し：ページの上から、NO のセルが `NO` の行と、それに続く NO のセルが空で照合に使う列の文字が
      どれも期待する見出しの文字列の一部である行。
    - 分かれた行：見出しより下で、NO のセルが数字でなく照合に使う列に文字がある行。そのページのすべての件と、
      その行がページの最初（見出しの直後）にあるときは前のページの最後の件に split_affected を付ける。
    """
    table = PdfTable(columns=spec.columns, page_count=len(pages), date_column=spec.date_column)
    last_row_of_prev_page: PdfRow | None = None
    for page in pages:
        xs = cluster(
            (r.x0 + r.x1) / 2
            for r in page.rects
            if (r.x1 - r.x0) < THIN and (r.bottom - r.top) > LONG
        )
        if len(xs) - 1 != spec.columns:
            table.skipped_pages.append({"page": page.number, "columns": max(len(xs) - 1, 0)})
            last_row_of_prev_page = None
            continue
        rows = _page_rows(page, xs, spec)
        body_start = 0
        seen_no = False
        while body_start < len(rows) and _is_header(rows[body_start], spec, seen_no):
            seen_no = True
            body_start += 1
        page_rows: list[PdfRow] = []
        split = False
        split_first = False
        for position, cells in enumerate(rows[body_start:]):
            no = key_text(cells.get(spec.no_column, ""))
            if no.isascii() and no.isdigit():
                page_rows.append(PdfRow(page=page.number, no=no, cells=cells))
            elif any(key_text(cells.get(col, "")) for col in spec.keep):
                split = True
                split_first = split_first or position == 0
        if split:
            table.split_pages.append(page.number)
            for row in page_rows:
                row.split_affected = True
            if split_first and last_row_of_prev_page is not None:
                last_row_of_prev_page.split_affected = True
        table.rows.extend(page_rows)
        last_row_of_prev_page = page_rows[-1] if page_rows else None
    return table


# ---------------------------------------------------------------------------
# 7.2 行の対応
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    status: str  # matched / not_found / ambiguous
    row: PdfRow | None = None
    reason: str | None = None  # split_row
    pages: list[int] = field(default_factory=list)  # ambiguous のときの候補のページ


def locate_row(table: PdfTable, no: str | None, contract_date_raw: str | None) -> Location:
    """NO のセルが CSV の NO と等しい行を全ページから探す（金額は使わない）。

    1つなら matched。2つ以上なら契約日のセルに CSV の契約日の表記を含むものに絞り、1つなら matched、
    それ以外は ambiguous。0なら not_found。分かれた行に当たる件は ambiguous（split_row）。
    """
    want = key_text(no)
    hits = [r for r in table.rows if want and r.no == want]
    if not hits:
        return Location("not_found")
    pages = sorted({r.page for r in hits})
    if any(r.split_affected for r in hits):
        return Location("ambiguous", reason="split_row", pages=pages)
    if len(hits) > 1:
        date = key_text(contract_date_raw)
        narrowed = [
            r for r in hits if date and date in key_text(r.cells.get(table.date_column, ""))
        ]
        if len(narrowed) != 1:
            return Location("ambiguous", pages=pages)
        hits = narrowed
    return Location("matched", row=hits[0])


# ---------------------------------------------------------------------------
# 7.3 項目の照合
# ---------------------------------------------------------------------------


def numbers_in(cell: str) -> list[str]:
    """セルの中の数値の並び（桁区切りを除いた数字の文字列）。"""
    return [m.group(0).replace(",", "") for m in NUMBER_RE.finditer(nfkc(cell))]


def match_number(value: str, cell: str) -> str:
    """数値の並びのどれかが value と等しければ match、並びはあるが等しいものがなければ mismatch、
    並びがなければ unverifiable。部分一致では照らさない。"""
    found = numbers_in(cell)
    if not found:
        return UNVERIFIABLE
    return MATCH if value in found else MISMATCH


def match_exact(value: str | None, cell: str) -> str:
    """NFKC・空白除去の後の完全一致。どちらかが空なら unverifiable。"""
    a, b = key_text(value), key_text(cell)
    if not a or not b:
        return UNVERIFIABLE
    return MATCH if a == b else MISMATCH


def match_date(value: str | None, cell: str) -> str:
    """CSV の表記（R6.4.1 の形）が、セルの中に前後が数字・`.` でない形であれば match。セルに R の日付の形が
    あり等しいものがなければ mismatch、なければ unverifiable。R の形でなければ完全一致で照らす。"""
    want = key_text(value)
    if not want:
        return UNVERIFIABLE
    if R_DATE_FULL_RE.fullmatch(want) is None:
        return match_exact(value, cell)
    found = R_DATE_RE.findall(nfkc(cell))
    if not found:
        return UNVERIFIABLE
    return MATCH if want in found else MISMATCH


def match_field(field_key: str, values: Mapping[str, object], cell: str) -> str:
    """項目（照合の記録のキー）を、対応する列のセルの文字列で照らす（7.3）。

    values は CSV の行の値：title_raw・counterparty_raw・method_raw・reason_raw・contract_date_raw・
    amount_raw（records.cells_json の元の値）と、amount_yen・amount_note・corp_number_state・corp_numbers
    （正規化の経路の値）。
    """
    if field_key == "amount_yen":
        yen = values.get("amount_yen")
        if isinstance(yen, int):
            return match_number(str(yen), cell)
        if values.get("amount_note") == "out_of_range":
            # 台帳の整数の範囲を超える金額も数値なので、元の値の数字の並びで照らす（D-091③・D-092④）
            digits = numbers_in(_str(values.get("amount_raw")))
            return match_number(digits[0], cell) if len(digits) == 1 else UNVERIFIABLE
        if values.get("amount_note") in ("unit_price_text", "foreign_currency", "non_numeric"):
            return match_exact(_str(values.get("amount_raw")), cell)
        return UNVERIFIABLE  # missing（と、迷う場合）
    if field_key == "corp_numbers":
        numbers = values.get("corp_numbers")
        if values.get("corp_number_state") == "digits13" and isinstance(numbers, list):
            return match_number(str(numbers[0]), cell)
        return UNVERIFIABLE  # ほかの状態（PDF の13桁での補完は P1）
    if field_key == "contract_date":
        return match_date(_str(values.get("contract_date_raw")), cell)
    if field_key in ("title_raw", "counterparty_raw", "method_raw", "reason_raw"):
        return match_exact(_str(values.get(field_key)), cell)
    raise ValueError(f"知らない照合の項目：{field_key}")


# 法人番号の補完で、件名と金額が CSV と PDF で一致したことを確かめる照合の項目（設計書7.4の条件3）
COMPLETION_FIELDS = ("title_raw", "amount_yen")


def corp_completion(
    raw: str | None, cell: str, fields: Mapping[str, str]
) -> tuple[dict, str | None]:
    """CSV の法人番号が指数表記の行で、PDF の法人番号の欄の13桁を採用するか（P1-b。設計書7.4、D-077）。

    PDF の欄が13桁ちょうど1つ（4.6 の digits13 と同じ判定）で、①検査用数字が合う（D-076）、②CSV の指数表記に
    残る有効桁と合う、③件名と金額の照合（fields）がどちらも match、のすべてを満たすときだけ採用する。
    戻り値は（照合の記録に書く結果, 採用した13桁）。満たさない条件を failed に順に書く（pdf_cell・check_digit・
    significant_digits・title_amount。欄の13桁が1つでなければ①②は見ない）。どちらかを正解にはしない。
    """
    failed: list[str] = []
    state, numbers = parse_corp_number(cell)
    number = numbers[0] if state == "digits13" else None
    if number is None:
        failed.append("pdf_cell")
    else:
        if not corp_check_digit_ok(number):
            failed.append("check_digit")
        if not exponent_matches(number, raw):
            failed.append("significant_digits")
    if any(fields.get(f) != MATCH for f in COMPLETION_FIELDS):
        failed.append("title_amount")
    if failed:
        return {"state": "unconfirmed", "failed": failed}, None
    return {"state": "valid_from_pdf"}, number


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
