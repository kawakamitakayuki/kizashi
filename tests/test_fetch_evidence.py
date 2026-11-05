"""追加資料の取得（kizashi fetch <run_id> <URL> [--index | --definition]）と探索上限の計上。

test_plan.md の FT-4（25MB を超える応答の打ち切り、41回目の通信の拒否）。implementation_spec.md 4.2・3.6。
通信は httpx.MockTransport、PDF のテキストは tests/fixtures/evidence/*.txt に替える（conftest）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from conftest import BASE, URL, WEB, Site, evidence_pdf_bytes, ledger_of
from kizashi import fetcher, limits
from kizashi.errors import KizashiError

SPEC_URL = "https://www.cao.go.jp/test/spec.pdf"
HTML_URL = "https://www.cao.go.jp/test/koukoku.html"
NOEXT_URL = "https://www.cao.go.jp/test/koukoku"
DEF_URL = "https://www.cao.go.jp/test/kisai_yoryo.pdf"


@pytest.fixture
def evsite(site: Site) -> Site:
    pdf = {"content-type": "application/pdf"}
    html = {"content-type": "text/html; charset=UTF-8"}
    site.routes.update(
        {
            SPEC_URL: httpx.Response(200, headers=pdf, content=evidence_pdf_bytes("spec_sample")),
            DEF_URL: httpx.Response(200, headers=pdf, content=evidence_pdf_bytes("spec_sample")),
            HTML_URL: httpx.Response(
                200, headers=html, content=(WEB / "evidence.html").read_bytes()
            ),
            NOEXT_URL: httpx.Response(
                200, headers=html, content=(WEB / "evidence.html").read_bytes()
            ),
        }
    )
    return site


def fetch(repo: Path, run_id: str, site: Site, url: str, kind: str = "evidence") -> dict:
    return fetcher.fetch_evidence(repo, run_id, url, kind, transport=site.transport)


def limits_of(repo: Path, run_id: str) -> dict:
    return json.loads(ledger_of(repo, run_id).execute("SELECT limits_json FROM runs").fetchone()[0])


def set_limits(repo: Path, run_id: str, **values: int) -> None:
    conn = sqlite3.connect(repo / "data" / "runs" / run_id / "ledger.sqlite")
    data = {**limits_of(repo, run_id), **values}
    conn.execute("UPDATE runs SET limits_json = ?", (json.dumps(data),))
    conn.commit()
    conn.close()


def source(repo: Path, run_id: str, source_id: str) -> sqlite3.Row:
    return (
        ledger_of(repo, run_id)
        .execute("SELECT * FROM sources WHERE source_id = ?", (source_id,))
        .fetchone()
    )


# ---------------------------------------------------------------------------
# 追加資料・--index・--definition
# ---------------------------------------------------------------------------


def test_fetch_pdf_evidence_writes_page_texts(repo: Path, synthetic_run: str, evsite: Site) -> None:
    """4.2：PDF の追加資料は原本を data/raw に置き、ページ別テキストを evidence/<資料ID>/pages/<n>.txt に
    書く。追加資料の上限に1つ数える。"""
    result = fetch(repo, synthetic_run, evsite, SPEC_URL)
    assert (result["source_id"], result["kind"], result["page_count"]) == ("S010", "evidence", 4)
    pages = repo / "data" / "runs" / synthetic_run / "evidence" / "S010" / "pages"
    assert sorted(p.name for p in pages.iterdir()) == ["1.txt", "2.txt", "3.txt", "4.txt"]
    assert "配送予定数量は年間1,200件とする。" in (pages / "4.txt").read_text(encoding="utf-8")
    row = source(repo, synthetic_run, "S010")
    assert (row["kind"], row["status"], row["page_count"], row["logical_key"]) == (
        "evidence",
        "fetched",
        4,
        None,
    )
    assert (repo / "data" / "raw" / f"{row['sha256']}.pdf").is_file()
    lim = limits_of(repo, synthetic_run)
    assert (lim["evidence_fetches"], lim["http_requests"]) == (1, 10)  # run の9回＋1回
    assert result["limits"]["evidence_fetches"] == 1


def test_fetch_index_and_definition_are_not_counted(
    repo: Path, synthetic_run: str, evsite: Site
) -> None:
    """4.2：--index（kind='index'）と --definition（kind='definition'）は追加資料の上限に数えない。
    HTML は1ページとし、ページ内のリンクの一覧（絶対 URL）を返す。スクリプトの文字は本文に入れない。"""
    index = fetch(repo, synthetic_run, evsite, HTML_URL, "index")
    definition = fetch(repo, synthetic_run, evsite, DEF_URL, "definition")
    assert (index["kind"], index["page_count"], definition["kind"]) == ("index", 1, "definition")
    assert limits_of(repo, synthetic_run)["evidence_fetches"] == 0
    assert [link["url"] for link in index["links"]] == [
        "https://www.cao.go.jp/test/spec.pdf",
        "https://www.cao.go.jp/chotatsu/kohyo/kohyo.html",
        "https://example.com/other.html",
    ]
    assert index["links"][0]["text"] == "仕様書（PDF）"
    text = (
        repo / "data" / "runs" / synthetic_run / "evidence" / index["source_id"] / "pages" / "1.txt"
    ).read_text(encoding="utf-8")
    assert "本件は、令和７年度の翻訳業務の 一般競争入札である。" in text
    assert "予定数量は年間300件とする。" in text
    assert "スクリプト" not in text and "color" not in text


def test_fetch_html_without_extension(repo: Path, synthetic_run: str, evsite: Site) -> None:
    """4.2：拡張子のない URL は、応答の種類と中身の先頭で HTML と判断する。"""
    result = fetch(repo, synthetic_run, evsite, NOEXT_URL)
    assert (result["kind"], result["media_type"], result["page_count"]) == (
        "evidence",
        "text/html",
        1,
    )


def test_fetch_refuses_disallowed_host_and_dataset_links(
    repo: Path, synthetic_run: str, evsite: Site
) -> None:
    """4.2・D-021：許可リスト外のホストと、公表一覧のデータ（run が取得する CSV・PDF）は通信せずに拒否する。"""
    for url, code in (
        ("https://example.com/spec.pdf", "E_HOST_NOT_ALLOWED"),
        ("http://www.cao.go.jp/test/spec.pdf", "E_HOST_NOT_ALLOWED"),
        (URL["k25_pdf"], "E_BAD_EVIDENCE_SOURCE"),
        (URL["k25_csv"], "E_BAD_EVIDENCE_SOURCE"),
    ):
        with pytest.raises(KizashiError) as info:
            fetch(repo, synthetic_run, evsite, url)
        assert info.value.code == code
    assert not [u for u in evsite.requests if "test/" in u or "example.com" in u]
    assert ledger_of(repo, synthetic_run).execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 9


@pytest.mark.parametrize(
    "url",
    [
        URL["k25_pdf"] + "?v=2",
        URL["k25_pdf"] + "#page=1",
        URL["k25_csv"].replace("_kohyo_kyoso.csv", "_KOHYO_KYOSO.CSV"),
        URL["k25_pdf"].replace(".pdf", "%2Epdf"),
    ],
)
def test_ft2_dataset_links_in_other_forms_are_refused(
    repo: Path, synthetic_run: str, evsite: Site, url: str
) -> None:
    """FT-2（D-034）：問い合わせ・断片・大文字・%の符号を付けた公表一覧の URL も、通信せずに拒否する。"""
    before = len(evsite.requests)
    with pytest.raises(KizashiError) as info:
        fetch(repo, synthetic_run, evsite, url)
    assert info.value.code == "E_BAD_EVIDENCE_SOURCE"
    assert len(evsite.requests) == before  # 通信していない
    assert ledger_of(repo, synthetic_run).execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 9


@pytest.mark.parametrize("steps", [1, 2])
def test_ft2_redirect_to_dataset_link_is_refused(
    repo: Path, synthetic_run: str, evsite: Site, steps: int
) -> None:
    """FT-2（D-034）：リダイレクトの途中・最後の URL が公表一覧のデータなら、その先へ通信せずに拒否する。

    run が取得していない改訂版の PDF（中身が違う）でも、ページ別テキストを作らない（D-021）。
    """
    hops = [f"https://www.cao.go.jp/test/latest{n}.pdf" for n in range(steps)]
    revised = BASE + "tekiseika/7nendo/20261001-02_kohyo_kyoso.pdf?from=latest"
    for here, there in zip(hops, [*hops[1:], revised], strict=True):
        evsite.routes[here] = httpx.Response(302, headers={"location": there})
    evsite.routes[revised] = httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=evidence_pdf_bytes("spec_sample")
    )
    with pytest.raises(KizashiError) as info:
        fetch(repo, synthetic_run, evsite, hops[0])
    assert info.value.code == "E_BAD_EVIDENCE_SOURCE"
    assert evsite.requests[-steps:] == hops  # 公表一覧の URL へは通信していない
    assert revised not in evsite.requests
    # 通信した後の拒否なので、取得の失敗として残す。ページ別テキストは作らない
    row = source(repo, synthetic_run, "S010")
    assert (row["status"], row["url"], row["sha256"]) == ("failed", hops[0], None)
    assert row["failure_reason"].startswith("E_BAD_EVIDENCE_SOURCE")
    assert limits_of(repo, synthetic_run)["evidence_fetches"] == 0
    assert not (repo / "data" / "runs" / synthetic_run / "evidence").exists()


@pytest.mark.parametrize(
    ("route", "code"),
    [
        (httpx.Response(404, content=b"not found"), "E_FETCH_FAILED"),
        (
            httpx.Response(200, headers={"content-type": "text/plain"}, content=b"plain text"),
            "E_FETCH_INVALID",
        ),
        (
            httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4\nbroken\n"
            ),
            "E_FETCH_INVALID",
        ),
        (
            httpx.Response(200, headers={"content-type": "application/pdf"}, content=b""),
            "E_FETCH_INVALID",
        ),
    ],
)
def test_fetch_failure_is_recorded(
    repo: Path, synthetic_run: str, evsite: Site, route: httpx.Response, code: str
) -> None:
    """4.2：取得に失敗した資料も sources に failed と理由を残す。ハッシュは求めない。上限に数えない。"""
    url = "https://www.cao.go.jp/test/bad.pdf"
    evsite.routes[url] = route
    with pytest.raises(KizashiError) as info:
        fetch(repo, synthetic_run, evsite, url)
    assert info.value.code == code
    assert info.value.affected_ids[-1] == "S010"
    row = source(repo, synthetic_run, "S010")
    assert (row["kind"], row["status"], row["sha256"]) == ("evidence", "failed", None)
    assert row["failure_reason"].startswith(code)
    assert limits_of(repo, synthetic_run)["evidence_fetches"] == 0
    assert not (repo / "data" / "runs" / synthetic_run / "evidence" / "S010").exists()


# ---------------------------------------------------------------------------
# FT-4：25MB を超える応答の打ち切り、41回目の通信の拒否（と、追加資料の上限、実行の合計の上限）
# ---------------------------------------------------------------------------


def test_ft4_response_over_limit_is_cut(
    repo: Path, synthetic_run: str, evsite: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT-4：1応答の上限（25MB）を超えたら打ち切る（上限を小さくして、長さの宣言のない応答で確かめる）。"""
    assert limits.MAX_RESPONSE_BYTES == 25 * 1024 * 1024
    monkeypatch.setattr(limits, "MAX_RESPONSE_BYTES", 1000)
    url = "https://www.cao.go.jp/test/big.pdf"

    def stream(request: httpx.Request) -> httpx.Response:
        chunks = [b"%PDF-1.4\n" + b"x" * 600, b"y" * 600]
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            stream=httpx.ByteStream(b"".join(chunks)),
        )

    evsite.routes[url] = stream
    with pytest.raises(KizashiError) as info:
        fetch(repo, synthetic_run, evsite, url)
    assert info.value.code == "E_FETCH_FAILED" and "打ち切" in info.value.message
    assert source(repo, synthetic_run, "S010")["status"] == "failed"
    assert not list((repo / "data" / "raw").glob(".tmp-*"))


