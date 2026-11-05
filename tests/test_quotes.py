"""引用の照合（test_plan.md の QT-1。implementation_spec.md 8章・6.1・6.4）。

合成データの実行（S001〜S009）に、取得を通さずに追加資料のページ別テキストを足して確かめる。
期待値は、合成の CSV（tests/fixtures/make_cao_fixtures.py）とこのファイルのページの文から手で決めた。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import add_evidence
from kizashi import ledger, quotes
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

PAGES = [
    (
        "（テスト用の合成の仕様書。実在の資料ではない）\n荷物等の配送業務　仕様書\n\n"
        "本業務は、荷物等の配送を行うものである。\n"
    ),
    "本年度は配送先を変更する予定はない。\n",
    "配送予定数量は年間１，２００件\nとする。\n配送の範囲は、別表の\nとおりとする。\n\n（令和７年度）\n\n附則",
]


@pytest.fixture
def ctx(repo: Path, synthetic_run: str):
    run_id = synthetic_run
    sid = add_evidence(repo, run_id, PAGES)
    assert sid == "S010"
    conn = ledger.open_ro(repo, run_id)
    yield conn, repo, run_id, load_adapter(repo, "cao-procurement")
    conn.close()


def verify(ctx, evidence: dict, **kw):
    conn, root, run_id, adapter = ctx
    return quotes.verify_quote(conn, root, run_id, adapter, evidence, **kw)


def ev(source_id: str, position: dict, quote: str, role: str = "support") -> dict:
    return {"source_id": source_id, "position": position, "quote": quote, "role": role}


def code_of(ctx, evidence: dict, **kw) -> str:
    with pytest.raises(KizashiError) as info:
        verify(ctx, evidence, **kw)
    return info.value.code


def test_qt1_nfkc_and_whitespace(ctx) -> None:
    """QT-1：全角の数字・桁区切りと改行があるページでも、NFKC と空白の除去の後に照合できる。"""
    verify(ctx, ev("S010", {"page": 3}, "配送予定数量は年間1,200件とする。"))
    verify(ctx, ev("S010", {"page": 1}, "本業務は、荷物等の配送を行うものである。"))


def test_qt1_csv_cell_by_take_key(ctx) -> None:
    """QT-1：CSV は column のキー（amount_raw など）のセルと照らす（S005:000007 は 750,000）。"""
    verify(ctx, ev("S005", {"row_ref": "S005:000007", "column": "amount_raw"}, "750,000"))
    verify(ctx, ev("S005", {"row_ref": "S005:000007", "column": "amount_raw"}, "７５０，０００"))
    verify(ctx, ev("S005", {"row_ref": "S005:000007", "column": "method_raw"}, "一般競争"))


def test_qt1_header_string_or_unknown_key_is_ref_not_found(ctx) -> None:
    """QT-1：見出しの文字列（契約金額）や存在しないキーは E_REF_NOT_FOUND。使えるキーと見出しを返す。"""
    with pytest.raises(KizashiError) as info:
        verify(ctx, ev("S005", {"row_ref": "S005:000007", "column": "契約金額"}, "750,000"))
    assert info.value.code == "E_REF_NOT_FOUND"
    assert "amount_raw（契約金額）" in info.value.next_action
    assert "officer_raw" not in info.value.next_action
    assert (
        code_of(ctx, ev("S005", {"row_ref": "S005:000007", "column": "officer_raw"}, "x"))
        == "E_REF_NOT_FOUND"
    )


@pytest.mark.parametrize(
    ("evidence", "code"),
    [
        (ev("S010", {"page": 3}, "配送予定数量は年間1,300件とする。"), "E_QUOTE_NOT_FOUND"),
        (
            ev("S005", {"row_ref": "S005:000007", "column": "amount_raw"}, "760,000"),
            "E_QUOTE_NOT_FOUND",
        ),
        (ev("S010", {"page": 9}, "本業務は"), "E_REF_NOT_FOUND"),
        (ev("S099", {"page": 1}, "本業務は"), "E_REF_NOT_FOUND"),
        (
            ev("S005", {"row_ref": "S005:000099", "column": "amount_raw"}, "750,000"),
            "E_REF_NOT_FOUND",
        ),
        (
            ev("S005", {"row_ref": "S001:000012", "column": "amount_raw"}, "650,000"),
            "E_REF_NOT_FOUND",
        ),
        (ev("S005", {"page": 1}, "750,000"), "E_SCHEMA"),
        (ev("S010", {"row_ref": "S010:000001", "column": "x"}, "本業務は"), "E_SCHEMA"),
        (ev("S010", {"page": 1}, "本業務は", role="maybe"), "E_SCHEMA"),
        ({"source_id": "S010", "position": {"page": 1}}, "E_SCHEMA"),
    ],
)
def test_qt1_missing_quote_or_reference(ctx, evidence: dict, code: str) -> None:
    """QT-1：存在しない引用、存在しない資料・ページ・行、資料の形に合わない位置は登録できない。"""
    assert code_of(ctx, evidence) == code


@pytest.mark.parametrize(
    "evidence",
    [
        ev("S010", {"page": 3}, "配送予定数量は年間1,200件と"),
        # 否定文の取り違え：「…予定はない。」を「…予定」で切る
        ev("S010", {"page": 2}, "本年度は配送先を変更する予定"),
        # 行の終わり（段落の終わりでない）で切った引用
        ev("S010", {"page": 3}, "配送の範囲は、別表の"),
        # CSV のセルの途中まで（S001:000002 の方式は「一般競争（単価）」）
        ev("S001", {"row_ref": "S001:000002", "column": "method_raw"}, "一般競争"),
    ],
)
def test_qt1_truncated_quote(ctx, evidence: dict) -> None:
    """QT-1：文の途中で切れた引用は E_QUOTE_TRUNCATED。"""
    assert code_of(ctx, evidence) == "E_QUOTE_TRUNCATED"


def test_qt1_paragraph_and_cell_end_are_boundaries(ctx) -> None:
    """QT-1：句点がなくても、段落の終わり（空行の前）・ページの終わり・セルの終わりまでなら通る。"""
    verify(ctx, ev("S010", {"page": 3}, "（令和7年度）"))
    verify(ctx, ev("S010", {"page": 3}, "附則"))
    verify(ctx, ev("S001", {"row_ref": "S001:000002", "column": "method_raw"}, "一般競争（単価）"))


def test_qt1_fiscal_year_of_record_claim(ctx) -> None:
    """QT-1：「原資料の記載」の fiscal_year と資料（データ）の年度が違えば E_QUOTE_YEAR。"""
    evidence = ev("S005", {"row_ref": "S005:000007", "column": "method_raw"}, "一般競争")
    verify(ctx, evidence, claim_type="record", fiscal_year=2025)
    assert code_of(ctx, evidence, claim_type="record", fiscal_year=2024) == "E_QUOTE_YEAR"
    verify(ctx, evidence, claim_type="observation", fiscal_year=2024)  # 原資料の記載だけを見る
    # 追加資料の年度は機械では決めない
    verify(ctx, ev("S010", {"page": 3}, "附則"), claim_type="record", fiscal_year=2024)


def test_qt1_bad_evidence_sources(ctx) -> None:
    """QT-1：索引ページと公表一覧の PDF（D-021）と知識ファイルは根拠にできない（E_BAD_EVIDENCE_SOURCE）。"""
    assert code_of(ctx, ev("S009", {"page": 1}, "PDF形式とCSV形式のデータは同一のものです。")) == (
        "E_BAD_EVIDENCE_SOURCE"
    )
    with pytest.raises(KizashiError) as info:
        verify(ctx, ev("S006", {"page": 1}, "翻訳業務"))
    assert info.value.code == "E_BAD_EVIDENCE_SOURCE"
    assert "S005" in info.value.next_action and "row_ref" in info.value.next_action
    knowledge = {"file": "playbook", "locator": "C024", "quote": "④契約金額を税込か確認"}
    assert code_of(ctx, knowledge) == "E_BAD_EVIDENCE_SOURCE"


def test_knowledge_quote_is_checked_in_references(tmp_path: Path) -> None:
    """6.4：係数の提案の手がかりの引用は、references/ の該当ファイルとの部分一致で確かめる。"""
    ref = tmp_path / "references"
    ref.mkdir()
    (ref / "investigative_reporting_playbook_v13.0.md").write_text(
        "### C999\n**応用手順**：①テスト ②契約金額を税込か\n確認する\n", encoding="utf-8"
    )
    quotes.verify_knowledge_quote(
        tmp_path, {"file": "playbook", "locator": "C999", "quote": "契約金額を税込か確認"}
    )
    with pytest.raises(KizashiError) as info:
        quotes.verify_knowledge_quote(
            tmp_path, {"file": "playbook", "locator": "C999", "quote": "税抜きで比べる"}
        )
    assert info.value.code == "E_QUOTE_NOT_FOUND"
    with pytest.raises(KizashiError) as info:
        quotes.verify_knowledge_quote(
            tmp_path, {"file": "catalog", "locator": "D249", "quote": "テスト"}
        )
    assert info.value.code == "E_REF_NOT_FOUND"  # 総覧のファイルがない
    with pytest.raises(KizashiError) as info:
        quotes.verify_knowledge_quote(
            tmp_path, {"file": "notes", "locator": "x", "quote": "テスト"}
        )
    assert info.value.code == "E_SCHEMA"
