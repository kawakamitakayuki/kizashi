"""D-092 の①〜⑥の回帰テスト（決定の D-092。利用者が判断を Claude Code に任せた）。

test_plan.md の CA-3（文書値・係数の値）、FT-4（追加資料の氏名）、DT-1（除いた行の理由）、PD-2（金額の照合）、
IN-1（data/ の中への書き込み）の回帰。期待値は implementation_spec.md の 3.3・3.4・4.2・6.2、
adapter_cao_procurement.md の 7.3・9.1・9.4 と、決定の D-092 から書き写した。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from conftest import URL, Site, calc_sum, fixture_bytes, ledger_of, make_lead, put, write_config
from kizashi import calc, fetcher, inbox, ledger, paths, pdftext, record, runner
from kizashi.errors import KizashiError

BIG = "9,223,372,036,854,775,808"  # 2^63（台帳の整数の範囲の外）


# ---------------------------------------------------------------------------
# ①（CA-3）：文書値・係数の value の数字は ASCII の 0〜9 だけ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["１２００", "1２00", "١٢٠٠", "１.５"])
def test_ca3_doc_value_must_use_ascii_digits(value: str) -> None:
    """CA-3（D-092①）：全角などの数字で書いた文書値の value は E_SCHEMA（引用の照合より前に止める）。"""
    with pytest.raises(KizashiError) as exc:
        calc.check_doc_value("件数は1,200件である。", value)
    assert exc.value.code == "E_SCHEMA"


def test_ca3_ascii_doc_value_still_matches() -> None:
    """CA-3（D-092①）：ASCII の数字の value は、これまでどおり引用の数値と照らす。"""
    assert str(calc.check_doc_value("件数は1,200件である。", "1200")) == "1200"


def test_cp1_coefficient_value_must_use_ascii_digits(repo: Path, synthetic_run: str) -> None:
    """CP-1・CA-3（D-092①）：係数の value も ASCII の数字だけ。全角の「１.１」は E_SCHEMA で登録しない。"""
    lead = make_lead(repo, synthetic_run)
    proposal = {
        "kind": "coefficient",
        "lead_id": lead["lead"],
        "key": "tax",
        "value": "１.１",
        "meaning": "税込と税抜の換算",
        "why_needed": "片方の年度が税抜かもしれない。",
        "apply_to": "両年度の合計",
        "basis": [],
        "status": "proposed",
    }
    with pytest.raises(KizashiError) as exc:
        record.record(repo, synthetic_run, put(repo, "c.json", proposal))
    assert exc.value.code == "E_SCHEMA"


# ---------------------------------------------------------------------------
# ②（FT-4）：取得済みの CSV が1本もない実行では、追加資料を取らずに E_V7
# ---------------------------------------------------------------------------


def test_ft4_evidence_without_any_csv_is_e_v7(repo: Path) -> None:
    """FT-4（D-092②）：氏名の一覧を作る原本がないので伏せられない。通信と保存の前に E_V7。"""
    site = Site({URL[k]: httpx.Response(404) for k in ("k24_csv", "z24_csv", "k25_csv", "z25_csv")})
    with pytest.raises(KizashiError):
        runner.start_run(repo, "cao-procurement", write_config(repo), transport=site.transport)
    (run_dir,) = (repo / "data" / "runs").iterdir()
    url = "https://www.cao.go.jp/test/spec.pdf"
    before = len(site.requests)
    with pytest.raises(KizashiError) as exc:
        fetcher.fetch_evidence(repo, run_dir.name, url, "evidence", transport=site.transport)
    assert exc.value.code == "E_V7"
    assert len(site.requests) == before
    assert not (run_dir / "evidence").exists()


# ---------------------------------------------------------------------------
# ③④（DT-1・PD-2）：整数の範囲外の金額は、除いた理由を書き分け、原本照合では数値として照らす
# ---------------------------------------------------------------------------


@pytest.fixture
def big_amount_run(repo: Path) -> str:
    """令和6年度の競争入札の NO 9（会議録作成業務）の契約金額を 2^63 にした実行。PDF は元の 350,000 のまま。"""
    data = fixture_bytes("kyoso_2024_mini.csv")
    assert data.count(b"350,000") == 1
    site = Site(
        {
            URL["k24_csv"]: httpx.Response(
                200,
                headers={"content-type": "text/csv"},
                content=data.replace(b"350,000", BIG.encode()),
            )
        }
    )
    result = runner.start_run(repo, "cao-procurement", write_config(repo), transport=site.transport)
    assert result["state"] == "completed"
    return result["run_id"]


def _big_row(repo: Path, run_id: str) -> str:
    conn = ledger_of(repo, run_id)
    (record_id,) = conn.execute(
        "SELECT record_id FROM procurement_contracts WHERE amount_note = 'out_of_range'"
    ).fetchone()
    conn.close()
    return record_id


def test_dt1_out_of_range_amount_is_excluded_with_its_own_reason(
    repo: Path, big_amount_run: str
) -> None:
    """DT-1（D-092③）：S1 の系列のSQL と検出SQL は、範囲外の金額の行を amount_out_of_range として除く
    （単価契約は unit_price、数値にならない金額は amount_not_numeric のまま）。"""
    record_id = _big_row(repo, big_amount_run)
    sql = (repo / "datasets" / "cao-procurement" / "series" / "s1_amount_change.sql").read_text(
        encoding="utf-8"
    )
    conn = ledger.open_ro(repo, big_amount_run)
    rows = conn.execute(sql, {"year": 2024}).fetchall()
    conn.close()
    reasons = {e["ref"]: e["reason"] for r in rows for e in json.loads(r[3] or "[]")}
    assert reasons[record_id] == "amount_out_of_range"
    assert "unit_price" in reasons.values()  # 合成データの単価契約の行（NO 8）


def test_pd2_out_of_range_amount_is_compared_as_a_number() -> None:
    """PD-2（D-092④）：範囲外の金額も数値として PDF の数値の並びと照らす（部分一致にしない）。"""
    values = {"amount_yen": None, "amount_note": "out_of_range", "amount_raw": BIG}
    assert pdftext.match_field("amount_yen", values, BIG) == pdftext.MATCH
    assert (
        pdftext.match_field("amount_yen", values, "9,223,372,036,854,775,809") == pdftext.MISMATCH
    )
    assert (
        pdftext.match_field("amount_yen", values, "19,223,372,036,854,775,808") == pdftext.MISMATCH
    )
    assert pdftext.match_field("amount_yen", values, "") == pdftext.UNVERIFIABLE


def test_pd2_out_of_range_amount_differs_from_pdf_in_the_run(
    repo: Path, big_amount_run: str
) -> None:
    """PD-2（D-092④）：PDF が元の 350,000 のままなら、原本照合の金額は mismatch（照合不能にしない）。"""
    record_id = _big_row(repo, big_amount_run)
    conn = ledger_of(repo, big_amount_run)
    results = [
        json.loads(r[0])
        for r in conn.execute(
            "SELECT result_json FROM computations WHERE kind = 'reconcile'"
            " AND json_extract(result_json, '$.type') = 'source'"
        )
    ]
    conn.close()
    (entry,) = [r["rows"][record_id] for r in results if record_id in r["rows"]]
    assert entry["fields"]["amount_yen"] == "mismatch"


# ---------------------------------------------------------------------------
# ⑤（FT-4）：リンクの文は、氏名を伏せてから100字に切る
# ---------------------------------------------------------------------------


def test_ft4_link_text_is_masked_before_cut(repo: Path, synthetic_run: str, site: Site) -> None:
    """FT-4（D-092⑤）：切れ目にかかる既知の氏名も「（氏名）」に替えてから切る（氏名の一部を残さない）。"""
    url = "https://www.cao.go.jp/test/links.html"
    text = "あ" * 97 + "架空太郎の資料"
    html = f'<html><body><p>本文。</p><a href="https://www.cao.go.jp/test/x.pdf">{text}</a></body></html>'
    site.routes[url] = httpx.Response(
        200, headers={"content-type": "text/html; charset=UTF-8"}, content=html.encode("utf-8")
    )
    result = fetcher.fetch_evidence(repo, synthetic_run, url, "evidence", transport=site.transport)
    links = json.loads((repo / result["links_file"]).read_text(encoding="utf-8"))
    (link,) = links
    assert "架空" not in link["text"] and "太" not in link["text"]
    assert len(link["text"]) <= 100
    assert link["text"].startswith("あ" * 97 + "（")
    # 伏せた数は、ページ別テキスト（リンクの文も本文に入る）の1つと、リンクの文の1つで2
    assert result["masked_names"] == 2


# ---------------------------------------------------------------------------
# ⑥（IN-1）：data/ の中かを確かめてから、フォルダーを作る
# ---------------------------------------------------------------------------


def _linked_run(repo: Path, tmp_path: Path) -> tuple[str, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    runs = repo / "data" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_id = "20261107-120000-zzzz"
    os.symlink(outside, runs / run_id)
    return run_id, outside


def test_in1_make_dir_checks_before_creating(repo: Path, tmp_path: Path) -> None:
    """IN-1（D-092⑥）：実行のフォルダーが data/ の外へのシンボリックリンクなら、外にフォルダーを作る前に止める。"""
    run_id, outside = _linked_run(repo, tmp_path)
    with pytest.raises(KizashiError) as exc:
        paths.make_dir_in_data(repo, paths.run_dir(repo, run_id) / "queries")
    assert exc.value.code == "E_INTERNAL"
    assert not (outside / "queries").exists()


def test_in1_accepted_dir_checks_before_creating(repo: Path, tmp_path: Path) -> None:
    """IN-1（D-092⑥）：accepted/ も、作る前に data/ の中かを確かめる。"""
    run_id, outside = _linked_run(repo, tmp_path)
    with pytest.raises(KizashiError):
        inbox.accepted_dir(repo, run_id)
    assert not (outside / "accepted").exists()


def test_in1_make_dir_inside_data(repo: Path) -> None:
    """IN-1（D-092⑥）：data/ の中なら、これまでどおり作る（すでにあってもよい）。"""
    target = paths.make_dir_in_data(repo, repo / "data" / "runs" / "x" / "memos")
    assert target.is_dir()
    assert paths.make_dir_in_data(repo, target) == target


def test_ca1_calc_sum_still_registers(repo: Path, synthetic_run: str) -> None:
    """念のため：D-092 の変更の後も、基礎集計の登録は変わらない。"""
    assert record.record(repo, synthetic_run, put(repo, "k.json", calc_sum("翻訳業務", 2024)))[
        "value"
    ]
