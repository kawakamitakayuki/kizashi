"""別経路の読み込み処理（アダプタ仕様6章。implementation_spec.md 2章）。T6a（取り込みの照合）と V3(b) で共有する。

正規化の経路（readers.py・normalize.py・adapters/）とは別に書き、その関数を呼ばない。
- csv モジュールでファイルを読む。
- 見出しの行は、最初のセルを NFKC にし空白を除いて `NO` になる行として探す（固定の行番号を使わない）。
  その行と次の行を、列ごとに縦に連結して見出しの文字列にする。上の行の空欄を左の値で埋めない。
- dataset.yaml の期待する見出しの文字列で列を探す。比べる前に、両方を NFKC にし、改行を含む空白をすべて除く。
  一致する列がちょうど1つでなければ V3B_HEADER_UNRESOLVED。見出しが空の列は使えない。
- 論理レコード番号は、見出しの2行の後のレコードを、空のレコードも含めて1から数える（正規化の経路と同じ数え方）。
- 値は前後の空白だけを除いた元の文字列で返す。金額はアダプタ仕様4.4と同じ正規表現で整数にする。

限界：同じ列を両方の経路で誤読する故障、行の入れ替え、過大と過小の相殺は残りうる（設計書5.5）。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kizashi.errors import KizashiError

HEADER_ROWS = 2
_SPACE_RE = re.compile(r"\s+")
# アダプタ仕様4.4：`^\d{1,3}(,\d{3})*$` または `^\d+$`
_AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{3})*|\d+")


def squash(text: str | None) -> str:
    """NFKC にし、改行を含む空白をすべて除く。"""
    return _SPACE_RE.sub("", unicodedata.normalize("NFKC", text or ""))


def parse_amount_yen(raw: str | None) -> int | None:
    """金額の元の値を整数の円にする。NFKC と前後の空白の除去の後に、4.4 の形でなければ None。"""
    text = unicodedata.normalize("NFKC", raw or "").strip()
    if _AMOUNT_RE.fullmatch(text) is None:
        return None
    return int(text.replace(",", ""))


@dataclass(frozen=True)
class IndependentTable:
    columns: dict[str, int]  # キー → 見出しの文字列で特定した列の番号（0始まり）
    records: tuple[tuple[int, dict[str, str]], ...]  # (論理レコード番号, キー → 元の値)


def _unresolved(path: Path, problems: list[str], stage: str) -> KizashiError:
    return KizashiError(
        "V3B_HEADER_UNRESOLVED",
        stage,
        f"見出しの文字列で列を特定できない（{path.name}）：" + "／".join(problems),
        next_action="開発側で原本（data/raw）の見出しと dataset.yaml の take の header を確かめる。",
    )


def read_by_headers(
    path: Path, encoding: str, expected: Mapping[str, str], *, stage: str = "reconcile_import"
) -> IndependentTable:
    """原本の CSV を読み、期待する見出しの文字列（キー → 見出し）で列を特定して、その列の値だけを返す。"""
    path = Path(path)
    try:
        text = path.read_bytes().decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise _unresolved(path, [f"{encoding} で読めない（{exc}）"], stage) from exc
    rows = list(csv.reader(io.StringIO(text, newline="")))

    start = next((i for i, row in enumerate(rows) if row and squash(row[0]) == "NO"), None)
    if start is None:
        raise _unresolved(path, ["最初のセルが NO の見出しの行がない"], stage)
    header = rows[start : start + HEADER_ROWS]
    width = max(len(r) for r in header)
    headers = [squash("".join(r[i] if i < len(r) else "" for r in header)) for i in range(width)]

    columns: dict[str, int] = {}
    problems: list[str] = []
    for key, wanted in expected.items():
        target = squash(wanted)
        if not target:
            problems.append(f"{key} の見出しが空")
            continue
        found = [i for i, h in enumerate(headers) if h == target]
        if not found:
            problems.append(f"{key} の見出し（{wanted}）が見つからない")
        elif len(found) > 1:
            problems.append(f"{key} の見出し（{wanted}）が2つ以上の列にある（{found}）")
        else:
            columns[key] = found[0]
    if problems:
        raise _unresolved(path, problems, stage)

    body = rows[start + HEADER_ROWS :]
    records = tuple(
        (
            number,
            {key: (row[col].strip() if col < len(row) else "") for key, col in columns.items()},
        )
        for number, row in enumerate(body, start=1)
    )
    return IndependentTable(columns=columns, records=records)
