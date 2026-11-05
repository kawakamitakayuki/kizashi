"""正規化の経路の CSV の読み込み（implementation_spec.md 2章の readers.py。アダプタ仕様2章）。

dataset.yaml の列番号で読む。1行目は表題、2行目は作成日、3・4行目（0始まりで2と3）が見出し、
5行目からがデータ。論理レコード番号は、見出しの2行の後の CSV のレコードを、空のレコードも含めて
1から数える（セルの中に改行があるので、物理的な行番号とは違う）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from kizashi.normalize import key_text

TITLE_ROWS = 2
HEADER_ROWS = 2


class DecodeError(Exception):
    """決めた文字コードで読めない（品質検査 Q01）。"""


@dataclass(frozen=True)
class CsvTable:
    encoding: str
    preamble: tuple[tuple[str, ...], ...]  # 表題と作成日の2行
    header_rows: tuple[tuple[str, ...], ...]  # 見出しの2行
    records: tuple[tuple[int, tuple[str, ...]], ...]  # (論理レコード番号, セル)

    def header_strings(self) -> list[str]:
        """列ごとに見出しの2行を縦に連結し、NFKC と空白・改行の除去をした文字列（2章）。

        上の行の空欄を左の値で埋めない。
        """
        width = max((len(r) for r in self.header_rows), default=0)
        return [
            key_text("".join(r[i] if i < len(r) else "" for r in self.header_rows))
            for i in range(width)
        ]


def decode(data: bytes, encoding: str) -> str:
    """決めた文字コードで厳密に読む。読めなければ DecodeError。"""
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise DecodeError(f"{encoding} で読めない：{exc}") from exc


def parse_text(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def read_table(data: bytes, encoding: str) -> CsvTable:
    rows = parse_text(decode(data, encoding))
    preamble = tuple(tuple(r) for r in rows[:TITLE_ROWS])
    header = tuple(tuple(r) for r in rows[TITLE_ROWS : TITLE_ROWS + HEADER_ROWS])
    body = rows[TITLE_ROWS + HEADER_ROWS :]
    records = tuple((no, tuple(r)) for no, r in enumerate(body, start=1))
    return CsvTable(encoding=encoding, preamble=preamble, header_rows=header, records=records)


def read_rows(path: Path, encoding: str) -> CsvTable:
    """原本のファイルを読む。"""
    return read_table(path.read_bytes(), encoding)