def test_ft4_declared_length_over_limit(repo: Path, synthetic_run: str, evsite: Site) -> None:
    """FT-4：Content-Length が25MBを超える応答は、本文を読まずに打ち切る。"""
    url = "https://www.cao.go.jp/test/huge.pdf"
    evsite.routes[url] = httpx.Response(
        200,
        headers={"content-type": "application/pdf", "content-length": str(26 * 1024 * 1024)},
        content=b"%PDF-1.4\n",
    )
    with pytest.raises(KizashiError) as info:
        fetch(repo, synthetic_run, evsite, url)
    assert info.value.code == "E_FETCH_FAILED" and "25MB" in info.value.message


def test_ft4_41st_request_is_refused(repo: Path, synthetic_run: str, evsite: Site) -> None:
    """FT-4：通信は実行全体で40回まで。40回使った後の41回目は送らずに E_LIMIT_HTTP。"""
    assert limits.MAX_HTTP_REQUESTS == 40
    set_limits(repo, synthetic_run, http_requests=39)
    fetch(repo, synthetic_run, evsite, SPEC_URL)  # 40回目
    assert limits_of(repo, synthetic_run)["http_requests"] == 40
    sent = len(evsite.requests)
    with pytest.raises(KizashiError) as info:
        fetch(repo, synthetic_run, evsite, HTML_URL, "index")  # 41回目
    assert info.value.code == "E_LIMIT_HTTP"
    assert len(evsite.requests) == sent
    assert limits_of(repo, synthetic_run)["http_requests"] == 40


