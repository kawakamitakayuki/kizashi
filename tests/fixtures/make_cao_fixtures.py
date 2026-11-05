"""内閣府の公表データの形をまねた合成の CSV を作る（test_plan.md の1章、アダプタ仕様11章）。

実データは使わない（D-007 が決まるまで）。受注者名と法人番号は明らかな架空の値、担当官の氏名も
架空の値にする。出力：datasets/cao-procurement/fixtures/ の
kyoso_2024_mini.csv・kyoso_2025_mini.csv・zuikei_2024_mini.csv・zuikei_2025_mini.csv・kyoso_bad_header.csv
（cp932、CRLF。表題2行・見出し2行は実データと同じ形）。

使い方：リポジトリの直下で `.venv/bin/python tests/fixtures/make_cao_fixtures.py`。
tests/test_fixtures.py が、置いたファイルがこのスクリプトの出力と同じであることを確かめる。
期待値（件数・合計など）は、テストを書く人がこの表から手で数えてテストに書く。
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "datasets" / "cao-procurement" / "fixtures"

TITLE_KYOSO = (
    "公共調達の適正化について（平成18年８月25日付財計第2017号）に基づく競争入札に係る情報の公表"
    "（物品・役務等）、\n（テスト用の合成データ。実在の契約ではない）"
)
TITLE_ZUIKEI = (
    "公共調達の適正化について（平成18年８月25日付財計第2017号）に基づく随意契約に係る情報の公表"
    "（物品・役務等）、\n（テスト用の合成データ。実在の契約ではない）"
)

# 担当官のセル（役職・所属・架空の氏名）。氏名の語：架空・太郎・仮名・花子・見本・三郎・空想・四郎・試験次郎
OFF_STD = "支出負担行為担当官\n内閣府大臣官房会計担当参事官\n架空　太郎"
OFF_BUNNIN = "分任支出負担行為担当官\n内閣府政策統括官（防災担当）付参事官\n仮名　花子"
OFF_TWO = (
    "支出負担行為担当官\n内閣府大臣官房会計担当参事官\n架空　太郎\n"
    "支出負担行為担当官代理\n食品安全委員会事務局総務課長\n見本　三郎"
)
OFF_ZENKAKU = "支出負担行為担当官　内閣府大臣官房会計担当参事官　架空　太郎"
OFF_EISEI = "支出負担行為担当官\n内閣衛星情報センター所長\n空想　四郎"
OFF_2WORDS = "支出負担行為担当官\n食品安全委員会事務局長\n試験次郎"

# 取り込まない列の値（台帳に残らないことを確かめる語：官庁町・模擬市・会計第一係）
OFFICER_ADDR = "テスト県架空市官庁町１－１"
CP_ADDR = "テスト県模擬市本町２－２"
ZUIKEI_INTERNAL = ["役務", "", "○", "3", "1", "会計第一係", "大臣官房", "", "", ""]

KYOSO_HEADER = (
    [
        "ＮＯ",
        "物品役務等の名称及び数量",
        "契約担当官等の氏名並びにその所属する部局の名称及び所在地",
        "",
        "契約を締結した日",
        "契約の相手方の商号又は名称及び住所",
        "",
        "法人番号",
        "一般競争入札・指名競争入札の別（総合評価の実施）",
        "予定価格",
        "契約金額",
        "落札率",
        "公益法人の場合",
        "",
        "",
        "備考",
    ],
    [
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "公益法人の区分",
        "国所管、都道府県所管の区分",
        "応札・応募者数",
        "",
    ],
)
ZUIKEI_HEADER_17 = (
    [
        "ＮＯ",
        "物品役務等の名称及び数量",
        "契約担当官等の氏名並びにその所属する部局の名称及び所在地",
        "",
        "契約を締結した日",
        "契約の相手方の商号又は名称及び住所",
        "",
        "法人番号",
        "随意契約によることとした\n会計法令の根拠条文及び理由\n（企画競争又は公募）",
        "予定価格",
        "契約金額",
        "落札率",
        "再就職の\n役員の数",
        "公益法人の場合",
        "",
        "",
        "備考",
    ],
    [
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "公益法人の区分",
        "国所管、都道府県所管の区分",
        "応札・応募者数",
        "",
    ],
)
ZUIKEI_HEADER_27 = (
    ZUIKEI_HEADER_17[0]
    + [
        "契約内容\n【該当するものをプルダウンから選択】",
        '電子応札\n【未対応案件であれば"○"を選択】',
        "電子契約\n【対応案件であれば○を選択】",
        "応札者数\n【数字を記入】",
        "",
        "発議係",
        "部局",
        "",
        "",
        "",
    ],
    ZUIKEI_HEADER_17[1] + ["", "", "", "", "うち電子応札者数", "", "名称", "", "", ""],
)


def kyoso(no, title, officer, date, cp, corp, method, estimate, amount, rate, note=""):
    return [
        no,
        title,
        officer,
        OFFICER_ADDR,
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
        note,
    ]


def zuikei(
    no, title, officer, date, cp, corp, reason, estimate, amount, rate, note="", internal=None
):
    row = [
        no,
        title,
        officer,
        OFFICER_ADDR,
        date,
        cp,
        CP_ADDR,
        corp,
        reason,
        estimate,
        amount,
        rate,
        "0",
        "",
        "",
        "",
        note,
    ]
    return row + (internal or [])


KYOSO_2024 = [
    kyoso(
        "1",
        "令和６年度庁舎清掃業務",
        OFF_STD,
        "R6.4.1",
        "株式会社テスト清掃",
        "1.00000E+12",
        "一般競争",
        "1,300,000",
        "1,200,000",
        "92.3%",
    ),
    kyoso(
        "2",
        "荷物等の配送業務",
        OFF_STD,
        "R6.4.1",
        "株式会社テスト配送",
        "0000000000001",
        "一般競争（単価）",
        "-",
        "3,000,000",
        "-",
        "単価契約",
    ),
    kyoso(
        "3",
        "システム保守業務\n（第３期）",
        OFF_BUNNIN,
        "令和6年5月10日",
        "株式会社テストシステム",
        "0000000000002\n0000000000003",
        "一般競争（総合評価）",
        "5,500,000",
        "5,000,000",
        "90.9%",
    ),
    kyoso(
        "4",
        "共同調達に係る消耗品の購入",
        OFF_TWO,
        "R6.6.3",
        "株式会社テスト商事",
        "-",
        "一般競争",
        "公表しない",
        "800,000",
        "-",
    ),
    kyoso(
        "5",
        "4月分　複写機保守",
        OFF_ZENKAKU,
        "R6.4.30",
        "株式会社テスト事務機",
        "1.00000E+12",
        "一般競争（単価）",
        "-",
        "120,000",
        "-",
    ),
    kyoso(
        "6",
        "広報誌印刷業務（令和６年度）",
        OFF_STD,
        "R6.7.1",
        "株式会社テスト印刷",
        "1.00000E+12",
        "一般競争（総合評価・単価）",
        "2,000,000",
        "1,800,000",
        "90.0%",
    ),
    kyoso(
        "7",
        "研修会場借上げ",
        OFF_EISEI,
        "R6.8.1",
        "株式会社テスト会館",
        "0000000000004",
        "一般競争",
        "600,000",
        "500,000",
        "83.3%",
    ),
    kyoso(
        "8",
        "消耗品の購入（単価契約）",
        OFF_STD,
        "R6.9.1",
        "株式会社テスト文具",
        "1.00000E+12",
        "一般競争",
        "-",
        "単価契約\n１件当たり　250円",
        "-",
    ),
    kyoso(
        "9",
        "会議録作成業務",
        OFF_STD,
        "R6.13.1",
        "株式会社テスト速記",
        "12345",
        "一般競争",
        "400,000",
        "350,000",
        "87.5%",
    ),
    kyoso(
        "10",
        "庁舎警備業務",
        OFF_STD,
        "R6.10.1",
        "株式会社テスト?警備",
        "1.00000E+12",
        "一般競争",
        "3,000,000",
        "2,700,000",
        "90.0%",
    ),
    kyoso(
        "11",
        "12か月間　車両運行管理業務",
        OFF_STD,
        "R6.4.1",
        "株式会社テスト交通",
        "1.00000E+12",
        "一般競争",
        "9,000,000",
        "8,400,000",
        "93.3%",
    ),
    kyoso(
        "12",
        "翻訳業務",
        OFF_2WORDS,
        "R7.3.31",
        "株式会社テスト翻訳",
        "1.00000E+12",
        "一般競争",
        "700,000",
        "650,000",
        "92.9%",
    ),
]

_DUP = (
    "物品の購入",
    OFF_STD,
    "R7.5.1",
    "株式会社テスト商事",
    "1.00000E+12",
    "一般競争",
    "1,000,000",
    "900,000",
    "90.0%",
)
KYOSO_2025 = [
    kyoso(
        "1",
        "令和7年度庁舎清掃業務",
        OFF_STD,
        "R7.4.1",
        "株式会社テスト清掃",
        "1.00000E+12",
        "一般競争",
        "1,600,000",
        "1,500,000",
        "93.8%",
    ),
    kyoso(
        "2",
        "荷物等の配送業務",
        OFF_STD,
        "R7.4.1",
        "株式会社テスト配送",
        "0000000000001",
        "一般競争",
        "3,500,000",
        "3,300,000",
        "94.3%",
    ),
    kyoso(
        "3",
        "広報誌印刷業務（令和７年度）",
        OFF_STD,
        "R7.7.1",
        "株式会社テスト印刷",
        "1.00000E+12",
        "一般競争（総合評価）",
        "2,100,000",
        "1,900,000",
        "90.5%",
    ),
    kyoso(
        "4",
        "研修会場借上げ",
        OFF_EISEI,
        "R7.8.1",
        "株式会社テスト会館",
        "0000000000004",
        "一般競争",
        "600,000",
        "500,000",
        "83.3%",
    ),
    kyoso("5", *_DUP),
    kyoso("6", *_DUP),
    kyoso(
        "7",
        "翻訳業務",
        OFF_STD,
        "R7.6.1",
        "株式会社テスト翻訳",
        "1.00000E+12",
        "一般競争",
        "800,000",
        "750,000",
        "93.8%",
    ),
    kyoso(
        "8",
        "会議録作成業務",
        OFF_STD,
        "R7.9.1",
        "株式会社テスト速記",
        "1.00000E+12",
        "一般競争",
        "400,000",
        "380,000",
        "95.0%",
    ),
    kyoso(
        "9",
        "庁舎警備業務",
        OFF_STD,
        "R7.10.1",
        "株式会社テスト?警備",
        "1.00000E+12",
        "一般競争",
        "3,000,000",
        "2,900,000",
        "96.7%",
    ),
    kyoso(
        "10",
        "車両運行管理業務（第２期）",
        OFF_STD,
        "R7.4.1",
        "株式会社テスト交通",
        "1.00000E+12",
        "一般競争",
        "9,500,000",
        "8,800,000",
        "92.6%",
    ),
    kyoso(
        "11",
        "複写機保守",
        OFF_EISEI,
        "R7.4.30",
        "株式会社テスト事務機",
        "1.00000E+12",
        "一般競争（単価）",
        "-",
        "130,000",
        "-",
    ),
    kyoso(
        "12",
        "消耗品の購入（単価契約）",
        OFF_STD,
        "R8.3.31",
        "株式会社テスト文具",
        "1.00000E+12",
        "一般競争（単価）",
        "-",
        "300,000",
        "-",
    ),
]

R_KOBO = "会計法第29条の3第4項\n公募"
R_KIKAKU = "会計法第29条の3第4項\n企画競争"
R_FUCHO = "予算決算及び会計令第99条の2\n（不調）"
R_SEISHITSU = "会計法第29条の3第4項\n契約の性質又は目的が競争を許さない場合"
R_JIZEN = "予算決算及び会計令99条の5\n慈善のため設立した救済施設から物件を買い入れるとき"
R_OTHER = "会計法第29条の3第5項\n予決令第99条第2号"
R_KAIKEIHOREI = "会計法令第99条の2\n企画競争"

ZUIKEI_2024 = [
    zuikei(
        "1",
        "調査研究委託",
        OFF_STD,
        "R6.4.1",
        "株式会社テスト総研",
        "0000000000005",
        R_KOBO,
        "10,000,000",
        "9,800,000",
        "98.0%",
    ),
    zuikei(
        "2",
        "統計データ作成業務",
        OFF_STD,
        "R6.5.1",
        "一般財団法人テスト協会",
        "0000000000006",
        R_KIKAKU,
        "-",
        "4,000,000",
        "-",
    ),
    zuikei(
        "3",
        "物品の購入",
        OFF_STD,
        "令和6年6月1日",
        "テスト商会",
        "1.00000E+12",
        R_FUCHO,
        "-",
        "12,345ドル",
        "-",
    ),
    zuikei(
        "4",
        "通訳業務",
        OFF_TWO,
        "R6.7.1",
        "株式会社テスト通訳",
        "1.00000E+12",
        R_SEISHITSU,
        "-",
        "単価　1時間当たり　8,000円",
        "-",
    ),
    zuikei(
        "5",
        "救済施設からの物品購入",
        OFF_STD,
        "R6.8.1",
        "社会福祉法人テスト園",
        "0000000000007",
        R_JIZEN,
        "300,000",
        "290,000",
        "96.7%",
    ),
    zuikei(
        "6",
        "業務用車両の賃借",
        OFF_STD,
        "R6.9.1",
        "株式会社テストリース",
        "1.00000E+12",
        R_OTHER,
        "1,500,000",
        "1,400,000",
        "93.3%",
    ),
    zuikei(
        "7",
        "保守点検業務",
        OFF_BUNNIN,
        "R6.10.1",
        "株式会社テスト技研",
        "-",
        R_KAIKEIHOREI,
        "-",
        "9ヵ月＠100,000円×3台ほか",
        "-",
    ),
    zuikei(
        "8",
        "職員研修業務",
        OFF_STD,
        "R6.11.1",
        "株式会社テスト教育",
        "0000000000008\n0000000000009",
        R_KOBO,
        "2,000,000",
        "1,950,000",
        "97.5%",
    ),
    zuikei(
        "9",
        "刊行物の購入",
        OFF_STD,
        "R6.12.1",
        "株式会社テスト書店",
        "法人番号なし",
        R_SEISHITSU,
        "-",
        "50,000",
        "-",
    ),
]

ZUIKEI_2025 = [
    zuikei(
        "1",
        "調査研究委託",
        OFF_STD,
        "R7.4.1",
        "株式会社テスト総研",
        "0000000000005",
        R_KIKAKU,
        "10,500,000",
        "10,000,000",
        "95.2%",
        internal=ZUIKEI_INTERNAL,
    ),
    zuikei(
        "2",
        "システム保守業務（第４期）",
        OFF_BUNNIN,
        "R7.4.1",
        "株式会社テストシステム",
        "0000000000002",
        R_SEISHITSU,
        "5,200,000",
        "5,100,000",
        "98.1%",
        internal=ZUIKEI_INTERNAL,
    ),
    zuikei(
        "3",
        "統計データ作成業務",
        OFF_STD,
        "R7.5.1",
        "一般財団法人テスト協会",
        "0000000000006",
        R_KIKAKU,
        "-",
        "4,200,000",
        "-",
        internal=ZUIKEI_INTERNAL,
    ),
    zuikei(
        "4",
        "通訳業務",
        OFF_STD,
        "R7.7.1",
        "株式会社テスト通訳",
        "0000000000010",
        R_SEISHITSU,
        "-",
        "単価　1時間当たり　8,500円",
        "-",
        internal=ZUIKEI_INTERNAL,
    ),
    zuikei(
        "5",
        "業務用車両の賃借",
        OFF_STD,
        "R7.9.1",
        "株式会社テストリース",
        "0000000000011",
        R_OTHER,
        "1,500,000",
        "1,450,000",
        "96.7%",
        internal=ZUIKEI_INTERNAL,
    ),
    zuikei(
        "6",
        "物品の購入（外国製）",
        OFF_STD,
        "R7.11.4",
        "テスト商会",
        "0000000000012",
        R_FUCHO,
        "-",
        "USD 10,000",
        "-",
        internal=ZUIKEI_INTERNAL,
    ),
    zuikei(
        "7",
        "職員研修業務",
        OFF_STD,
        "R8.3.31",
        "株式会社テスト教育",
        "0000000000013",
        R_KOBO,
        "2,100,000",
        "2,000,000",
        "95.2%",
        internal=ZUIKEI_INTERNAL,
    ),
]


def _preamble(title: str, width: int, created_col: int, created: str) -> list[list[str]]:
    first = [title] + [""] * (width - 1)
    second = [""] * width
    second[created_col] = created
    return [first, second]


def build(
    rows: list[list[str]],
    header: tuple[list[str], list[str]],
    title: str,
    created: str,
    created_col: int,
) -> bytes:
    width = len(header[0])
    assert all(len(r) == width for r in rows), "列の数が見出しと違う行がある"
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerows(_preamble(title, width, created_col, created))
    writer.writerows(header)
    writer.writerows(rows)
    return buf.getvalue().encode("cp932")


def outputs() -> dict[str, bytes]:
    bad_header = (list(KYOSO_HEADER[0]), list(KYOSO_HEADER[1]))
    bad_header[0][10] = "契約金額（税込）"
    return {
        "kyoso_2024_mini.csv": build(
            KYOSO_2024, KYOSO_HEADER, TITLE_KYOSO, "令和７年７月11日現在", 15
        ),
        "kyoso_2025_mini.csv": build(
            KYOSO_2025, KYOSO_HEADER, TITLE_KYOSO, "令和８年５月12日現在", 15
        ),
        "zuikei_2024_mini.csv": build(
            ZUIKEI_2024, ZUIKEI_HEADER_17, TITLE_ZUIKEI, "令和７年５月14日現在", 16
        ),
        "zuikei_2025_mini.csv": build(
            ZUIKEI_2025, ZUIKEI_HEADER_27, TITLE_ZUIKEI, "令和８年５月12日現在", 16
        ),
        "kyoso_bad_header.csv": build(
            KYOSO_2024, bad_header, TITLE_KYOSO, "令和７年７月11日現在", 15
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in outputs().items():
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
