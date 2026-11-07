"""bf1bede の実装レビューの修正のうち、取得の回帰テスト（1-5・1-7）。

test_plan.md の FT-3（取得の失敗を sources に残す）と FT-4（追加資料のページ別テキスト）の回帰。期待値は
implementation_spec.md の 3.7（E_FETCH_FAILED は終了コード4）、4.2（失敗も sources に failed と理由、通信は
すべて数える）と、decisions.md の「bf1bede の実装レビューの記録」の 1-5・1-7 の条件から書き写した。
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

import httpx
import pytest

from conftest import Site, evidence_pdf_bytes, ledger_of
from kizashi import fetcher
from kizashi.errors import KizashiError

SPEC_URL = "https://www.cao.go.jp/test/spec.pdf"


def http_requests(repo: Path, run_id: str) -> int:
    conn = ledger_of(repo, run_id)
    (limits_json,) = conn.execute("SELECT limits_json FROM runs").fetchone()
    conn.close()
    return json.loads(limits_json)["http_requests"]


def sources_for(repo: Path, run_id: str, url: str) -> list[dict]:
    conn = ledger_of(repo, run_id)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT source_id, status, failure_reason, sha256 FROM sources WHERE url = ?", (url,)
        )
    ]
    conn.close()
    return rows


def raising(exc_type: type[Exception]):
    def handler(request: httpx.Request) -> httpx.Response:
        if issubclass(exc_type, httpx.RequestError):
            raise exc_type("合成の通信の失敗", request=request)
        raise exc_type("合成の例外")

    return handler


# ---------------------------------------------------------------------------
# 1-5（FT-3）：httpx の通信・応答の例外は E_FETCH_FAILED にし、failed と通信の回数を残す
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_type",
    [httpx.DecodingError, httpx.ProxyError, httpx.UnsupportedProtocol, httpx.LocalProtocolError],
)
def test_ft3_other_httpx_errors_are_fetch_failed(
    repo: Path, synthetic_run: str, site: Site, exc_type: type[Exception]
) -> None:
    """FT-3（1-5）：一時的でない httpx の例外は E_FETCH_FAILED（retryable は false。再試行しない）。sources に
    failed と理由を残し、ハッシュを求めない。通信の1回は上限に数えて保存する。"""
    site.routes[SPEC_URL] = raising(exc_type)
    before = http_requests(repo, synthetic_run)
    with pytest.raises(KizashiError) as exc:
        fetcher.fetch_evidence(repo, synthetic_run, SPEC_URL, "evidence", transport=site.transport)
    assert exc.value.code == "E_FETCH_FAILED"
    assert exc.value.retryable is False
    assert exc.value.exit_code == 4
    (row,) = sources_for(repo, synthetic_run, SPEC_URL)
    assert row["status"] == "failed"
    assert exc_type.__name__ in row["failure_reason"]
    assert row["sha256"] is None
    assert http_requests(repo, synthetic_run) == before + 1


def test_ft3_non_httpx_errors_are_not_turned_into_fetch_failures(
    repo: Path, synthetic_run: str, site: Site
) -> None:
    """FT-3（1-5）：httpx の通信・応答の例外でない予期しない例外は、通信の失敗にしない（そのまま上げる）。"""
    site.routes[SPEC_URL] = raising(RuntimeError)
    with pytest.raises(RuntimeError):
        fetcher.fetch_evidence(repo, synthetic_run, SPEC_URL, "evidence", transport=site.transport)


# ---------------------------------------------------------------------------
# 1-7（FT-4）：ページ別テキストの書き込みの途中で失敗したら、その取得が作ったフォルダーだけを消す
# ---------------------------------------------------------------------------


def test_ft4_partial_evidence_folder_is_removed(
    repo: Path, synthetic_run: str, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT-4（1-7）：書きかけの evidence/<資料ID>/ は消す。台帳は変えず（同じ資料IDを次に使う）、data/raw/ の原本は
    消さない。直した後の取得は通る。"""
    site.routes[SPEC_URL] = httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=evidence_pdf_bytes("spec_sample")
    )
    original = pathlib.Path.write_text

    def failing_write(self: pathlib.Path, *args, **kwargs):
        if self.name == "1.txt" and "evidence" in self.parts:
            raise OSError("合成の書き込みの失敗")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", failing_write)
    evidence_dir = repo / "data" / "runs" / synthetic_run / "evidence"
    before = sorted(p.name for p in evidence_dir.iterdir()) if evidence_dir.is_dir() else []
    with pytest.raises(OSError):
        fetcher.fetch_evidence(repo, synthetic_run, SPEC_URL, "evidence", transport=site.transport)
    after = sorted(p.name for p in evidence_dir.iterdir()) if evidence_dir.is_dir() else []
    assert after == before
    assert sources_for(repo, synthetic_run, SPEC_URL) == []
    raw = list((repo / "data" / "raw").glob("*.pdf"))
    raw_before = {p.name for p in raw}

    monkeypatch.setattr(pathlib.Path, "write_text", original)
    result = fetcher.fetch_evidence(
        repo, synthetic_run, SPEC_URL, "evidence", transport=site.transport
    )
    assert (repo / result["pages_dir"] / "1.txt").is_file()
    assert raw_before <= {p.name for p in (repo / "data" / "raw").glob("*.pdf")}


def test_ft4_existing_evidence_folders_are_kept_on_failure(
    repo: Path, synthetic_run: str, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT-4（1-7）：失敗したときに消すのは、その取得が新しく作ったフォルダーだけ。登録済みの資料のページ別テキストは
    残す。"""
    site.routes[SPEC_URL] = httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=evidence_pdf_bytes("spec_sample")
    )
    first = fetcher.fetch_evidence(
        repo, synthetic_run, SPEC_URL, "evidence", transport=site.transport
    )
    kept = repo / first["pages_dir"] / "1.txt"
    other = "https://www.cao.go.jp/test/spec2.pdf"
    site.routes[other] = site.routes[SPEC_URL]
    original = pathlib.Path.write_text

    def failing_write(self: pathlib.Path, *args, **kwargs):
        if self.name == "1.txt" and first["source_id"] not in self.parts:
            raise OSError("合成の書き込みの失敗")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", failing_write)
    with pytest.raises(OSError):
        fetcher.fetch_evidence(repo, synthetic_run, other, "definition", transport=site.transport)
    assert kept.is_file()
