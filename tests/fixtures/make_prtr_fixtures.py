"""PRTR の合成のテスト用データ（datasets/prtr/fixtures/）を作る（P1-c。docs/adapter_prtr.md 12章、test_plan.md の PR-1）。

本紙（23列）と別紙（41列）の形をまねた2年度分（排出年度2023・2024）。cp932、各項目を `"` で囲み、`,` で区切り、
行の区切りは CR＋LF、見出しの行はない。実データは使わない（D-007）。名称・氏名・住所・法人番号・物質は架空の値で、
物質の管理番号 998・999 は実在しない（令和5・6年度の実データの管理番号は 1〜754）。

実行：uv run python tests/fixtures/make_prtr_fixtures.py（出力を datasets/prtr/fixtures/ に書く）
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "datasets" / "prtr" / "fixtures"

SUB_A = ("テスト物質甲", "999")
SUB_B = ("テスト物質乙", "998")
KG, TEQ = "1", "2"

# 届出者（本紙5〜10列）。取り込まない列にだけ置く語：氏名は架空・太郎など、住所は模擬市・官庁町
REPORTER_CORP = ("株式会社テスト化学", "架空 太郎")
REPORTER_PERSON = ("見本 三郎", "")  # 個人の届出者（氏名が5列目に入る）


def honshi(
    seiri: str,
    year: int,
    corp: str,
    pref: str,
    city: str,
    industry: tuple[str, str],
    substances: int,
    *,
    reporter: tuple[str, str] = REPORTER_CORP,
    facility: str = "株式会社テスト化学 第一工場",
) -> list[str]:
    """本紙の1行（23列。docs/adapter_prtr.md 3.1）。"""
    name, representative = reporter
    return [
        seiri,  # 1 整理番号
        str(year),  # 2 排出年度
        "経済産業大臣",  # 3 届出先
        "東京都知事",  # 4 提出先
        name,  # 5 届出者：氏名（法人にあっては名称）
        representative,  # 6 届出者：代表者の氏名
        "100-0000",  # 7 届出者：郵便番号
        "東京都",  # 8 届出者：都道府県
        "模擬市",  # 9 届出者：市区町村
        "官庁町1-1",  # 10 届出者：町域名
        name,  # 11 事業者の名称
        corp,  # 12 法人番号
        name,  # 13 前回の事業者の名称
        facility,  # 14 事業所の名称
        facility,  # 15 前回の事業所の名称
        "100-0001",  # 16 事業所：郵便番号
        pref,  # 17 事業所：都道府県
        city,  # 18 事業所：市区町村
        "見本町2-2",  # 19 事業所：町域名
        "50",  # 20 従業員の数
        industry[1],  # 21 業種名
        industry[0],  # 22 業種コード
        str(substances),  # 23 届出物質数
    ]


def besshi(seiri: str, sheet: int, substance: tuple[str, str], unit: str, air: str) -> list[str]:
    """別紙の1行（41列。docs/adapter_prtr.md 3.2）。大気以外の量は 0.0。"""
    return [
        seiri,  # 1 整理番号
        str(sheet),  # 2 別紙番号
        substance[0],  # 3 物質の名称
        substance[1],  # 4 管理番号
        unit,  # 5 単位
        air,  # 6 大気
        "0.0",  # 7 公共用水域
        "テスト川",  # 8 排出先の河川等の名称
        "0.0",  # 9 土壌
        "0.0",  # 10 埋立
        "2",  # 11 安定型
        "2",  # 12 管理型
        "2",  # 13 遮断型
        "0.0",  # 14 下水道
        "テスト浄化センター",  # 15 下水道終末処理施設の名称
        "0.0",  # 16 事業所の外
        *["2"] * 25,  # 17〜41 廃棄物の処理方法・種類
    ]


CHEM = ("2000", "化学工業")
PLASTIC = ("2100", "プラスチック製品製造業")
METAL = ("2200", "金属製品製造業")


def year_rows(year: int) -> tuple[list[list[str]], list[list[str]]]:
    """排出年度ごとの本紙と別紙。整理番号は年度ごとに別の値（実データでも年度をまたいで同じ番号はない）。"""
    base = 0 if year == 2023 else 100

    def sn(i: int, prefix: str = "E") -> str:
        return f"{prefix}{base + i:07d}-00001-01"

    first = year == 2023
    h = [
        # 1：キーに1事業所。物質甲は増加（10.5 → 12.0）、物質乙は変わらない（3.0）
        honshi(sn(1), year, "0000000000001", "東京都", "テスト市", CHEM, 2),
        # 2・3：同じ法人・市区町村・業種に2事業所（同じ事業所と決められないので比べない）
        honshi(sn(2), year, "0000000000002", "東京都", "テスト市", CHEM, 1),
        honshi(sn(3), year, "0000000000002", "東京都", "テスト市", CHEM, 1, facility="第二工場"),
        # 4：法人番号が空欄の個人事業者（年度間を結ばない）
        honshi(
            sn(4),
            year,
            "",
            "東京都",
            "テスト市",
            CHEM,
            1,
            reporter=REPORTER_PERSON,
            facility="見本三郎商店",
        ),
        # 5：ほかの都道府県
        honshi(sn(5), year, "0000000000005", "大阪府", "テスト市", CHEM, 1),
        # 6：物質甲は令和5年度だけ（令和6年度は物質乙だけ）
        honshi(sn(6), year, "0000000000006", "東京都", "テスト町", CHEM, 1),
        # 7：単位 mg-TEQ。減少（0.5 → 0.25）
        honshi(sn(7, "P"), year, "0000000000007", "東京都", "テスト市", PLASTIC, 1),
        # 8：令和6年度の大気が空欄
        honshi(sn(8), year, "0000000000008", "東京都", "テスト市", METAL, 1),
    ]
    b = [
        besshi(sn(1), 1, SUB_A, KG, "10.5" if first else "12.0"),
        besshi(sn(1), 2, SUB_B, KG, "3.0"),
        besshi(sn(2), 1, SUB_A, KG, "5.0" if first else "7.0"),
        besshi(sn(3), 1, SUB_A, KG, "5.0" if first else "1.0"),
        besshi(sn(4), 1, SUB_A, KG, "100.0" if first else "200.0"),
        besshi(sn(5), 1, SUB_A, KG, "4.0" if first else "9.0"),
        besshi(sn(6), 1, SUB_A if first else SUB_B, KG, "2.0"),
        besshi(sn(7, "P"), 1, SUB_A, TEQ, "0.5" if first else "0.25"),
        besshi(sn(8), 1, SUB_A, KG, "6.0" if first else ""),
    ]
    return h, b


def encode(rows: list[list[str]]) -> bytes:
    buf = io.StringIO(newline="")
    csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n").writerows(rows)
    return buf.getvalue().encode("cp932")


def outputs() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for year in (2023, 2024):
        h, b = year_rows(year)
        files[f"honshi_{year}_mini.txt"] = encode(h)
        files[f"besshi_{year}_mini.txt"] = encode(b)
    return files


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in outputs().items():
        (OUT / name).write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
