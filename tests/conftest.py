"""テストの共通の準備。通信は httpx.MockTransport で合成のページと CSV を返す（実データは使わない）。

PDF のライブラリはテストで動かさない（D-002）。pdftext.load_pages を、合成の罫線と文字の座標
（tests/fixtures/pdf_cells/*.json）を返す関数に替える（自動で全テストに効く）。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from kizashi import pdftext

REPO = Path(__file__).resolve().parents[1]
DATASET_DIR = REPO / "datasets" / "cao-procurement"
FIXTURES = DATASET_DIR / "fixtures"
WEB = Path(__file__).resolve().parent / "fixtures" / "web"
PDF_CELLS = Path(__file__).resolve().parent / "fixtures" / "pdf_cells"

# 合成の PDF（pdf_bytes の印）→ 合成の座標のファイル
SYNTHETIC_PDFS = {
    "k24_pdf": "kyoso_2024_mini",
    "z24_pdf": "zuikei_2024_mini",
    "k25_pdf": "kyoso_2025_mini",
    "z25_pdf": "zuikei_2025_mini",
}

PAGE = "https://www.cao.go.jp/chotatsu/kohyo/kohyo.html"
BASE = "https://www.cao.go.jp/chotatsu/kohyo/"
URL = {
    "k24_csv": BASE + "tekiseika/6nendo/20250711-2_kohyo_kyoso.csv",
    "k24_pdf": BASE + "tekiseika/6nendo/20250711-2_kohyo_kyoso.pdf",
    "z24_csv": BASE + "tekiseika/6nendo/20250514_kohyo_zuikei.csv",
    "z24_pdf": BASE + "tekiseika/6nendo/20250514_kohyo_zuikei.pdf",
    "k25_csv": BASE + "tekiseika/7nendo/20260512-02_kohyo_kyoso.csv",
    "k25_pdf": BASE + "tekiseika/7nendo/20260512-02_kohyo_kyoso.pdf",
    "z25_csv": BASE + "tekiseika/7nendo/20260512-03_kohyo_zuikei.csv",
    "z25_pdf": BASE + "tekiseika/7nendo/20260512-03_kohyo_zuikei.pdf",
}

# 担当官の架空の氏名の語（make_cao_fixtures.py）。台帳のどこにもあってはならない。
NAME_WORDS = ("架空", "太郎", "仮名", "花子", "見本", "三郎", "空想", "四郎", "試験次郎", "次郎")
# 取り込まない列にだけある語（担当官の所在地、相手方の住所、随意契約の内部用の列）。
DROPPED_WORDS = ("官庁町", "模擬市", "会計第一係")

RUN_CONFIG = {
    "kind": "run_config",
    "domain": "cao-procurement",
    "compare_years": [2024, 2025],
    "series_years": [2024, 2025],
    "question": "同種の業務で、契約金額・契約方式・随意契約の理由が年度間で変わったものはあるか",
    "model": "テスト用のモデル名",
}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def pdf_bytes(name: str) -> bytes:
    """合成の PDF。中身は印だけで、読み込み（pdftext.load_pages）は合成の座標に替える。"""
    return f"%PDF-1.4\n% synthetic test pdf {name}\n".encode("ascii")


def pdf_pages(name: str) -> list[pdftext.Page]:
    """tests/fixtures/pdf_cells/<name>.json を、pdftext のページの一覧にする。"""
    doc = json.loads((PDF_CELLS / f"{name}.json").read_text(encoding="utf-8"))
    return [
        pdftext.Page(
            number=p["number"],
            rects=tuple(pdftext.Rect(*r) for r in p["rects"]),
            chars=tuple(pdftext.Char(*c) for c in p["chars"]),
        )
        for p in doc["pages"]
    ]


@pytest.fixture(autouse=True)
def synthetic_pdf_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF のライブラリを動かさない。印のない PDF は、読めない PDF として例外にする。"""

    def load(path: Path) -> list[pdftext.Page]:
        found = re.search(rb"synthetic test pdf (\w+)", Path(path).read_bytes())
        name = found.group(1).decode("ascii") if found else None
        if name not in SYNTHETIC_PDFS:
            raise ValueError("合成の PDF でない（読めない PDF）")
        return pdf_pages(SYNTHETIC_PDFS[name])

    monkeypatch.setattr(pdftext, "load_pages", load)


