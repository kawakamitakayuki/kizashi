"""V7 の数値・氏名・強い語の検査の関数の単体テスト（implementation_spec.md 9章の V7 の行）。

P0-4 では record がこの関数を使う（4.5）。check の V7 としての CK-4 の確認は P0-8 で足す。
期待値は9章の表の「対象外の形」と test_plan.md の CK-4 の例から書き写したもの。
"""

from __future__ import annotations

import pytest

from kizashi import check


@pytest.mark.parametrize(
    "text",
    [
        "令和6年度から令和7年度に変わった。",
        "令和元年度と令和6・7年度を比べる。",
        "平成30年度の公表資料。",
        "2024年度と2024・2025年度。",
        "契約日は R6.4.1 である。R7 の資料。",
        "契約日は令和6年4月1日である。",
        "4月1日に締結した。",
        "会計法第29条の3第4項による。",
        "予算決算及び会計令99条の5による。",
        "予決令第99条第2号による。",
        "資料 S004 の S004:000042 と計算 K0012、候補 L0007、主張 C0031、対応 M002。",
        "型 T6a と T1、検出 S2、品質検査 Q05。",
        "合計は{calc:K0012}である。",
        "全角の ＩＤ Ｓ００４ と 令和６年度。",
    ],
)
def test_v7_allowed_forms_are_not_raw_numbers(text: str) -> None:
    """V7：年度・日付・条文・ID・計算の参照は生の数値として扱わない。"""
    assert check.raw_numbers(text) == []


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("3条件を満たす。", "3"),
        ("1,200万円に増えた。", "1,200"),
        ("増減率は12.5%である。", "12.5"),
        ("全角の１２件。", "12"),
        ("S4 は検出でない。", "4"),
        ("合計は{calc:K0012}で、差は300である。", "300"),
    ],
)
def test_v7_raw_numbers_are_found(text: str, hit: str) -> None:
    """V7：金額・件数・比率の数字（全角はNFKCにしてから見る）は不合格。"""
    assert check.raw_numbers(text) == [hit]


def test_v7_numbers_inside_verified_quotes_are_ignored() -> None:
    """V7：照合済みの引用の中の数値は対象外。引用の外の数値は対象。"""
    quote = "配送予定数量は年間1,200件とする。"
    text = f"仕様書には「{quote}」とある。"
    assert check.raw_numbers(text, quotes=[quote]) == []
    assert check.raw_numbers(text + "差は5件。", quotes=[quote]) == ["5"]


def test_v7_personal_names_full_name_only() -> None:
    """V7：氏名は姓と名をつないだ全体（空白なし・半角・全角）で照らす。1語だけでは照らさない。"""
    names = check.name_variants([("架空", "太郎"), ("試験次郎",)])
    assert check.personal_names("担当は架空太郎である。", names) == [(3, 7)]
    assert check.personal_names("担当は架空 太郎である。", names) == [(3, 8)]
    assert check.personal_names("担当は架空　太郎である。", names) == [(3, 8)]
    assert check.personal_names("担当は試験次郎である。", names) == [(3, 7)]
    assert check.personal_names("架空の業務と太郎の件。", names) == []


def test_v7_strong_words_warn_with_exceptions() -> None:
    """V7：強い語は警告。例外語（不正確・不正アクセス）と引用の中は警告しない。"""
    assert check.strong_words("不正の疑惑がある。") == ["不正", "疑惑"]
    assert check.strong_words("記載が不正確で、不正アクセスの対策の契約。") == []
    assert check.strong_words("資料に「談合の防止」とある。", quotes=["談合の防止"]) == []


def test_v7_findings_collects_fields() -> None:
    """V7：欄ごとに不合格（数値・氏名）と警告（強い語）を集める。氏名の語そのものは返さない。"""
    names = check.name_variants([("架空", "太郎")])
    errors, warnings = check.v7_findings(
        {"text": "差は300で、架空太郎が担当した。", "question": "不正はあるか"},
        names=names,
    )
    assert errors == [
        {"code": "V7_RAW_NUMBER", "field": "text", "hit": "300"},
        {"code": "V7_PERSONAL_NAME", "field": "text", "at": [7, 11], "what": "name"},
    ]
    assert warnings == [{"code": "W7_STRONG_WORD", "field": "question", "hit": "不正"}]


def test_v7_names_only_fields_skip_numbers_but_not_names() -> None:
    """V7（D-035・D-036）：数値の検査を当てない欄（比較の条件、単位、引用など）にも氏名の検査を当てる。
    強い語の警告は words_only の欄（比較の条件）にも出す（不合格にはしない）。"""
    names = check.name_variants([("架空", "太郎")])
    comparison = {
        "comparison.row_meaning": "1件の契約",
        "comparison.population": "架空　太郎が担当した契約で、不正の有無は問わない",
    }
    errors, warnings = check.v7_findings(
        {"text": "合計を見た。"},
        names=names,
        names_only={**comparison, "unit": "円", "evidence[0].quote": "担当は架空太郎とする。"},
        words_only=comparison,
    )
    assert errors == [
        {
            "code": "V7_PERSONAL_NAME",
            "field": "comparison.population",
            "at": [0, 5],
            "what": "name",
        },
        {"code": "V7_PERSONAL_NAME", "field": "evidence[0].quote", "at": [3, 7], "what": "name"},
    ]
    assert warnings == [{"code": "W7_STRONG_WORD", "field": "comparison.population", "hit": "不正"}]


def test_string_leaves_walks_nested_values() -> None:
    """D-036：提案の中の文字列の値を、入れ子（辞書・配列）も含めて欄の道筋と一緒に取り出す。"""
    data = {
        "kind": "claim",
        "text": "本文",
        "calc_ids": ["K0001"],
        "evidence": [{"quote": "引用", "position": {"page": 1}}],
        "comparison": {"population": "母集団"},
        "flag": True,
    }
    assert list(check.string_leaves(data)) == [
        ("kind", "claim"),
        ("text", "本文"),
        ("calc_ids[0]", "K0001"),
        ("evidence[0].quote", "引用"),
        ("comparison.population", "母集団"),
    ]