def test_ft4_13th_evidence_is_refused(repo: Path, synthetic_run: str, evsite: Site) -> None:
    """4.2・3.6：追加資料は12件まで。13件目は通信せずに E_LIMIT_EVIDENCE。--index・--definition は取れる。"""
    assert limits.MAX_EVIDENCE_FETCH == 12
    set_limits(repo, synthetic_run, evidence_fetches=12)
    sent = len(evsite.requests)
    with pytest.raises(KizashiError) as info:
        fetch(repo, synthetic_run, evsite, SPEC_URL)
    assert info.value.code == "E_LIMIT_EVIDENCE"
    assert len(evsite.requests) == sent
    fetch(repo, synthetic_run, evsite, DEF_URL, "definition")
    fetch(repo, synthetic_run, evsite, HTML_URL, "index")
    assert limits_of(repo, synthetic_run)["evidence_fetches"] == 12


def test_ft4_run_total_bytes(repo: Path, synthetic_run: str, evsite: Site) -> None:
    """3.6：実行全体の合計が100MBを超えたら E_LIMIT_BYTES。"""
    set_limits(repo, synthetic_run, bytes_total=limits.MAX_RUN_BYTES - 10)
    with pytest.raises(KizashiError) as info:
        fetch(repo, synthetic_run, evsite, SPEC_URL)
    assert info.value.code == "E_LIMIT_BYTES"
    assert source(repo, synthetic_run, "S010")["status"] == "failed"