EVIDENCE = Path(__file__).resolve().parent / "fixtures" / "evidence"


def evidence_pdf_bytes(name: str) -> bytes:
    """合成の追加資料の PDF。中身は印だけで、ページ別テキストは tests/fixtures/evidence/<name>.txt
    （ページを \\f で区切ったもの）に替える。"""
    return f"%PDF-1.4\n% synthetic evidence pdf {name}\n".encode("ascii")


@pytest.fixture(autouse=True)
def synthetic_evidence_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """追加資料の PDF のテキストの抽出でも PDF のライブラリを動かさない。印のない PDF は読めない PDF。"""

    def extract(path: Path) -> list[str]:
        found = re.search(rb"synthetic evidence pdf (\w+)", Path(path).read_bytes())
        if found is None or not (EVIDENCE / f"{found.group(1).decode('ascii')}.txt").is_file():
            raise ValueError("合成の追加資料の PDF でない（読めない PDF）")
        text = (EVIDENCE / f"{found.group(1).decode('ascii')}.txt").read_text(encoding="utf-8")
        return text.split("\f")

    monkeypatch.setattr(pdftext, "extract_pages", extract)


def default_routes() -> dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]]:
    csv = {"content-type": "text/csv"}
    pdf = {"content-type": "application/pdf"}
    return {
        PAGE: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=UTF-8"},
            content=(WEB / "kohyo.html").read_bytes(),
        ),
        URL["k24_csv"]: httpx.Response(
            200, headers=csv, content=fixture_bytes("kyoso_2024_mini.csv")
        ),
        URL["z24_csv"]: httpx.Response(
            200, headers=csv, content=fixture_bytes("zuikei_2024_mini.csv")
        ),
        URL["k25_csv"]: httpx.Response(
            200, headers=csv, content=fixture_bytes("kyoso_2025_mini.csv")
        ),
        URL["z25_csv"]: httpx.Response(
            200, headers=csv, content=fixture_bytes("zuikei_2025_mini.csv")
        ),
        **{
            URL[k]: httpx.Response(200, headers=pdf, content=pdf_bytes(k))
            for k in ("k24_pdf", "z24_pdf", "k25_pdf", "z25_pdf")
        },
    }


class Site:
    """合成のサイト。届いた要求の URL を requests に残す。登録のない URL は 404。"""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = default_routes()
        self.routes.update(routes or {})
        self.requests: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, content=b"not found")
        if callable(route):
            return route(request)
        return httpx.Response(route.status_code, headers=route.headers, content=route.content)


@pytest.fixture
def site() -> Site:
    return Site()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """リポジトリの直下と見なされるフォルダー（pyproject.toml、datasets/cao-procurement/dataset.yaml、inbox/）。"""
    root = tmp_path / "repo"
    (root / "datasets" / "cao-procurement").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "fake"\n', encoding="utf-8")
    shutil.copy(
        DATASET_DIR / "dataset.yaml", root / "datasets" / "cao-procurement" / "dataset.yaml"
    )
    (root / "inbox").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.delenv("KIZASHI_PROFILE", raising=False)
    monkeypatch.delenv("KIZASHI_CLAUDE_VERSION", raising=False)
    return root


def write_config(root: Path, name: str = "run-config.json", **overrides: object) -> str:
    config = {**RUN_CONFIG, **overrides}
    config = {k: v for k, v in config.items() if v is not None}
    (root / "inbox" / name).write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return f"inbox/{name}"


@pytest.fixture
def synthetic_run(repo: Path, site: Site) -> str:
    """合成データ（令和6・7年度）で completed にした実行の ID。資料は S001〜S009（test_run.py）。"""
    from kizashi import runner

    result = runner.start_run(repo, "cao-procurement", write_config(repo), transport=site.transport)
    assert result["state"] == "completed"
    return result["run_id"]


