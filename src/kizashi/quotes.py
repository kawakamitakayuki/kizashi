"""引用の照合（implementation_spec.md 8章の quotes.py、6.1 の根拠の書式、6.4 の知識ファイルの手がかり）。

- 資料のページ別テキスト（CSV は records.cells_json の column のキーの値）と引用を、どちらも NFKC にし、
  空白と改行を除いて比べる。部分一致でよい。
- 引用は文末（。．！？で終わる）か、資料の段落の終わり（空行の前とページの終わり）・セルの終わりまで
  含むこと。途中で切れていれば E_QUOTE_TRUNCATED。資料の中で直後が数字の「.」は文末にしない。
- 資料の数値（7.2 の数値の並びに、直前の符号・▲・△ を含めたもの）の途中で始まる・終わる引用も
  E_QUOTE_TRUNCATED（`750,000` の `000`、`1.5件` の `1.`、`-12件` の `12件`、`.5` の `5` を通さない。
  D-033・D-036）。引用だけを読んだときの数値が資料の中で読む数値と違う引用（`10-15件。` の `-15件。`。
  資料では「-」が符号でない）も E_QUOTE_TRUNCATED（D-039）。
- 主張の種類が「原資料の記載」（record）で、主張の fiscal_year とデータの資料の年度（論理資料のキー）が
  食い違えば E_QUOTE_YEAR。追加資料の年度は機械では決めない。
- 索引ページ（kind='index'）、公表一覧の PDF（kind='dataset' の PDF。D-021）、知識ファイルは根拠に
  できない（E_BAD_EVIDENCE_SOURCE）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from kizashi import ledger, paths
from kizashi.adapters import Adapter
from kizashi.errors import KizashiError
from kizashi.normalize import key_text

STAGE = "record"
ROLES = ("support", "oppose", "background")
# 文末の文字（NFKC の後。．！？は . ! ? になる）。
SENTENCE_END = ("。", ".", "!", "?")
_PARAGRAPH_RE = re.compile(r"\n[ \t　\r]*\n")
# 資料の数値。引用の境目がこの途中なら通さない（D-033・D-036）。7.2 の数値の並び（前後が数字・,・. で
# ない、数字と , . の連なり）に、直前の ▲・△ と符号（-・−・+。前が数字・英字でないもの）を含める。
# 空白は key_text で除いた後に見る。
_NUMBER_RUN_RE = re.compile(r"(?:[▲△]|(?<![0-9A-Za-z])[-−+])?(?<![\d,.])[\d,.]*\d[\d,.]*(?![\d,.])")

# 6.4 の basis の file → references/ のファイル名の頭（版の部分は問わない）。
KNOWLEDGE_FILES = {
    "playbook": "investigative_reporting_playbook_",
    "case_studies": "investigative_open_data_case_studies_",
    "catalog": "investigative_open_data_catalog_",
}


@dataclass(frozen=True)
class VerifiedQuote:
    source_id: str
    quote: str  # 提案に書かれた引用（照合済み）


def _err(code: str, message: str, *, next_action: str = "", affected: tuple = ()) -> KizashiError:
    return KizashiError(code, STAGE, message, affected_ids=affected, next_action=next_action)


def _schema(message: str) -> KizashiError:
    return _err(
        "E_SCHEMA",
        message,
        next_action='根拠は {"source_id", "position", "quote", "role"} の形で書く'
        "（PDF・Webページは position に page、CSV は row_ref と column）。",
    )


def _segments_text(segments: list[str]) -> tuple[str, set[int], list[tuple[int, int]]]:
    """区切り（段落・セル）ごとに正規化してつなぐ。区切りの終わりの位置の集合と、数値の範囲
    （[始め, 終わり)。区切りをまたいでつながない）も返す。"""
    text = ""
    ends: set[int] = set()
    runs: list[tuple[int, int]] = []
    for seg in segments:
        part = key_text(seg)
        runs.extend(
            (len(text) + m.start(), len(text) + m.end()) for m in _NUMBER_RUN_RE.finditer(part)
        )
        text += part
        ends.add(len(text))
    return text, ends, runs


def _inside_number(pos: int, runs: list[tuple[int, int]]) -> bool:
    return any(start < pos < end for start, end in runs)


def _same_numbers(target: str, start: int, runs: set[tuple[int, int]]) -> bool:
    """引用だけを読んだときの数値（符号・▲・△ を含む）が、資料のその位置で読む数値と同じか。

    資料の「10-15件。」から「-15件。」を引用すると、引用だけでは -15 に読めるが、資料では「-」が符号でなく
    15 である。こうした引用を通さない（D-033 の「符号の落ちた・付いた値に計算IDを付けない」）。"""
    return all(
        (start + m.start(), start + m.end()) in runs for m in _NUMBER_RUN_RE.finditer(target)
    )


def _sentence_end(text: str, target: str, end: int) -> bool:
    """引用が文末の文字で終わるか。資料の中で直後が数字の「.」は小数点なので文末にしない。"""
    if not target.endswith(SENTENCE_END):
        return False
    return not (target.endswith(".") and end < len(text) and text[end].isdigit())


def _truncated(message: str, affected: tuple) -> KizashiError:
    return _err(
        "E_QUOTE_TRUNCATED",
        message,
        next_action="否定や条件を取り違えないよう、文末まで含めて引用する。数値は全体を引用する。",
        affected=affected,
    )


def _match(segments: list[str], quote: str, where: str, affected: tuple) -> None:
    target = key_text(quote)
    if not target:
        raise _schema("引用（quote）が空である。")
    text, ends, runs = _segments_text(segments)
    starts = []
    i = text.find(target)
    while i != -1:
        starts.append(i)
        i = text.find(target, i + 1)
    if not starts:
        raise _err(
            "E_QUOTE_NOT_FOUND",
            f"引用が{where}にない（NFKC にし空白と改行を除いて比べた）。",
            next_action="資料の該当箇所を Read で読み直し、原文どおりに引用する。",
            affected=affected,
        )
    whole = [
        s
        for s in starts
        if not _inside_number(s, runs) and not _inside_number(s + len(target), runs)
    ]
    if not whole:
        raise _truncated(
            f"引用が資料の数値の途中で始まるか、途中で終わる（{where}。数値は全体を引用する）。"
            "PDF の行の終わりは段落の終わりにしないので、前の行の終わりの数字と次の行の始めの数字は1つの数値として"
            "読む（例：「付録９」の次の行の「１９９９年度」は「91999」）。同じことを裏付ける別の文（数値の途中から"
            "始まらず、文末で終わるもの）があるときだけ、それを引用する。なければ根拠にせず、記者が原本で確かめる"
            "事項として残す（D-031④・D-050）。",
            affected,
        )
    run_set = set(runs)
    whole = [s for s in whole if _same_numbers(target, s, run_set)]
    if not whole:
        raise _truncated(
            f"引用の中の数値が、資料の中で読む数値と違う（{where}。資料で符号でない「-」・「+」から"
            "引用を始めない。例：「10-15件。」の「-15件。」は「範囲は10-15件。」のように引用する）。",
            affected,
        )
    for s in whole:
        end = s + len(target)
        if end in ends or _sentence_end(text, target, end):
            return
    raise _truncated(
        f"引用が文の途中で切れている（{where}。文末の。．！？か、段落・セルの終わりまで引用する）。",
        affected,
    )


def _source(conn: sqlite3.Connection, source_id: str) -> tuple:
    row = conn.execute(
        "SELECT source_id, kind, logical_key, status FROM sources WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        raise _err(
            "E_REF_NOT_FOUND",
            f"資料がない：{source_id}",
            next_action="inspect <run_id> で資料IDを確かめる。",
            affected=(source_id,),
        )
    return row


def _csv_of_pdf(conn: sqlite3.Connection, logical_key: str) -> str | None:
    csv_key = logical_key[: -len("|pdf")] + "|csv"
    row = conn.execute(
        "SELECT source_id FROM sources WHERE logical_key = ? AND kind = 'dataset'", (csv_key,)
    ).fetchone()
    return None if row is None else row[0]


def _year_of(logical_key: str | None) -> int | None:
    if not logical_key:
        return None
    parts = logical_key.split("|")
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None


def _key_list(adapter: Adapter, logical_key: str, keys: list[str]) -> str:
    year = _year_of(logical_key)
    family = logical_key.split("|")[2]
    spec = adapter.file_spec(year, family) if year is not None else None
    take = spec.take if spec is not None else {}
    return "、".join(f"{k}（{take.get(k, {}).get('header', '')}）" for k in keys)


def verify_quote(
    conn: sqlite3.Connection,
    root: Path,
    run_id: str,
    adapter: Adapter,
    evidence: object,
    *,
    claim_type: str | None = None,
    fiscal_year: int | None = None,
) -> VerifiedQuote:
    """根拠の1件（6.1 の書式）を照合する。失敗すれば型付きのエラー。"""
    if isinstance(evidence, dict) and "file" in evidence:
        raise _err(
            "E_BAD_EVIDENCE_SOURCE",
            "知識ファイル（references/）は主張の根拠にできない。",
            next_action="知識ファイルは係数の提案の手がかり（kind: coefficient の basis）にだけ使う。"
            "根拠には取得した公的資料の位置を書く。",
        )
    if not isinstance(evidence, dict):
        raise _schema("根拠の要素がオブジェクトでない。")
    missing = [k for k in ("source_id", "position", "quote", "role") if k not in evidence]
    if missing:
        raise _schema(f"根拠に {'・'.join(missing)} がない。")
    extra = sorted(set(evidence) - {"source_id", "position", "quote", "role"})
    if extra:
        raise _schema(f"根拠に知らない項目がある：{'・'.join(extra)}")
    source_id, position, quote, role = (
        evidence["source_id"],
        evidence["position"],
        evidence["quote"],
        evidence["role"],
    )
    if not isinstance(source_id, str) or ledger.id_kind(source_id) != "source":
        raise _schema(f"source_id が資料ID（S＋3桁）でない：{source_id!r}")
    if not isinstance(quote, str) or not quote.strip():
        raise _schema("quote が空でない文字列でない。")
    if role not in ROLES:
        raise _schema(
            f"role は {'・'.join(ROLES)} のどれか：{role!r}（根拠の役割。support は支える、oppose は反する、"
            "background は背景。判定の judgment の値（refute など）とは別）"
        )
    if not isinstance(position, dict):
        raise _schema("position がオブジェクトでない。")

    _, kind, logical_key, status = _source(conn, source_id)
    is_pdf_dataset = kind == "dataset" and (logical_key or "").endswith("|pdf")
    if kind == "index":
        raise _err(
            "E_BAD_EVIDENCE_SOURCE",
            f"索引ページ（{source_id}。fetch --index で取得した資料）は根拠にできない。",
            next_action="リンク先の資料を取得して、その資料を引用する。",
            affected=(source_id,),
        )
    if is_pdf_dataset:
        csv_id = _csv_of_pdf(conn, logical_key)
        raise _err(
            "E_BAD_EVIDENCE_SOURCE",
            f"公表一覧の PDF（{source_id}）はページ別テキストがなく、根拠にできない（D-021）。",
            next_action=(
                f"同じ年度・方式の CSV（{csv_id}）の row_ref と column で引用する"
                f"（kizashi inspect <row_ref> --run {run_id} で値を読む）。"
            ),
            affected=(source_id,),
        )
    if status != "fetched":
        raise _err(
            "E_REF_NOT_FOUND",
            f"資料 {source_id} は取得できていない（状態：{status}）。",
            affected=(source_id,),
        )

    if kind == "dataset":
        if set(position) != {"row_ref", "column"}:
            raise _schema(f"CSV の資料（{source_id}）の position は row_ref と column で書く。")
        row_ref, column = position["row_ref"], position["column"]
        if not isinstance(row_ref, str) or not isinstance(column, str):
            raise _schema("row_ref と column は文字列で書く。")
        if not row_ref.startswith(f"{source_id}:"):
            raise _err(
                "E_REF_NOT_FOUND",
                f"資料 {source_id} にレコード {row_ref} はない（row_ref の資料IDが違う）。",
                affected=(row_ref,),
            )
        rec = conn.execute(
            "SELECT cells_json FROM records WHERE record_id = ?", (row_ref,)
        ).fetchone()
        if rec is None:
            raise _err("E_REF_NOT_FOUND", f"レコードがない：{row_ref}", affected=(row_ref,))
        cells = json.loads(rec[0])
        if column not in cells:
            raise _err(
                "E_REF_NOT_FOUND",
                f"レコード {row_ref} に列のキー {column!r} はない（見出しの文字列では書かない）。",
                next_action=f"使えるキー：{_key_list(adapter, logical_key, list(cells))}",
                affected=(row_ref,),
            )
        _match([cells[column]], quote, f"{row_ref} の {column}", (row_ref,))
    else:
        if set(position) != {"page"}:
            raise _schema(f"PDF・Webページの資料（{source_id}）の position は page で書く。")
        page = position["page"]
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise _schema("page は1以上の整数で書く。")
        path = paths.run_dir(root, run_id) / "evidence" / source_id / "pages" / f"{page}.txt"
        if not path.is_file():
            raise _err(
                "E_REF_NOT_FOUND",
                f"資料 {source_id} に {page} ページはない。",
                next_action=f"kizashi inspect {source_id} --run {run_id} でページ数を確かめる。",
                affected=(source_id,),
            )
        text = path.read_text(encoding="utf-8")
        _match(_PARAGRAPH_RE.split(text), quote, f"{source_id} の {page} ページ", (source_id,))

    if claim_type == "record" and fiscal_year is not None and kind == "dataset":
        year = _year_of(logical_key)
        if year is not None and year != fiscal_year:
            raise _err(
                "E_QUOTE_YEAR",
                f"「原資料の記載」の年度（{fiscal_year}）と資料 {source_id} の年度（{year}）が違う。",
                next_action="主張の fiscal_year か、引用する資料を直す。",
                affected=(source_id,),
            )
    return VerifiedQuote(source_id=source_id, quote=quote)


def verify_knowledge_quote(root: Path, item: object) -> None:
    """係数の提案の手がかり（6.4）の知識ファイルの引用が、references/ の該当ファイルにあるかを確かめる。

    主張の根拠ではないので、文末までの規則は当てない。
    """
    if not isinstance(item, dict) or set(item) != {"file", "locator", "quote"}:
        raise _schema('知識ファイルの手がかりは {"file", "locator", "quote"} の形で書く。')
    prefix = KNOWLEDGE_FILES.get(item["file"]) if isinstance(item["file"], str) else None
    if prefix is None:
        raise _schema(f"file は {'・'.join(KNOWLEDGE_FILES)} のどれか：{item['file']!r}")
    if not isinstance(item["quote"], str) or not key_text(item["quote"]):
        raise _schema("quote が空でない文字列でない。")
    if not isinstance(item["locator"], str) or not item["locator"].strip():
        raise _schema("locator が空でない文字列でない。")
    files = sorted((root / "references").glob(f"{prefix}*.md"))
    if len(files) != 1:
        raise _err(
            "E_REF_NOT_FOUND",
            f"references/ に知識ファイル（{item['file']}）が1つだけあるのでない（{len(files)}個）。",
        )
    text = key_text(files[0].read_text(encoding="utf-8"))
    if key_text(item["quote"]) not in text:
        raise _err(
            "E_QUOTE_NOT_FOUND",
            f"手がかりの引用が知識ファイル（{item['file']}、{item['locator']}）にない。",
            next_action="Grep で該当の ID の箇所を探し、原文どおりに引用する。",
        )
