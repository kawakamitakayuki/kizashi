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


# ---------------------------------------------------------------------------
# D-064 係数の手がかり、D-066 壊れたページ別テキスト
# ---------------------------------------------------------------------------


def declined_coefficient(lead_id: str, basis: list) -> dict:
    return {
        "kind": "coefficient",
        "lead_id": lead_id,
        "key": "review_basis",
        "value": "1",
        "meaning": "架空の補正の案",
        "why_needed": "架空の説明を調べるため",
        "apply_to": "検討の対象",
        "basis": basis,
        "status": "declined",
        "reporter_note": "補正は要らないと記者が判断した。",
    }


def test_qt1_coefficient_basis_page_text_is_rechecked(repo: Path, synthetic_run: str) -> None:
    """QT-1・CK-1（D-064）：係数の手がかり（basis）に引用した追加資料のページも、check で引用を照らし、原本から
    抽出し直して比べる。ページ別テキストだけを原本にない文に変えて手がかりにすると V4_PAGE_TEXT_MISMATCH
    （係数が declined でも、メモに出るので）。"""
    lead = prepare(repo, synthetic_run)
    source = fetch_html(repo, synthetic_run, "これは架空の原資料である。")
    invented = "原本にはない架空の補正の理由である。"
    page_of(repo, source).write_text(invented, encoding="utf-8")
    basis = [
        {
            "source_id": source["source_id"],
            "position": {"page": 1},
            "quote": invented,
            "role": "support",
        }
    ]
    rec(repo, synthetic_run, declined_coefficient(lead["lead"], basis))
    for call in (check.run_check, render.render_lead):
        assert "V4_PAGE_TEXT_MISMATCH" in failing_codes(call, repo, synthetic_run, lead["lead"])


