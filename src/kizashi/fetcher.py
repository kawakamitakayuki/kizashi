"""取得（implementation_spec.md 2章の fetcher.py、4.2、3.6）。

- 許可リスト（dataset.yaml の fetch.allowed_hosts）の HTTPS だけ。
- リダイレクトは自動で追わず、1段ずつ許可リストで検査する（最大5段）。
- 1応答25MBを超えたら打ち切る。実行全体の合計が100MBを超えたら E_LIMIT_BYTES。
  通信は実行全体で40回まで（リダイレクトの各段と再試行を含めて数える）。
- 一時的な通信の失敗（接続・読み込みの失敗、時間切れ、HTTP 5xx）だけを、2回まで再試行する。
- 一時ファイルに保存し、形式（拡張子と中身の先頭）、空でないこと、HTML のエラーページでないことと、
  分野の検査（期待する見出し・年度）を通してから data/raw/<sha256>.<拡張子> に移す。上書きしない。
- 取得に失敗した資料も sources に failed と理由を残す。ハッシュは求めない。
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

import httpx

from kizashi import limits, paths
from kizashi.errors import KizashiError
from kizashi.hashing import sha256_bytes
from kizashi.ledger import now_iso

TIMEOUT_SEC = 30.0
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
EXTENSIONS = {"csv": (".csv",), "pdf": (".pdf",), "html": (".html", ".htm")}


@dataclass
class Budget:
    """実行全体の通信の計上（runs.limits_json）。"""

    http_requests: int = 0
    bytes_total: int = 0
    evidence_fetches: int = 0

    @classmethod
    def from_json(cls, data: dict | None) -> Budget:
        data = data or {}
        return cls(
            http_requests=int(data.get("http_requests", 0)),
            bytes_total=int(data.get("bytes_total", 0)),
            evidence_fetches=int(data.get("evidence_fetches", 0)),
        )

    def to_json(self) -> dict:
        return {
            "http_requests": self.http_requests,
            "bytes_total": self.bytes_total,
            "evidence_fetches": self.evidence_fetches,
        }

    def count_request(self) -> None:
        if self.http_requests >= limits.MAX_HTTP_REQUESTS:
            raise KizashiError(
                "E_LIMIT_HTTP",
                "fetch",
                f"通信の回数が実行の上限（{limits.MAX_HTTP_REQUESTS}回）に達した。",
                next_action="保留にして記者に伝える。新しい実行で取り直す。",
            )
        self.http_requests += 1

    def add_bytes(self, n: int) -> None:
        self.bytes_total += n
        if self.bytes_total > limits.MAX_RUN_BYTES:
            raise KizashiError(
                "E_LIMIT_BYTES",
                "fetch",
                f"取得した量が実行の上限（{limits.MAX_RUN_BYTES // (1024 * 1024)}MB）を超えた。",
                next_action="保留にして記者に伝える。",
            )


@dataclass(frozen=True)
class Download:
    url: str
    final_url: str
    sha256: str
    path: Path
    size_bytes: int
    media_type: str | None


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """リダイレクトを自動で追わない HTTP クライアント。"""
    from kizashi.versions import code_version

    return httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(TIMEOUT_SEC),
        headers={"User-Agent": f"kizashi/{code_version()}"},
        transport=transport,
    )


# ---------------------------------------------------------------------------
# 許可リスト
# ---------------------------------------------------------------------------


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    """`*.go.jp` は go.jp の下のホスト（go.jp そのものは含まない）。それ以外は完全一致。"""
    host = host.lower().rstrip(".")
    for pattern in allowed_hosts:
        pattern = pattern.lower()
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == pattern:
            return True
    return False


def check_url(
    url: str | httpx.URL, allowed_hosts: Iterable[str], *, redirect: bool = False
) -> httpx.URL:
    """許可リストの HTTPS の URL かを確かめる。違えば E_HOST_NOT_ALLOWED（リダイレクト先なら E_REDIRECT_NOT_ALLOWED）。"""
    code = "E_REDIRECT_NOT_ALLOWED" if redirect else "E_HOST_NOT_ALLOWED"
    where = "リダイレクト先" if redirect else "取得先"
    try:
        parsed = httpx.URL(str(url))
    except (httpx.InvalidURL, TypeError) as exc:
        raise KizashiError(code, "fetch", f"{where}の URL として読めない：{url}") from exc
    allowed = list(allowed_hosts)
    if (
        parsed.scheme != "https"
        or not parsed.host
        or parsed.userinfo
        or not host_allowed(parsed.host, allowed)
    ):
        raise KizashiError(
            code,
            "fetch",
            f"{where}が許可リスト（{', '.join(allowed)} の HTTPS）にない：{url}",
            next_action="許可リストの公的サイトの資料だけを取得する。",
        )
    return parsed


# ---------------------------------------------------------------------------
# 通信
# ---------------------------------------------------------------------------


class _Transient(Exception):
    pass


def _fetch_failed(url: object, why: str, *, retryable: bool) -> KizashiError:
    return KizashiError(
        "E_FETCH_FAILED",
        "fetch",
        f"取得に失敗した（{why}）：{url}",
        retryable=retryable,
        next_action=(
            "時間をおいて再試行する（kizashi run --resume）。"
            if retryable
            else "URL と公表ページを確かめる。"
        ),
    )


def _get_once(
    client: httpx.Client, url: httpx.URL, budget: Budget
) -> tuple[int, httpx.Headers, bytes]:
    try:
        with client.stream("GET", url) as response:
            status = response.status_code
            if status != 200:
                return status, response.headers, b""
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limits.MAX_RESPONSE_BYTES:
                raise _fetch_failed(url, "応答が25MBを超える", retryable=False)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limits.MAX_RESPONSE_BYTES:
                    raise _fetch_failed(url, "応答が25MBを超えたので打ち切った", retryable=False)
                budget.add_bytes(len(chunk))
                chunks.append(chunk)
            return status, response.headers, b"".join(chunks)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        raise _Transient(f"{type(exc).__name__}") from exc


def _request(
    client: httpx.Client, url: httpx.URL, budget: Budget
) -> tuple[int, httpx.Headers, bytes]:
    """1段の GET。一時的な失敗だけを MAX_RETRIES 回まで再試行する。"""
    why = ""
    for _ in range(limits.MAX_RETRIES + 1):
        budget.count_request()
        try:
            status, headers, content = _get_once(client, url, budget)
        except _Transient as exc:
            why = f"通信の失敗：{exc}"
            continue
        if status >= 500:
            why = f"HTTP {status}"
            continue
        return status, headers, content
    raise _fetch_failed(url, f"{why}。{limits.MAX_RETRIES}回再試行した", retryable=True)


def http_get(
    client: httpx.Client, url: str, *, allowed_hosts: Iterable[str], budget: Budget
) -> tuple[str, bytes, str | None]:
    """許可リストとリダイレクトを1段ずつ検査して GET する。(最終の URL, 中身, media type) を返す。"""
    allowed = list(allowed_hosts)
    current = check_url(url, allowed)
    for _ in range(limits.MAX_REDIRECTS + 1):
        status, headers, content = _request(client, current, budget)
        if status in REDIRECT_STATUSES:
            location = headers.get("location")
            if not location:
                raise _fetch_failed(current, f"HTTP {status} に Location がない", retryable=False)
            current = check_url(current.join(location), allowed, redirect=True)
            continue
        if status != 200:
            raise _fetch_failed(current, f"HTTP {status}", retryable=False)
        media_type = headers.get("content-type")
        return str(current), content, media_type.split(";")[0].strip() if media_type else None
    raise _fetch_failed(url, f"リダイレクトが{limits.MAX_REDIRECTS}段を超えた", retryable=False)


# ---------------------------------------------------------------------------
# 中身の検査と保存
# ---------------------------------------------------------------------------


def _invalid(why: str) -> KizashiError:
    return KizashiError(
        "E_FETCH_INVALID",
        "fetch",
        f"取得したファイルが期待と違う：{why}",
        next_action="公表ページと取得したファイルを記者と確かめる。",
    )


def _looks_html(content: bytes) -> bool:
    head = content[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html", b"<?xml")) or b"<html" in head[:512]


def check_content(url: str, content: bytes, fmt: str) -> None:
    """形式（拡張子と中身の先頭）、空でないこと、HTML のエラーページでないこと（4.2）。"""
    suffix = PurePosixPath(httpx.URL(url).path).suffix.lower()
    if suffix not in EXTENSIONS[fmt]:
        raise _invalid(f"URL の拡張子が {fmt} でない：{url}")
    if not content:
        raise _invalid("空のファイル")
    if fmt == "html":
        if not _looks_html(content) and b"<html" not in content[:8192].lower():
            raise _invalid("HTML のページでない")
        return
    if _looks_html(content):
        raise _invalid("HTML のページ（エラーページなど）が返った")
    if fmt == "pdf" and not content.startswith(b"%PDF-"):
        raise _invalid("PDF の先頭（%PDF-）がない")


def fetch_to_raw(
    root: Path,
    client: httpx.Client,
    url: str,
    *,
    fmt: str,
    allowed_hosts: Iterable[str],
    budget: Budget,
    validate: Callable[[bytes], None] | None = None,
) -> Download:
    """取得し、一時ファイルに保存して検査してから data/raw/<sha256>.<拡張子> に移す。"""
    final_url, content, media_type = http_get(
        client, url, allowed_hosts=allowed_hosts, budget=budget
    )
    raw = paths.raw_dir(root)
    raw.mkdir(parents=True, exist_ok=True)
    paths.ensure_in_data(root, raw)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=raw)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        saved = tmp.read_bytes()
        check_content(url, saved, fmt)
        if validate is not None:
            validate(saved)
        sha = sha256_bytes(saved)
        final = raw / f"{sha}.{fmt}"
        if final.exists():
            tmp.unlink()  # 同じ中身の原本がある。上書きしない
        else:
            os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return Download(
        url=url,
        final_url=final_url,
        sha256=sha,
        path=final,
        size_bytes=len(saved),
        media_type=media_type,
    )


# ---------------------------------------------------------------------------
# リンクの取り出し
# ---------------------------------------------------------------------------


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            for name, value in attrs:
                if name.lower() == "href" and value:
                    self.hrefs.append(value.strip())


def discover_links(page_url: str, html_text: str, pattern: str) -> list[dict[str, str]]:
    """ページの中のリンクを絶対 URL にし、パターンに合うものを、名前付きのグループと一緒に返す。"""
    parser = _LinkParser()
    parser.feed(html_text)
    base = httpx.URL(page_url)
    regex = re.compile(pattern)
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        try:
            absolute = str(base.join(href))
        except httpx.InvalidURL:
            continue
        match = regex.search(absolute)
        if match is None or absolute in seen:
            continue
        seen.add(absolute)
        found.append(
            {"url": absolute, **{k: v for k, v in match.groupdict().items() if v is not None}}
        )
    return found


# ---------------------------------------------------------------------------
# sources への登録
# ---------------------------------------------------------------------------


def register_fetched(
    conn: sqlite3.Connection,
    source_id: str,
    download: Download,
    *,
    referrer_url: str | None,
    encoding: str | None,
) -> None:
    conn.execute(
        "UPDATE sources SET url = ?, final_url = ?, referrer_url = ?, fetched_at = ?, sha256 = ?,"
        " media_type = ?, encoding = ?, size_bytes = ?, status = 'fetched', failure_reason = NULL"
        " WHERE source_id = ?",
        (
            download.url,
            download.final_url,
            referrer_url,
            now_iso(),
            download.sha256,
            download.media_type,
            encoding,
            download.size_bytes,
            source_id,
        ),
    )


def register_failure(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    status: str,
    reason: str,
    url: str | None = None,
    referrer_url: str | None = None,
) -> None:
    """取得できなかった資料を failed / not_published と理由で残す。ハッシュは求めない。"""
    conn.execute(
        "UPDATE sources SET url = COALESCE(?, url), referrer_url = COALESCE(?, referrer_url),"
        " fetched_at = ?, sha256 = NULL, size_bytes = NULL, status = ?, failure_reason = ?"
        " WHERE source_id = ?",
        (url, referrer_url, now_iso(), status, reason, source_id),
    )


def error_reason(exc: KizashiError) -> str:
    return f"{exc.code}: {exc.message}"
