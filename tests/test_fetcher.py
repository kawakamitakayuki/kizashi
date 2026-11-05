"""取得のテスト（test_plan.md の FT-1〜FT-3）。通信は httpx.MockTransport だけを使う。

期待値は implementation_spec.md 4.2・3.6 と、合成のページ（tests/fixtures/web/kohyo.html）から手で書いたもの。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from conftest import PAGE, URL, WEB, Site, fixture_bytes
from kizashi import fetcher, limits
from kizashi.adapters import cao_procurement as cao
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

ALLOWED = ["*.go.jp", "*.lg.jp"]
PATTERN = (
    r"tekiseika/(?P<reiwa>\d+)nendo/[^/]+_kohyo_(?P<family>kyoso|zuikei)\.(?P<format>csv|pdf)$"
)


def client_for(site: Site) -> httpx.Client:
    return fetcher.make_client(site.transport)


# ---------------------------------------------------------------------------
# FT-1：リンクの取り出しから保存まで。エラーページ・空・期待と違う年度を取得済みにしない
# ---------------------------------------------------------------------------


def test_ft1_discover_links_from_page() -> None:
    """FT-1：公表ページの相対リンクを絶対 URL にし、リンクの形に合うものだけを返す。"""
    links = fetcher.discover_links(PAGE, (WEB / "kohyo.html").read_text(encoding="utf-8"), PATTERN)
    found = {(link["url"], link["reiwa"], link["family"], link["format"]) for link in links}
    assert found == {
        (URL["k25_pdf"], "7", "kyoso", "pdf"),
        (URL["k25_csv"], "7", "kyoso", "csv"),
        (URL["k24_pdf"], "6", "kyoso", "pdf"),
        (URL["k24_csv"], "6", "kyoso", "csv"),
        (URL["z25_pdf"], "7", "zuikei", "pdf"),
        (URL["z25_csv"], "7", "zuikei", "csv"),
        (URL["z24_pdf"], "6", "zuikei", "pdf"),
        (URL["z24_csv"], "6", "zuikei", "csv"),
        # 形は合うが許可リストの外（取得の段階で拒否する。FT-2）
        (
            "https://example.com/chotatsu/kohyo/tekiseika/5nendo/20241105-2_kohyo_kyoso.csv",
            "5",
            "kyoso",
            "csv",
        ),
        (
            "http://www.cao.go.jp/chotatsu/kohyo/tekiseika/5nendo/20240711-2_kohyo_zuikei.csv",
            "5",
            "zuikei",
            "csv",
        ),
    }


def test_ft1_fetch_saves_by_sha256(repo: Path, site: Site) -> None:
    """FT-1：取得したファイルを data/raw/<sha256>.<拡張子> に保存する。同じ中身は上書きしない。"""
    budget = fetcher.Budget()
    data = fixture_bytes("kyoso_2024_mini.csv")
    sha = hashlib.sha256(data).hexdigest()
    with client_for(site) as client:
        first = fetcher.fetch_to_raw(
            repo, client, URL["k24_csv"], fmt="csv", allowed_hosts=ALLOWED, budget=budget
        )
        path = repo / "data" / "raw" / f"{sha}.csv"
        assert first.sha256 == sha and first.path == path and first.size_bytes == len(data)
        assert first.media_type == "text/csv"
        assert path.read_bytes() == data
        mtime = path.stat().st_mtime_ns
        second = fetcher.fetch_to_raw(
            repo, client, URL["k24_csv"], fmt="csv", allowed_hosts=ALLOWED, budget=budget
        )
    assert second.sha256 == sha
    assert path.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in (repo / "data" / "raw").iterdir()) == [
        f"{sha}.csv"
    ]  # 一時ファイルが残らない
    assert budget.http_requests == 2
    assert budget.bytes_total == 2 * len(data)


@pytest.mark.parametrize(
    ("content", "fmt", "url", "why"),
    [
        (b"", "csv", URL["k24_csv"], "空のファイル"),
        (b"<!DOCTYPE html><html><body>error</body></html>", "csv", URL["k24_csv"], "HTML のページ"),
        (b"\r\n  <html><body>404</body></html>", "pdf", URL["k24_pdf"], "HTML のページ"),
        (b"not a pdf", "pdf", URL["k24_pdf"], "%PDF-"),
        (b"%PDF-1.4\n", "csv", URL["k24_pdf"], "拡張子"),
    ],
)
def test_ft1_invalid_content_is_not_saved(
    repo: Path, content: bytes, fmt: str, url: str, why: str
) -> None:
    """FT-1：空、HTML のエラーページ、形式の違うファイルは E_FETCH_INVALID で、data/raw に残さない。"""
    site = Site({url: httpx.Response(200, content=content)})
    with client_for(site) as client, pytest.raises(KizashiError) as info:
        fetcher.fetch_to_raw(
            repo, client, url, fmt=fmt, allowed_hosts=ALLOWED, budget=fetcher.Budget()
        )
    assert info.value.code == "E_FETCH_INVALID"
    assert why in info.value.message
    assert list((repo / "data" / "raw").iterdir()) == []


def test_ft1_wrong_year_csv_is_rejected(repo: Path) -> None:
    """FT-1：令和7年度（2025）として取得した CSV の契約日が令和6年度なら E_FETCH_INVALID（D-025）。"""
    adapter = load_adapter(repo, "cao-procurement")
    spec = adapter.file_spec(2025, "kyoso")
    with pytest.raises(KizashiError) as info:
        cao.validate_csv(fixture_bytes("kyoso_2024_mini.csv"), spec)
    assert info.value.code == "E_FETCH_INVALID"
    assert "2025年度" in info.value.message
    # 読める契約日は11行（NO 9 の R6.13.1 は読めない）。そのすべてが年度の外
    assert "11行" in info.value.message
    cao.validate_csv(fixture_bytes("kyoso_2025_mini.csv"), spec)  # 正しい年度は通る


def test_ft1_one_row_outside_year_is_rejected(repo: Path) -> None:
    """FT-1：年度の外の契約日が1行でもあれば E_FETCH_INVALID（D-025）。外れた行の NO を理由に書く。"""
    adapter = load_adapter(repo, "cao-procurement")
    spec = adapter.file_spec(2024, "kyoso")
    data = fixture_bytes("kyoso_2024_mini.csv").replace(
        "R7.3.31".encode("cp932"), "R7.4.1".encode("cp932")
    )
    with pytest.raises(KizashiError) as info:
        cao.validate_csv(data, spec)
    assert "1行。NO：12" in info.value.message


def test_ft1_csv_without_dates_or_no_header(repo: Path) -> None:
    """FT-1：読める契約日がない表と、「NO」の見出しの行がない CSV は E_FETCH_INVALID。"""
    adapter = load_adapter(repo, "cao-procurement")
    spec = adapter.file_spec(2024, "kyoso")
    header_only = b"\r\n".join(fixture_bytes("kyoso_2024_mini.csv").split(b"\r\n")[:4])
    # 表題と作成日の2行の後に、見出しの2行だけ（セル内の改行を含むため、切り方は手で確かめた）
    with pytest.raises(KizashiError) as info:
        cao.validate_csv(header_only + b"\r\n", spec)
    assert info.value.code == "E_FETCH_INVALID"
    with pytest.raises(KizashiError) as info:
        cao.validate_csv("a,b\r\n1,2\r\n".encode("cp932"), spec)
    assert "NO" in info.value.message
    with pytest.raises(KizashiError):
        cao.validate_csv(b"\xff\xfe\x00\x81\x00", spec)


def test_ft1_format_changed_csv_is_kept_for_normalize(repo: Path) -> None:
    """FT-1：見出しが期待と違う CSV は、取得では止めない（原本を残し、正規化で E_FORMAT_CHANGED。D-027）。"""
    adapter = load_adapter(repo, "cao-procurement")
    cao.validate_csv(fixture_bytes("kyoso_bad_header.csv"), adapter.file_spec(2024, "kyoso"))
    # 列の数が違う（随意契約の17列を競争入札の16列として読む）ものも同じ
    cao.validate_csv(fixture_bytes("zuikei_2024_mini.csv"), adapter.file_spec(2024, "kyoso"))


# ---------------------------------------------------------------------------
# FT-2：許可リスト外のホスト、許可リスト外へのリダイレクトを拒否する
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://www.cao.go.jp/chotatsu/kohyo/kohyo.html", True),
        ("https://www.city.example.lg.jp/a.pdf", True),
        ("https://WWW.CAO.GO.JP/a.csv", True),
        ("http://www.cao.go.jp/a.csv", False),
        ("https://example.com/a.csv", False),
        ("https://go.jp/a.csv", False),
        ("https://www.cao.go.jp.example.com/a.csv", False),
        ("https://evil-go.jp/a.csv", False),
        ("https://user@www.cao.go.jp/a.csv", False),
        ("ftp://www.cao.go.jp/a.csv", False),
    ],
)
def test_ft2_host_allowlist(url: str, allowed: bool) -> None:
    """FT-2：*.go.jp と *.lg.jp の HTTPS だけ（go.jp そのもの、似た名前、利用者名付きは拒否）。"""
    if allowed:
        fetcher.check_url(url, ALLOWED)
    else:
        with pytest.raises(KizashiError) as info:
            fetcher.check_url(url, ALLOWED)
        assert info.value.code == "E_HOST_NOT_ALLOWED"


def test_ft2_disallowed_host_is_not_requested(repo: Path, site: Site) -> None:
    """FT-2：許可リスト外の URL には通信しない。"""
    with client_for(site) as client, pytest.raises(KizashiError) as info:
        fetcher.fetch_to_raw(
            repo,
            client,
            "https://example.com/x_kohyo_kyoso.csv",
            fmt="csv",
            allowed_hosts=ALLOWED,
            budget=fetcher.Budget(),
        )
    assert info.value.code == "E_HOST_NOT_ALLOWED"
    assert site.requests == []


@pytest.mark.parametrize(
    "location",
    ["https://evil.example.org/x.csv", "http://www.cao.go.jp/x.csv", "https://go.jp/x.csv"],
)
def test_ft2_redirect_outside_allowlist(repo: Path, location: str) -> None:
    """FT-2：許可リスト外へのリダイレクトは、追う前に E_REDIRECT_NOT_ALLOWED。"""
    site = Site({URL["k24_csv"]: httpx.Response(302, headers={"location": location})})
    with client_for(site) as client, pytest.raises(KizashiError) as info:
        fetcher.http_get(client, URL["k24_csv"], allowed_hosts=ALLOWED, budget=fetcher.Budget())
    assert info.value.code == "E_REDIRECT_NOT_ALLOWED"
    assert site.requests == [URL["k24_csv"]]


def test_ft2_redirect_inside_allowlist_is_followed_step_by_step(repo: Path) -> None:
    """FT-2：許可リストの中のリダイレクトは1段ずつ追う（相対の Location も解決する）。各段を通信に数える。"""
    mid = "https://www2.cao.go.jp/moved/a.csv"
    data = fixture_bytes("kyoso_2024_mini.csv")
    site = Site(
        {
            URL["k24_csv"]: httpx.Response(301, headers={"location": mid}),
            mid: httpx.Response(302, headers={"location": "/final/b.csv"}),
            "https://www2.cao.go.jp/final/b.csv": httpx.Response(200, content=data),
        }
    )
    budget = fetcher.Budget()
    with client_for(site) as client:
        final_url, content, _ = fetcher.http_get(
            client, URL["k24_csv"], allowed_hosts=ALLOWED, budget=budget
        )
    assert final_url == "https://www2.cao.go.jp/final/b.csv"
    assert content == data
    assert site.requests == [URL["k24_csv"], mid, "https://www2.cao.go.jp/final/b.csv"]
    assert budget.http_requests == 3


def test_ft2_too_many_redirects(repo: Path) -> None:
    """FT-2：リダイレクトは5段まで。6段目は追わずに E_FETCH_FAILED。"""
    routes = {
        f"https://www.cao.go.jp/r{i}.csv": httpx.Response(
            302, headers={"location": f"/r{i + 1}.csv"}
        )
        for i in range(10)
    }
    site = Site(routes)
    with client_for(site) as client, pytest.raises(KizashiError) as info:
        fetcher.http_get(
            client, "https://www.cao.go.jp/r0.csv", allowed_hosts=ALLOWED, budget=fetcher.Budget()
        )
    assert info.value.code == "E_FETCH_FAILED"
    assert len(site.requests) == limits.MAX_REDIRECTS + 1


# ---------------------------------------------------------------------------
# FT-3：取得失敗を sources に failed と理由で残し、ハッシュを求めない（実行のテストは test_run.py）
# ---------------------------------------------------------------------------


def test_ft3_http_404_is_not_retried(repo: Path) -> None:
    """FT-3：HTTP 404 は再試行しない失敗（retryable は false）。"""
    site = Site({URL["k24_pdf"]: httpx.Response(404)})
    with client_for(site) as client, pytest.raises(KizashiError) as info:
        fetcher.http_get(client, URL["k24_pdf"], allowed_hosts=ALLOWED, budget=fetcher.Budget())
    assert (info.value.code, info.value.retryable) == ("E_FETCH_FAILED", False)
    assert "HTTP 404" in info.value.message
    assert len(site.requests) == 1


def test_ft3_transient_failure_is_retried_twice(repo: Path) -> None:
    """FT-3：一時的な失敗（接続の失敗、HTTP 503）は2回まで再試行し、だめなら retryable の失敗。"""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    site = Site({URL["k24_pdf"]: refuse, URL["z24_pdf"]: httpx.Response(503)})
    budget = fetcher.Budget()
    with client_for(site) as client:
        for url in (URL["k24_pdf"], URL["z24_pdf"]):
            with pytest.raises(KizashiError) as info:
                fetcher.http_get(client, url, allowed_hosts=ALLOWED, budget=budget)
            assert (info.value.code, info.value.retryable) == ("E_FETCH_FAILED", True)
    assert site.requests == [URL["k24_pdf"]] * 3 + [URL["z24_pdf"]] * 3
    assert budget.http_requests == 6


def test_ft3_transient_then_success(repo: Path) -> None:
    """FT-3：1回目が一時的な失敗でも、再試行で取れれば成功。"""
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, content=b"%PDF-1.4\n")

    site = Site({URL["k24_pdf"]: flaky})
    with client_for(site) as client:
        _, content, _ = fetcher.http_get(
            client, URL["k24_pdf"], allowed_hosts=ALLOWED, budget=fetcher.Budget()
        )
    assert content == b"%PDF-1.4\n"
    assert calls["n"] == 2