def test_qt1_corrupt_page_text_is_a_typed_failure(repo: Path, synthetic_run: str, capsys) -> None:
    """QT-1・CK-1（D-066）：引用したページ別テキストが UTF-8 として壊れていても、想定外のエラー（E_INTERNAL）に
    せず、check は V4_PAGE_TEXT_MISMATCH（終了コード1）。壊れたページへの引用の record は E_QUOTE_NOT_FOUND。"""
    import json

    from kizashi import cli, record

    lead = prepare(repo, synthetic_run)
    source = fetch_html(repo, synthetic_run, "これは架空の原資料である。")
    record_claim(
        repo, synthetic_run, lead["lead"], source["source_id"], "これは架空の原資料である。"
    )
    page_of(repo, source).write_bytes(b"\xff")
    code = cli.main(["check", lead["lead"], "--run", synthetic_run])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "V4_PAGE_TEXT_MISMATCH" in [f["code"] for f in payload["error"]["failures"]]
    proposal = repo / "inbox" / "again.json"
    proposal.write_text(
        json.dumps(
            {
                "kind": "claim",
                "lead_id": lead["lead"],
                "type": "record",
                "fiscal_year": 2025,
                "text": "公表元の資料に記載がある。",
                "calc_ids": [],
                "evidence": [
                    {
                        "source_id": source["source_id"],
                        "position": {"page": 1},
                        "quote": "これは架空の原資料である。",
                        "role": "support",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(KizashiError) as info:
        record.record(repo, synthetic_run, "inbox/again.json")
    assert info.value.code == "E_QUOTE_NOT_FOUND" and info.value.exit_code == 1


# ---------------------------------------------------------------------------
# D-067 inbox のファイル名、D-068 リンクの URL の既知の氏名
# ---------------------------------------------------------------------------


def test_qu1_query_with_non_ascii_file_name_saves_nothing(repo: Path, synthetic_run: str) -> None:
    """QU-1・CK-4（D-067）：氏名を含む（ASCII でない）名前の探索SQLは、実行も保存もせず E_INBOX_PATH。queries/・
    accepted/・events・返却に名前が残らない。"""
    from kizashi import query

    (repo / "inbox" / "架空太郎.sql").write_text("SELECT 1 AS n", encoding="utf-8")
    with pytest.raises(KizashiError) as info:
        query.run_query(repo, synthetic_run, "inbox/架空太郎.sql")
    assert info.value.code == "E_INBOX_PATH" and "架空太郎" not in info.value.message
    run_dir = repo / "data" / "runs" / synthetic_run
    assert not any("架空太郎" in p.name for p in run_dir.rglob("*"))
    conn = ledger_of(repo, synthetic_run)
    assert not any("架空太郎" in line for line in conn.iterdump())
    conn.close()


def test_ft4_links_with_known_names_in_url_are_not_saved(repo: Path, synthetic_run: str) -> None:
    """FT-4（D-068）：リンクの URL（% の符号を戻した形）に実行の担当官の氏名があるリンクは、links.json と取得の
    結果に残さず、除いた数（dropped_links）だけを返す。ほかのリンクは残る。原本は書き換えない。"""
    import json
    from urllib.parse import quote, unquote

    named = "https://www.cao.go.jp/test/spec.html?person=" + quote("架空太郎")
    plain = "https://www.cao.go.jp/test/plain.html"
    body = f'<a href="{named}">架空 太郎の資料</a><a href="{plain}">仕様書</a>'
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=f"<!doctype html><html><body>{body}</body></html>".encode(),
        )
    )
    result = fetcher.fetch_evidence(repo, synthetic_run, URL, "evidence", transport=transport)
    assert [link["url"] for link in result["links"]] == [plain]
    assert result["dropped_links"] == 1
    stored = (
        repo / "data" / "runs" / synthetic_run / "evidence" / result["source_id"] / "links.json"
    ).read_text(encoding="utf-8")
    assert "架空太郎" not in unquote(stored) and "架空太郎" not in unquote(
        json.dumps(result, ensure_ascii=False)
    )
    (raw,) = raw_dir(repo).glob(f"{result['sha256']}.*")
    assert quote("架空太郎").encode() in raw.read_bytes()  # 原本は書き換えない


def test_ft4_fetch_url_with_known_name_is_refused_before_request(
    repo: Path, synthetic_run: str
) -> None:
    """FT-4（D-068）：kizashi fetch に、% の符号を戻すと実行の担当官の氏名を含む URL を渡すと、通信と保存の前に
    E_V7 で拒否する（資料を登録しない。不合格の文に URL と氏名を書かない）。"""
    from urllib.parse import quote

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=b"<html><body>x</body></html>")

    conn = ledger_of(repo, synthetic_run)
    (before,) = conn.execute("SELECT COUNT(*) FROM sources").fetchone()
    conn.close()
    url = "https://www.cao.go.jp/test/spec.html?person=" + quote("架空　太郎")
    with pytest.raises(KizashiError) as info:
        fetcher.fetch_evidence(
            repo, synthetic_run, url, "evidence", transport=httpx.MockTransport(handler)
        )
    assert info.value.code == "E_V7" and requests == []
    assert "太郎" not in info.value.message and quote("太郎") not in info.value.message
    conn = ledger_of(repo, synthetic_run)
    (after,) = conn.execute("SELECT COUNT(*) FROM sources").fetchone()
    conn.close()
    assert after == before


SAFE_PAGE = "https://www.cao.go.jp/test/privacy-redirect.html"


@pytest.mark.parametrize(
    "target",
    [
        "https://www.cao.go.jp/test/details.html",
        "https://unapproved.example.invalid/details.html",
        "dataset",
    ],
    ids=["allowed-host", "unapproved-host", "dataset-url"],
)
def test_ft4_redirect_to_named_url_is_refused_first(
    repo: Path, synthetic_run: str, site, target: str
) -> None:
    """FT-4（D-068）：リダイレクト先の URL に実行の担当官の氏名があれば、許可リスト外のホストや公表一覧のデータの
    検査より先に E_V7 で止める（その先には通信しない）。不合格の文にも、台帳（取得の失敗の記録を含む）にも、
    氏名と URL を残さない。"""
    from urllib.parse import quote, unquote

    from conftest import URL as FIXTURE_URL

    base = FIXTURE_URL["k24_csv"] if target == "dataset" else target
    destination = base + "?person=" + quote("架空太郎")
    site.routes[SAFE_PAGE] = httpx.Response(302, headers={"Location": destination})
    site.requests.clear()
    with pytest.raises(KizashiError) as info:
        fetcher.fetch_evidence(repo, synthetic_run, SAFE_PAGE, "evidence", transport=site.transport)
    assert info.value.code == "E_V7"
    assert site.requests == [SAFE_PAGE]
    assert "架空太郎" not in unquote(info.value.message + info.value.next_action)
    conn = ledger_of(repo, synthetic_run)
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert "架空太郎" not in unquote(dump)


def test_ft4_recheck_of_named_url_is_refused_before_request(
    repo: Path, synthetic_run: str, site
) -> None:
    """FT-4・HS-3（D-068）：前の実装で氏名を含む URL の資料が台帳にあっても、fetch --recheck は通信の前に E_V7 で
    止め、新しい版を登録しない（取り直しも取得と同じ入り口で URL を照らす）。前の実装で登録された状態は、台帳に
    行を書いて作る。"""
    from urllib.parse import quote

    named = "https://www.cao.go.jp/test/legacy.html?person=" + quote("架空太郎")
    content = b"<html><body><p>legacy</p></body></html>"
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    (raw_dir(repo) / f"{sha}.html").write_bytes(content)
    tamper(
        repo,
        synthetic_run,
        "INSERT INTO sources (source_id, kind, url, final_url, fetched_at, sha256, media_type,"
        " size_bytes, page_count, status) VALUES ('S099', 'evidence', ?, ?, '2026-11-06T00:00:00+09:00',"
        " ?, 'text/html', ?, 1, 'fetched')",
        (named, named, sha, len(content)),
    )
    site.routes[named] = httpx.Response(200, content=b"<html><body><p>new</p></body></html>")
    site.requests.clear()
    with pytest.raises(KizashiError) as info:
        fetcher.recheck_source(repo, synthetic_run, "S099", transport=site.transport)
    assert info.value.code == "E_V7" and site.requests == []
    assert "架空" not in info.value.message
    conn = ledger_of(repo, synthetic_run)
    (newer,) = conn.execute("SELECT COUNT(*) FROM sources WHERE supersedes = 'S099'").fetchone()
    conn.close()
    assert newer == 0
