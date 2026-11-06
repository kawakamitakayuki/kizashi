"""文書値・ページ別テキストの破損の検出と、追加資料の既知の氏名（test_plan.md の CK-1・QT-1・FT-4・CK-4。
D-060・D-061・D-062）。

- D-060：check でも、文書値の値を引用の中の数値と照らし直す（V3D_DOC_VALUE_MISMATCH）。
- D-061：check で、追加資料のページ別テキストを原本（V2 で SHA-256 を確かめた data/raw）から抽出し直し、保存した
  テキストと比べる（V4_PAGE_TEXT_MISMATCH）。
- D-062：追加資料のページ別テキストを書く前に、実行の担当官の氏名（V7 と同じ、姓名をつないだ3通り）を「（氏名）」に
  替える。原本は書き換えない。伏せた数だけを返し、氏名は返さない。未知の氏名の検出は保証しない。

資料の文と値は架空。担当官の氏名「架空　太郎」は合成データ（tests/fixtures/make_cao_fixtures.py）の値。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import ledger_of, rec
from kizashi import check, fetcher, render
from kizashi.errors import KizashiError
from kizashi.paths import raw_dir
from test_check import codes, many_rounds, prepare  # noqa: F401

URL = "https://www.cao.go.jp/test/integrity.html"
TWELVE = "架空の調査の対象は12件である。"


def fetch_html(repo: Path, run_id: str, *paragraphs: str) -> dict:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=f"<!doctype html><html><body>{body}</body></html>".encode(),
        )
    )
    return fetcher.fetch_evidence(repo, run_id, URL, "evidence", transport=transport)


def page_of(repo: Path, fetched: dict, n: int = 1) -> Path:
    return repo / fetched["pages_dir"] / f"{n}.txt"


def tamper(repo: Path, run_id: str, sql: str, params: tuple = ()) -> None:
    conn = ledger_of(repo, run_id)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def failing_codes(call, *args) -> list[str]:
    with pytest.raises(KizashiError) as info:
        call(*args)
    return codes(info)


# ---------------------------------------------------------------------------
# D-060 文書値
# ---------------------------------------------------------------------------


def doc_value_lead(repo: Path, run_id: str) -> tuple[dict, str]:
    lead = prepare(repo, run_id)
    source = fetch_html(repo, run_id, TWELVE)
    calc_id = rec(
        repo,
        run_id,
        {
            "kind": "doc_value",
            "lead_id": lead["lead"],
            "source_id": source["source_id"],
            "position": {"page": 1},
            "quote": TWELVE,
            "value": "12",
            "unit": "件",
            "label": "架空の調査の対象件数",
        },
    )["id"]
    rec(
        repo,
        run_id,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "observation",
            "text": f"架空の調査の対象は{{calc:{calc_id}}}である。",
            "calc_ids": [calc_id],
            "evidence": [],
        },
    )
    return lead, calc_id


def test_ck1_doc_value_rechecked_against_quote(repo: Path, synthetic_run: str) -> None:
    """CK-1（D-060）：引用の「12件」と同じ文書値12は合格。台帳の値だけを13に変えると、check と render は
    V3D_DOC_VALUE_MISMATCH（メモを書かない）。"""
    lead, calc_id = doc_value_lead(repo, synthetic_run)
    assert check.run_check(repo, synthetic_run, lead["lead"])["passed"] is True
    tamper(
        repo, synthetic_run, "UPDATE computations SET value = '13' WHERE calc_id = ?", (calc_id,)
    )
    for call in (check.run_check, render.render_lead):
        assert "V3D_DOC_VALUE_MISMATCH" in failing_codes(call, repo, synthetic_run, lead["lead"])
    memos = repo / "data" / "runs" / synthetic_run / "memos"
    assert not memos.exists() or not any(memos.iterdir())


# ---------------------------------------------------------------------------
# D-061 ページ別テキスト
# ---------------------------------------------------------------------------


def record_claim(repo: Path, run_id: str, lead_id: str, source_id: str, quote: str) -> None:
    rec(
        repo,
        run_id,
        {
            "kind": "claim",
            "lead_id": lead_id,
            "type": "record",
            "fiscal_year": 2025,
            "text": "公表元の資料に記載がある。",
            "calc_ids": [],
            "evidence": [
                {
                    "source_id": source_id,
                    "position": {"page": 1},
                    "quote": quote,
                    "role": "support",
                }
            ],
        },
    )


def test_qt1_page_text_changed_after_fetch_is_rejected(repo: Path, synthetic_run: str) -> None:
    """QT-1・CK-1（D-061）：原本にない文をページ別テキストにだけ足して引用すると、record は保存したテキストで
    照らすので通るが、check は原本から抽出し直したテキストと違うので V4_PAGE_TEXT_MISMATCH（render もメモを
    書かない）。原本と台帳の SHA-256 は変えていない。"""
    lead = prepare(repo, synthetic_run)
    source = fetch_html(repo, synthetic_run, "これは架空の原資料である。")
    page = page_of(repo, source)
    page.write_text("原資料にはない架空の断定が記されている。", encoding="utf-8")
    record_claim(
        repo,
        synthetic_run,
        lead["lead"],
        source["source_id"],
        "原資料にはない架空の断定が記されている。",
    )
    for call in (check.run_check, render.render_lead):
        found = failing_codes(call, repo, synthetic_run, lead["lead"])
        assert "V4_PAGE_TEXT_MISMATCH" in found
    memos = repo / "data" / "runs" / synthetic_run / "memos"
    assert not memos.exists() or not any(memos.iterdir())


def test_qt1_unchanged_page_text_passes_and_later_change_is_caught(
    repo: Path, synthetic_run: str
) -> None:
    """QT-1（D-061）：取得したままのページ別テキストの引用は check を通る。メモを書いた後にテキストを変えると、
    次の check で V4_PAGE_TEXT_MISMATCH。"""
    lead = prepare(repo, synthetic_run)
    source = fetch_html(repo, synthetic_run, "これは架空の原資料である。")
    record_claim(
        repo, synthetic_run, lead["lead"], source["source_id"], "これは架空の原資料である。"
    )
    assert check.run_check(repo, synthetic_run, lead["lead"])["passed"] is True
    page = page_of(repo, source)
    page.write_text(page.read_text(encoding="utf-8") + "\n\n足した文。", encoding="utf-8")
    assert "V4_PAGE_TEXT_MISMATCH" in failing_codes(
        check.run_check, repo, synthetic_run, lead["lead"]
    )


# ---------------------------------------------------------------------------
# D-062 追加資料の既知の氏名
# ---------------------------------------------------------------------------


def test_ft4_known_officer_names_are_masked_in_page_text(repo: Path, synthetic_run: str) -> None:
    """FT-4・CK-4（D-062）：実行の担当官の氏名（全角空白・半角空白・空白なしの3通り）は、ページ別テキストでは
    「（氏名）」に替わる（3か所）。姓だけ（「架空の会議」の「架空」）は替えない。原本は書き換えず、取得の結果は
    伏せた数だけを返す。伏せたテキストを引用した主張は check を通る（抽出し直したテキストも同じく伏せて比べる）。"""
    lead = prepare(repo, synthetic_run)
    source = fetch_html(
        repo,
        synthetic_run,
        "架空　太郎は架空の会議に出席した。",
        "架空 太郎と架空太郎の名も記されている。",
    )
    assert source["masked_names"] == 3
    assert "太郎" not in str(source)
    text = page_of(repo, source).read_text(encoding="utf-8")
    assert "太郎" not in text
    assert "（氏名）は架空の会議に出席した。" in text
    assert "（氏名）と（氏名）の名も記されている。" in text
    (raw,) = raw_dir(repo).glob(f"{source['sha256']}.*")
    assert "架空　太郎".encode() in raw.read_bytes()  # 原本は書き換えない
    record_claim(
        repo, synthetic_run, lead["lead"], source["source_id"], "（氏名）は架空の会議に出席した。"
    )
    assert check.run_check(repo, synthetic_run, lead["lead"])["passed"] is True
