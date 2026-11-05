"""取得（implementation_spec.md 2章の fetcher.py、4.2、3.6）。

- 許可リスト（dataset.yaml の fetch.allowed_hosts）の HTTPS だけ。
- リダイレクトは自動で追わず、1段ずつ許可リストで検査する（最大5段）。
- 1応答25MBを超えたら打ち切る。実行全体の合計が100MBを超えたら E_LIMIT_BYTES。
  通信は実行全体で40回まで（リダイレクトの各段と再試行を含めて数える）。
- 一時的な通信の失敗（接続・読み込みの失敗、時間切れ、HTTP 5xx）だけを、2回まで再試行する。
- 一時ファイルに保存し、形式（拡張子と中身の先頭）、空でないこと、HTML のエラーページでないことと、
  分野の検査（期待する見出し・年度）を通してから data/raw/<sha256>.<拡張子> に移す。上書きしない。
- 取得に失敗した資料も sources に failed と理由を残す。ハッシュは求めない。
- 追加資料（fetch_evidence）：PDF と HTML のページ別テキストを evidence/<資料ID>/pages/<n>.txt に書く。
  公表一覧のデータは取得しない（D-021）。追加資料だけ MAX_EVIDENCE_FETCH に数える。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

import httpx

from kizashi import ledger, limits, paths, pdftext
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json, sha256_bytes
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
    client: httpx.Client,
    url: str,
    *,
    allowed_hosts: Iterable[str],
    budget: Budget,
    refuse_redirect: Callable[[httpx.URL], None] | None = None,
) -> tuple[str, bytes, str | None]:
    """許可リストとリダイレクトを1段ずつ検査して GET する。(最終の URL, 中身, media type) を返す。

    refuse_redirect は、リダイレクト先に通信する前に呼ぶ検査（拒否するときは KizashiError を出す）。
    """
    allowed = list(allowed_hosts)
    current = check_url(url, allowed)
    for _ in range(limits.MAX_REDIRECTS + 1):
        status, headers, content = _request(client, current, budget)
        if status in REDIRECT_STATUSES:
            location = headers.get("location")
            if not location:
                raise _fetch_failed(current, f"HTTP {status} に Location がない", retryable=False)
            current = check_url(current.join(location), allowed, redirect=True)
            if refuse_redirect is not None:
                refuse_redirect(current)
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


def check_content(url: str, content: bytes, fmt: str, *, check_suffix: bool = True) -> None:
    """形式（拡張子と中身の先頭）、空でないこと、HTML のエラーページでないこと（4.2）。

    check_suffix が False のとき（拡張子のない追加資料の URL）は、中身の先頭だけで確かめる。
    """
    suffix = PurePosixPath(httpx.URL(url).path).suffix.lower()
    if check_suffix and suffix not in EXTENSIONS[fmt]:
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
    sha, final, size = store_raw(root, url, content, fmt, validate=validate)
    return Download(
        url=url,
        final_url=final_url,
        sha256=sha,
        path=final,
        size_bytes=size,
        media_type=media_type,
    )


def store_raw(
    root: Path,
    url: str,
    content: bytes,
    fmt: str,
    *,
    validate: Callable[[bytes], None] | None = None,
    check_suffix: bool = True,
) -> tuple[str, Path, int]:
    """一時ファイルに保存して検査し、data/raw/<sha256>.<拡張子> に移す。(SHA-256, パス, 大きさ) を返す。"""
    raw = paths.raw_dir(root)
    raw.mkdir(parents=True, exist_ok=True)
    paths.ensure_in_data(root, raw)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=raw)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        saved = tmp.read_bytes()
        check_content(url, saved, fmt, check_suffix=check_suffix)
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
    return sha, final, len(saved)


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


# ---------------------------------------------------------------------------
# 追加資料の取得（kizashi fetch <run_id> <URL> [--index | --definition]。4.2）
# ---------------------------------------------------------------------------

EVIDENCE_KINDS = ("evidence", "definition", "index")
MAX_LINKS_SHOWN = 100  # 出力に載せるリンクの数。全部は evidence/<資料ID>/links.json に書く
_MEDIA_FORMATS = {"application/pdf": "pdf", "text/html": "html", "application/xhtml+xml": "html"}
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", re.IGNORECASE)
_SJIS_NAMES = {"shift_jis", "shift-jis", "sjis", "x-sjis", "windows-31j", "ms932"}
BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "caption", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "td", "th", "title", "tr", "ul",
    }
)  # fmt: skip
SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})


def detect_format(
    url: str, final_url: str, media_type: str | None, content: bytes
) -> tuple[str, str | None]:
    """追加資料の形式（pdf か html）と、拡張子で決めたときはその URL を返す。

    拡張子（.pdf・.html・.htm）があればそれで決め、なければ応答の種類と中身の先頭で決める。
    """
    for candidate in (url, final_url):
        suffix = PurePosixPath(httpx.URL(candidate).path).suffix.lower()
        for fmt in ("pdf", "html"):
            if suffix in EXTENSIONS[fmt]:
                return fmt, candidate
    if media_type in _MEDIA_FORMATS:
        return _MEDIA_FORMATS[media_type], None
    if content.startswith(b"%PDF-"):
        return "pdf", None
    if _looks_html(content):
        return "html", None
    raise _invalid(
        f"PDF か HTML の資料でない（応答の種類：{media_type}）。追加資料は PDF と HTML だけ"
    )


def decode_html(content: bytes) -> tuple[str, str]:
    """HTML を文字列にする（BOM、meta の charset、UTF-8、cp932、EUC-JP の順に試す）。"""
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    candidates: list[str] = []
    found = _META_CHARSET_RE.search(content[:4096])
    if found:
        name = found.group(1).decode("ascii").lower()
        candidates.append("cp932" if name in _SJIS_NAMES else name)
    candidates += ["utf-8", "cp932", "euc_jp"]
    for encoding in candidates:
        try:
            return content.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace"), "utf-8"


class _TextParser(HTMLParser):
    """HTML の本文とリンクを取り出す。段落（ブロックの要素と表のセル）の間に空行を入れる（引用の照合の
    段落の終わり。quotes.py）。スクリプトとスタイルの中身は捨てる。"""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = httpx.URL(base_url)
        self.parts: list[str] = []
        self.skip = 0
        self.links: list[dict[str, str]] = []
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n\n")
        if tag == "a":
            href = next((v for k, v in attrs if k.lower() == "href" and v), None)
            self._anchor = (href.strip(), []) if href else None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n\n")
        if tag == "a" and self._anchor is not None:
            href, texts = self._anchor
            self._anchor = None
            if href.startswith(("#", "javascript:", "mailto:")):
                return
            try:
                absolute = str(self.base.join(href))
            except httpx.InvalidURL:
                return
            if all(link["url"] != absolute for link in self.links):
                text = re.sub(r"\s+", " ", "".join(texts)).strip()
                self.links.append({"url": absolute, "text": text[:100]})

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        text = re.sub(r"\s+", " ", data)
        self.parts.append(text)
        if self._anchor is not None:
            self._anchor[1].append(text)

    def text(self) -> str:
        lines = [line.strip() for line in "".join(self.parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def html_text(content: bytes, base_url: str) -> tuple[str, list[dict[str, str]], str]:
    """HTML の (本文のテキスト, リンクの一覧, 文字コード)。"""
    decoded, encoding = decode_html(content)
    parser = _TextParser(base_url)
    parser.feed(decoded)
    parser.close()
    return parser.text(), parser.links, encoding


def is_dataset_url(url: str | httpx.URL, link_pattern: str) -> bool:
    """公表一覧のデータの URL か（dataset.yaml の link_pattern。D-031⑪・D-034）。

    問い合わせ（?…）と断片（#…）を除き、%の符号を戻したパスで、大文字と小文字を区別せずに照らす。
    """
    try:
        parsed = httpx.URL(str(url))
    except (httpx.InvalidURL, TypeError):
        return False
    plain = f"{parsed.scheme}://{parsed.host}{parsed.path}"
    return re.search(link_pattern, plain, re.IGNORECASE) is not None


def _dataset_refused(url: object, *, redirect: bool) -> KizashiError:
    where = "リダイレクト先" if redirect else "取得先"
    return KizashiError(
        "E_BAD_EVIDENCE_SOURCE",
        "fetch",
        f"{where}が公表一覧のデータ（{url}）である。公表一覧のデータは run が取得し、"
        "追加資料としては取得しない（D-021・D-034）。",
        next_action="CSV のレコードを kizashi inspect <row_ref> で読み、row_ref で引用する。",
    )


def _register_evidence_failure(
    conn: sqlite3.Connection, kind: str, url: str, exc: KizashiError, budget: Budget, run_id: str
) -> str:
    with ledger.transaction(conn):
        source_id = ledger.next_id(conn, "S")
        conn.execute(
            "INSERT INTO sources (source_id, kind, url, fetched_at, status, failure_reason)"
            " VALUES (?, ?, ?, ?, 'failed', ?)",
            (source_id, kind, url, now_iso(), error_reason(exc)),
        )
        _save_budget(conn, run_id, budget)
    return source_id


def _save_budget(conn: sqlite3.Connection, run_id: str, budget: Budget) -> None:
    conn.execute(
        "UPDATE runs SET limits_json = ? WHERE run_id = ?",
        (canonical_json(budget.to_json()), run_id),
    )


def fetch_evidence(
    root: Path,
    run_id: str,
    url: str,
    kind: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """追加資料（evidence）・定義（definition）・索引（index）を取得し、ページ別テキストを保存する。

    - 追加資料だけ MAX_EVIDENCE_FETCH に数える（取得に成功したもの）。通信はすべて MAX_HTTP_REQUESTS に数える。
    - PDF はページごと、HTML は1ページ（とリンクの一覧）。evidence/<資料ID>/pages/<n>.txt に書く。
    - 公表一覧のデータ（dataset.yaml の link_pattern に合う URL と、run が取得した資料と同じ中身）は
      取得しない（ページ別テキストを作らない。D-021）。URL は、リダイレクトの途中と最後の URL も、
      通信の前に照らす（D-034）。
    - 取得に失敗したら sources に failed と理由を残す（許可リスト外など、通信の前に拒否したものは残さない）。
    """
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"未知の資料の種類：{kind}")
    conn = ledger.open_rw(root, run_id)
    try:
        run = conn.execute(
            "SELECT domain, limits_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise KizashiError("E_REF_NOT_FOUND", "fetch", f"実行の記録がない：{run_id}")
        adapter = load_adapter(root, run[0])
        budget = Budget.from_json(json.loads(run[1]))
        allowed = list(adapter.config["fetch"]["allowed_hosts"])
        if kind == "evidence" and budget.evidence_fetches >= limits.MAX_EVIDENCE_FETCH:
            raise KizashiError(
                "E_LIMIT_EVIDENCE",
                "fetch",
                f"追加資料の取得が上限（{limits.MAX_EVIDENCE_FETCH}件）に達している。",
                affected_ids=[run_id],
                next_action="保留にして記者に伝える（上限に達しても結論を強めない）。",
            )
        check_url(url, allowed)
        link_pattern = adapter.config["source"]["link_pattern"]
        if is_dataset_url(url, link_pattern):
            raise _dataset_refused(url, redirect=False)

        def refuse_redirect(target: httpx.URL) -> None:
            if is_dataset_url(target, link_pattern):
                raise _dataset_refused(target, redirect=True)

        client = make_client(transport)
        try:
            final_url, content, media_type = http_get(
                client,
                url,
                allowed_hosts=allowed,
                budget=budget,
                refuse_redirect=refuse_redirect,
            )
            fmt, suffix_url = detect_format(url, final_url, media_type, content)
            sha, path, size = store_raw(
                root, suffix_url or url, content, fmt, check_suffix=suffix_url is not None
            )
            same = conn.execute(
                "SELECT source_id FROM sources WHERE kind = 'dataset' AND sha256 = ?", (sha,)
            ).fetchone()
            if same is not None:
                raise KizashiError(
                    "E_BAD_EVIDENCE_SOURCE",
                    "fetch",
                    f"公表一覧の資料（{same[0]}）と同じ中身で、追加資料としては保存しない（D-021）。",
                    next_action="CSV のレコードを kizashi inspect <row_ref> で読み、row_ref で引用する。",
                )
            links: list[dict[str, str]] = []
            encoding = None
            if fmt == "pdf":
                try:
                    pages = pdftext.extract_pages(path)
                except Exception as exc:  # noqa: BLE001  PDF のライブラリの例外をまとめて扱う
                    raise _invalid(f"PDF のテキストを読めない（{type(exc).__name__}）") from None
            else:
                text, links, encoding = html_text(content, final_url)
                pages = [text]
        except KizashiError as exc:
            source_id = _register_evidence_failure(conn, kind, url, exc, budget, run_id)
            raise KizashiError(
                exc.code,
                "fetch",
                exc.message,
                affected_ids=[run_id, source_id],
                retryable=exc.retryable,
                next_action=exc.next_action,
            ) from None
        finally:
            client.close()

        if kind == "evidence":
            budget.evidence_fetches += 1
        run_dir = paths.run_dir(root, run_id)
        written: Path | None = None
        try:
            with ledger.transaction(conn):
                source_id = ledger.next_id(conn, "S")
                written = run_dir / "evidence" / source_id
                page_dir = written / "pages"
                page_dir.mkdir(parents=True)
                paths.ensure_in_data(root, page_dir)
                for number, page in enumerate(pages, start=1):
                    (page_dir / f"{number}.txt").write_text(page, encoding="utf-8")
                if links:
                    (written / "links.json").write_text(
                        json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8"
                    )
                conn.execute(
                    "INSERT INTO sources (source_id, kind, url, final_url, fetched_at, sha256, media_type,"
                    " encoding, size_bytes, page_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " 'fetched')",
                    (
                        source_id,
                        kind,
                        url,
                        final_url,
                        now_iso(),
                        sha,
                        media_type,
                        encoding,
                        size,
                        len(pages),
                    ),
                )
                _save_budget(conn, run_id, budget)
        except BaseException:
            if written is not None:
                shutil.rmtree(written, ignore_errors=True)
            raise
    finally:
        conn.close()
    result = {
        "run_id": run_id,
        "source_id": source_id,
        "kind": kind,
        "url": url,
        "final_url": final_url,
        "sha256": sha,
        "media_type": media_type,
        "page_count": len(pages),
        "pages_dir": (written / "pages").relative_to(root).as_posix(),
        "counted_as_evidence": kind == "evidence",
        "limits": {
            **budget.to_json(),
            "max_evidence_fetches": limits.MAX_EVIDENCE_FETCH,
            "max_http_requests": limits.MAX_HTTP_REQUESTS,
        },
    }
    if fmt == "html":
        result["links"] = links[:MAX_LINKS_SHOWN]
        result["links_total"] = len(links)
        if links:
            result["links_file"] = (written / "links.json").relative_to(root).as_posix()
    return result
