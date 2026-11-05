"""公表一覧の PDF の罫線と文字の座標をまねた合成のデータを作る（test_plan.md の1章 pdf_cells、アダプタ仕様11章）。

PDF のライブラリは使わない。テストでは pdftext.load_pages を monkeypatch して、このデータをページの一覧として
返す（D-002）。実データは使わない（D-007 が決まるまで）。受注者名・法人番号・担当官の氏名は架空の値。

出力：tests/fixtures/pdf_cells/ の
- kyoso_2024_mini.json・kyoso_2025_mini.json・zuikei_2024_mini.json・zuikei_2025_mini.json：
  合成の CSV（make_cao_fixtures.py の表）と同じ表を PDF の形にしたもの（1ページに5件、ページごとに1行の見出し）。
  随意契約は CSV の公表の17列だけ（令和7年度の内部用の10列は PDF にない）。CSV の `?` は PDF では `−`（U+2212）。
  zuikei_2025 の NO 6 の件名の「入」は康熙部首の「⼊」（U+2F0A。上端が1.51pt 下）、NO 2 の備考の列にセルの中だけの横線。
- traps_zuikei.json・traps_kyoso.json：PD-1・PD-2 の罠（下の traps_zuikei・traps_kyoso の表）。

座標の単位は pt。top・bottom はページの上からの距離（pdfplumber と同じ向き）。罫線は細い四角形（幅か高さが0.5pt）。
JSON の形：{"pages": [{"number": n, "rects": [[x0, top, x1, bottom], …], "chars": [[text, x0, top, x1, bottom, size], …]}]}

使い方：リポジトリの直下で `.venv/bin/python tests/fixtures/make_pdf_cells.py`。
tests/test_fixtures.py が、置いたファイルがこのスクリプトの出力と同じであることを確かめる。
期待値（行の対応・項目の照合の結果）は、テストを書く人がこの表から手で決めてテストに書く。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "pdf_cells"

SIZE = 10.0  # 文字の大きさ
PITCH = 12.0  # セルの中の行の間隔
PAD = 2.0  # セルの内側の余白
LEFT = 20.0
TOP = 30.0
RULE = 0.5  # 罫線の太さ
KANGXI = "⼊"  # U+2F0A（NFKC で「入」）。別のフォントで描かれ、上端が下にずれる
KANGXI_SHIFT = 1.51
PDF_SUBST = {"?": "−"}  # CSV の `?` は、PDF では元の文字（アダプタ仕様2章）

KYOSO_WIDTHS = (24, 110, 70, 40, 50, 90, 40, 75, 90, 55, 70, 35, 25, 25, 25, 40)
ZUIKEI_WIDTHS = (24, 110, 70, 40, 50, 90, 40, 75, 150, 55, 70, 35, 25, 25, 25, 25, 40)
ROWS_PER_PAGE = 5


def _load_csv_tables():
    spec = importlib.util.spec_from_file_location(
        "make_cao_fixtures", HERE / "make_cao_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CSV = _load_csv_tables()


def _r(value: float) -> float:
    return round(value, 2)


def char_width(ch: str) -> float:
    return SIZE / 2 if ch.isascii() else SIZE


def wrap(text: str, width: float) -> list[str]:
    """セルの文字列を、改行と列の幅で行に分ける。"""
    lines: list[str] = []
    for part in text.split("\n"):
        line, used = "", 0.0
        for ch in part:
            w = char_width(ch)
            if line and used + w > width:
                lines.append(line)
                line, used = "", 0.0
            line += ch
            used += w
        lines.append(line)
    return lines


class Page:
    def __init__(self, number: int, widths: Sequence[float]) -> None:
        self.number = number
        self.xs = [LEFT]
        for w in widths:
            self.xs.append(self.xs[-1] + w)
        self.y = TOP
        self.bounds = [TOP]
        self.chars: list[list] = []
        self.inner: list[list[float]] = []

    @property
    def columns(self) -> int:
        return len(self.xs) - 1

    def row(self, cells: Sequence[str], *, inner_line_col: int | None = None) -> None:
        assert len(cells) == self.columns, (len(cells), self.columns)
        laid = [wrap(cell, self.xs[i + 1] - self.xs[i] - 2 * PAD) for i, cell in enumerate(cells)]
        height = max(len(lines) for lines in laid) * PITCH + 2 * PAD
        top = self.y
        for i, lines in enumerate(laid):
            for j, line in enumerate(lines):
                x = self.xs[i] + PAD
                y = top + PAD + j * PITCH
                for ch in line:
                    w = char_width(ch)
                    if ch.strip():
                        dy = KANGXI_SHIFT if ch == KANGXI else 0.0
                        self.chars.append(
                            [ch, _r(x), _r(y + dy), _r(x + w), _r(y + dy + SIZE), SIZE]
                        )
                    x += w
        if inner_line_col is not None:
            # セルの中だけにある横線（NO の列を横切らない）
            mid = top + height / 2
            c = inner_line_col
            self.inner.append(
                [_r(self.xs[c] + 1), _r(mid - RULE / 2), _r(self.xs[c + 1] - 1), _r(mid + RULE / 2)]
            )
        self.y = top + height
        self.bounds.append(self.y)

    def to_json(self) -> dict:
        rects = [[_r(x - RULE / 2), TOP, _r(x + RULE / 2), _r(self.y)] for x in self.xs]
        rects += [[LEFT, _r(y - RULE / 2), _r(self.xs[-1]), _r(y + RULE / 2)] for y in self.bounds]
        rects += self.inner
        return {"number": self.number, "rects": rects, "chars": self.chars}


def header_cells(header: tuple[list[str], list[str]], columns: int) -> list[str]:
    """CSV の見出しの2行を、PDF の1行の見出しのセルにする（列ごとに縦に連結）。"""
    top, bottom = header
    return ["\n".join(part for part in (top[i], bottom[i]) if part) for i in range(columns)]


def pdf_cell(text: str) -> str:
    for old, new in PDF_SUBST.items():
        text = text.replace(old, new)
    return text


def mini(
    rows: list[list[str]],
    header: tuple[list[str], list[str]],
    widths: Sequence[float],
    *,
    title_subst: dict[str, tuple[str, str]] | None = None,
    inner_lines: dict[str, int] | None = None,
) -> dict:
    """合成の CSV の表を、PDF のページ（1ページに5件）にする。"""
    columns = len(widths)
    pages: list[Page] = []
    for start in range(0, len(rows), ROWS_PER_PAGE):
        page = Page(len(pages) + 1, widths)
        page.row(header_cells(header, columns))
        for row in rows[start : start + ROWS_PER_PAGE]:
            cells = [pdf_cell(c) for c in row[:columns]]
            no = row[0]
            if title_subst and no in title_subst:
                old, new = title_subst[no]
                cells[1] = cells[1].replace(old, new)
            page.row(cells, inner_line_col=(inner_lines or {}).get(no))
        pages.append(page)
    return {"pages": [p.to_json() for p in pages]}


# ---------------------------------------------------------------------------
# 罠（PD-1・PD-2）。随意契約の17列の形
# ---------------------------------------------------------------------------

LONG_REASON = (
    "会計法第29条の3第4項\n"
    "本業務は、令和６年度に実施する調査研究の継続であり、前年度の調査で収集したデータと分析の手法を"
    "引き継ぐ必要がある。前年度の受託者は、当該データの構造と分析のための独自の手順を有しており、"
    "他の者が同等の品質で短期間に業務を行うことはできない。また、調査の対象機関との調整も前年度から"
    "継続しており、受託者を変更すると調整をやり直す必要が生じ、年度内に成果を得ることができない。"
    "このため、契約の性質又は目的が競争を許さない場合に該当する。"
)
R_SEISHITSU = "会計法第29条の3第4項\n契約の性質又は目的が競争を許さない場合"
R_KOBO = "会計法第29条の3第4項\n公募"
R_FUCHO = "予算決算及び会計令第99条の2\n（不調）"
# 担当官の列に、架空の氏名を縦に1文字ずつ並べる（架空・太郎、仮名・花子）
OFF_VERTICAL_1 = "支出負担行為担当官\n内閣府大臣官房会計担当参事官\n架\n空\n太\n郎"
OFF_VERTICAL_2 = "支出負担行為担当官\n内閣府大臣官房会計担当参事官\n仮\n名\n花\n子"
OFF_ADDR = CSV.OFFICER_ADDR
CP_ADDR = CSV.CP_ADDR

ZUIKEI_HEADER_A = [
    "ＮＯ",
    "物品役務等の名称及び数量",
    "契約担当官等の氏名並びにその所属する部局の名称及び所在地",
    "",
    "契約を締結した日",
    "契約の相手方の商号又は名称及び住所",
    "",
    "法人番号",
    "随意契約によることとした\n会計法令の根拠条文及び理由",
    "予定価格",
    "契約金額",
    "落札率",
    "再就職の\n役員の数",
    "公益法人の場合",
    "",
    "",
    "備考",
]
# 見出しの2行目：NO のセルは空。照合に使う列（8列目）の文字は期待する見出しの文字列の一部
ZUIKEI_HEADER_B = [
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "（企画競争又は公募）",
    "",
    "",
    "",
    "",
    "公益法人の区分",
    "国所管、都道府県所管の区分",
    "応札・応募者数",
    "",
]
ZUIKEI_HEADER_1ROW = header_cells((ZUIKEI_HEADER_A, ZUIKEI_HEADER_B), 17)


def zrow(
    no: str,
    title: str,
    date: str,
    cp: str,
    corp: str,
    reason: str,
    estimate: str,
    amount: str,
    *,
    officer: str = CSV.OFF_STD,
    note: str = "",
) -> list[str]:
    return [
        no,
        title,
        officer,
        OFF_ADDR,
        date,
        cp,
        CP_ADDR,
        corp,
        reason,
        estimate,
        amount,
        "-",
        "0",
        "",
        "",
        "",
        note,
    ]


def traps_zuikei() -> dict:
    """PD-1・PD-2 の罠。ページ1：見出し2行・複数行のセル・セルの中の横線・縦の氏名・康熙部首・金額の罠。
    ページ2：見出し1行・数値のない金額のセル・ページの最後の件（NO 7）。
    ページ3：見出しの直後に前のページから続く行（NO が空で件名に文字）・NO 8。
    ページ4：列が15（使わない）・NO 9。ページ5：同じ NO の行が2つ（NO 10 は契約日が違う、NO 11 は同じ）。
    """
    pages: list[Page] = []

    p1 = Page(1, ZUIKEI_WIDTHS)
    p1.row(ZUIKEI_HEADER_A)
    p1.row(ZUIKEI_HEADER_B)
    p1.row(
        zrow(
            "1",
            "調査研究委託\n（第２期）",
            "R6.4.1",
            "株式会社テスト総研",
            "0000000000005",
            LONG_REASON,
            "10,000,000",
            "9,800,000",
            officer=OFF_VERTICAL_1,
            note="備考の記載",
        ),
        inner_line_col=16,
    )
    p1.row(
        zrow(
            "2",
            f"物品の購{KANGXI}",
            "R6.5.1",
            "テスト商会",
            "1.00000E+12",
            R_FUCHO,
            "1,000,000",
            "11,000,000",
            officer=OFF_VERTICAL_2,
        )
    )
    p1.row(
        zrow(
            "3",
            "通訳業務",
            "R6.7.1",
            "株式会社テスト通訳",
            "0000000000010",
            R_SEISHITSU,
            "-",
            "単価　1時間当たり　8,000円",
        )
    )
    p1.row(
        zrow(
            "4",
            "物品の購入（外国製）",
            "R6.11.4",
            "テスト商会",
            "0000000000012",
            R_FUCHO,
            "-",
            "12,345ドル",
        )
    )
    p1.row(
        zrow(
            "5",
            "調査委託",
            "R6.6.3",
            "（公財）テスト−研究所",
            "0000000000014",
            R_KOBO,
            "2,000,000",
            "1,950,000",
        )
    )
    pages.append(p1)

    p2 = Page(2, ZUIKEI_WIDTHS)
    p2.row(ZUIKEI_HEADER_1ROW)
    p2.row(zrow("6", "刊行物の購入", "R6.8.1", "株式会社テスト書店", "-", R_SEISHITSU, "-", "-"))
    p2.row(
        zrow(
            "7",
            "保守点検業務",
            "R6.9.1",
            "株式会社テスト技研",
            "0000000000015",
            R_KOBO,
            "800,000",
            "780,000",
        )
    )
    pages.append(p2)

    p3 = Page(3, ZUIKEI_WIDTHS)
    p3.row(ZUIKEI_HEADER_1ROW)
    continued = [""] * 17
    continued[1] = "（保守点検業務の続き）"
    p3.row(continued)
    p3.row(
        zrow(
            "8",
            "清掃業務",
            "R6.10.1",
            "株式会社テスト清掃",
            "0000000000016",
            R_KOBO,
            "500,000",
            "480,000",
        )
    )
    pages.append(p3)

    # 列が15のページ（13・14列目がない）
    drop = {13, 14}
    widths15 = [w for i, w in enumerate(ZUIKEI_WIDTHS) if i not in drop]
    p4 = Page(4, widths15)
    p4.row([c for i, c in enumerate(ZUIKEI_HEADER_1ROW) if i not in drop])
    row9 = zrow(
        "9", "印刷業務", "R6.12.1", "株式会社テスト印刷", "0000000000017", R_KOBO, "-", "200,000"
    )
    p4.row([c for i, c in enumerate(row9) if i not in drop])
    pages.append(p4)

    p5 = Page(5, ZUIKEI_WIDTHS)
    p5.row(ZUIKEI_HEADER_1ROW)
    for no, date, amount in (
        ("10", "R6.10.1", "300,000"),
        ("10", "R6.10.2", "310,000"),
        ("11", "R6.11.1", "100,000"),
        ("11", "R6.11.1", "120,000"),
    ):
        title = "会場借上げ" if no == "10" else "備品の購入"
        p5.row(zrow(no, title, date, "株式会社テスト会館", "0000000000004", R_KOBO, "-", amount))
    pages.append(p5)
    return {"pages": [p.to_json() for p in pages]}


def krow(
    no: str,
    title: str,
    date: str,
    cp: str,
    corp: str,
    method: str,
    estimate: str,
    amount: str,
    rate: str,
) -> list[str]:
    return [
        no,
        title,
        CSV.OFF_STD,
        OFF_ADDR,
        date,
        cp,
        CP_ADDR,
        corp,
        method,
        estimate,
        amount,
        rate,
        "",
        "",
        "",
        "",
    ]


def traps_kyoso() -> dict:
    """PD-2 の罠（競争入札の16列）。NO 1：方式の単価、予定価格の列にも数値。NO 2：契約日のセルが R6.4.10。"""
    page = Page(1, KYOSO_WIDTHS)
    page.row(header_cells(CSV.KYOSO_HEADER, 16))
    page.row(
        krow(
            "1",
            "荷物等の配送業務",
            "R6.4.1",
            "株式会社テスト配送",
            "0000000000001",
            "一般競争（単価）",
            "1,100,000",
            "1,000,000",
            "90.9%",
        )
    )
    page.row(
        krow(
            "2",
            "庁舎清掃業務",
            "R6.4.10",
            "株式会社テスト清掃",
            "0000000000002\n0000000000003",
            "一般競争（総合評価）",
            "公表しない",
            "2,000,000",
            "-",
        )
    )
    return {"pages": [page.to_json()]}


def outputs() -> dict[str, bytes]:
    docs = {
        "kyoso_2024_mini.json": mini(CSV.KYOSO_2024, CSV.KYOSO_HEADER, KYOSO_WIDTHS),
        "kyoso_2025_mini.json": mini(CSV.KYOSO_2025, CSV.KYOSO_HEADER, KYOSO_WIDTHS),
        "zuikei_2024_mini.json": mini(CSV.ZUIKEI_2024, CSV.ZUIKEI_HEADER_17, ZUIKEI_WIDTHS),
        "zuikei_2025_mini.json": mini(
            CSV.ZUIKEI_2025,
            CSV.ZUIKEI_HEADER_27,
            ZUIKEI_WIDTHS,
            title_subst={"6": ("入", KANGXI)},
            inner_lines={"2": 16},
        ),
        "traps_zuikei.json": traps_zuikei(),
        "traps_kyoso.json": traps_kyoso(),
    }
    return {
        name: (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for name, doc in docs.items()
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in outputs().items():
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