def add_evidence(
    root: Path,
    run_id: str,
    pages: list[str],
    *,
    kind: str = "evidence",
    url: str = "https://www.cao.go.jp/test/spec.pdf",
) -> str:
    """取得を通さずに、追加資料（ページ別テキスト付き）を台帳に足す（引用の照合のテスト用）。"""
    from kizashi import ledger

    conn = ledger.open_rw(root, run_id)
    try:
        with ledger.transaction(conn):
            source_id = ledger.next_id(conn, "S")
            conn.execute(
                "INSERT INTO sources (source_id, kind, url, final_url, fetched_at, sha256, media_type,"
                " size_bytes, page_count, status) VALUES (?, ?, ?, ?, ?, ?, 'application/pdf', 1, ?,"
                " 'fetched')",
                (source_id, kind, url, url, ledger.now_iso(), "0" * 64, len(pages)),
            )
    finally:
        conn.close()
    page_dir = root / "data" / "runs" / run_id / "evidence" / source_id / "pages"
    page_dir.mkdir(parents=True)
    for n, text in enumerate(pages, start=1):
        (page_dir / f"{n}.txt").write_text(text, encoding="utf-8")
    return source_id


def ledger_of(root: Path, run_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(root / "data" / "runs" / run_id / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def all_text_in_ledger(conn: sqlite3.Connection) -> str:
    """台帳のすべての表のすべての値をつないだ文字列（全文検索用）。"""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    parts: list[str] = []
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):
            parts.extend(str(v) for v in row if v is not None)
    return "\n".join(parts)


def all_bytes_under(path: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file())


# ---------------------------------------------------------------------------
# 提案の登録の手順（record・inspect・KN-2 のテストで使う）
# ---------------------------------------------------------------------------


def put(root: Path, name: str, data: dict) -> str:
    (root / "inbox" / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return f"inbox/{name}"


def rec(root: Path, run_id: str, data: dict, name: str = "p.json") -> dict:
    from kizashi import record

    return record.record(root, run_id, put(root, name, data))


def calc_sum(group: str, year: int, **extra) -> dict:
    return {
        "kind": "calc",
        "op": "sum",
        "table": "procurement_contracts",
        "column": "amount_yen",
        "filter": [
            {"col": "group_key", "op": "=", "value": group},
            {"col": "fiscal_year", "op": "=", "value": year},
        ],
        "label": f"{group}の契約金額の合計",
        "unit": "円",
        **extra,
    }


COMPARISON = {
    "population": "内閣府の競争入札・随意契約のうち、件名のまとめのキーが両年度にある契約",
    "row_meaning": "1件の契約",
    "period": "会計年度",
    "unit": "円",
    "entity_mapping": "件名のまとめのキー",
    "formula": "年度ごとの契約金額の合計の差",
}


def make_lead(root: Path, run_id: str, group: str = "翻訳業務", qname: str = "q-001.sql") -> dict:
    """探索SQL → 両年度の計算 → 候補（screen_id='explore'）。計算ID と候補ID を返す。"""
    (root / "inbox" / qname).write_text(
        f"SELECT fiscal_year, amount_yen FROM procurement_contracts WHERE group_key = '{group}'",
        encoding="utf-8",
    )
    from kizashi import query

    query.run_query(root, run_id, f"inbox/{qname}")
    k_from = rec(root, run_id, calc_sum(group, 2024))["id"]
    k_to = rec(root, run_id, calc_sum(group, 2025))["id"]
    lead = rec(
        root,
        run_id,
        {
            "kind": "lead",
            "query_file": qname,
            "lead_key": group,
            "change_category": "amount_up",
            "period_from": 2024,
            "period_to": 2025,
            "calc_from": k_from,
            "calc_to": k_to,
            "calc_ids": [],
            "comparison": COMPARISON,
            "note": "探索で見つけた",
        },
    )
    return {"lead": lead["id"], "from": k_from, "to": k_to}
