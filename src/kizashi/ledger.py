"""台帳（SQLite）の接続、スキーマの作成、IDの採番、トランザクション、events への追記。

implementation_spec.md 2章の ledger.py、3.3（ID）、5章（DDL）。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from kizashi import paths
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
LEDGER_NAME = "ledger.sqlite"

# 3.3 の ID の書式。
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[a-z0-9]{4}$")
ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "run": RUN_ID_RE,
    "source": re.compile(r"^S\d{3}$"),
    "record": re.compile(r"^S\d{3}:\d{6}$"),
    "calc": re.compile(r"^K\d{4}$"),
    "lead": re.compile(r"^L\d{4}$"),
    "claim": re.compile(r"^C\d{4}$"),
    "match": re.compile(r"^M\d{3}$"),
}

# 採番する ID の接頭辞 → (表, 列, 桁数)。
_ID_TABLES: dict[str, tuple[str, str, int]] = {
    "S": ("sources", "source_id", 3),
    "K": ("computations", "calc_id", 4),
    "L": ("leads", "lead_id", 4),
    "C": ("claims", "claim_id", 4),
    "M": ("procurement_matches", "match_id", 3),
}

EVENT_KINDS = (
    "state_change",
    "record",
    "query",
    "check",
    "render",
    "correction",
    "recheck",
    "resume",
    "stage",
)


def id_kind(value: str) -> str | None:
    """ID の種類（run・source・record・calc・lead・claim・match）。書式に合わなければ None。"""
    for kind, pattern in ID_PATTERNS.items():
        if pattern.fullmatch(value):
            return kind
    return None


def now_iso() -> str:
    """日時の記録（現地時刻、時差付き、秒まで）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def record_id(source_id: str, logical_no: int) -> str:
    """row_ref（資料ID＋「:」＋論理レコード番号6桁。3.3）。"""
    return f"{source_id}:{logical_no:06d}"


def decimal_cmp(a: object, b: object) -> int | None:
    """2つの値を十進数（Decimal）として比べ、-1・0・1 を返す（D-081。分野の SQL が使う decimal_cmp）。

    SQLite の REAL にすると、十進数では異なる値（1000000000.0000003 と …004）が同じになるため。
    どちらかが NULL、空、十進数として読めない、有限でない（NaN・無限大）なら None（SQL の NULL）。
    """
    if a is None or b is None:
        return None
    try:
        x, y = Decimal(str(a)), Decimal(str(b))
    except InvalidOperation:
        return None
    if not (x.is_finite() and y.is_finite()):
        return None
    return (x > y) - (x < y)


def register_functions(conn: sqlite3.Connection) -> None:
    """台帳の接続に、分野の SQL が使う関数を登録する（D-081）。"""
    conn.create_function("decimal_cmp", 2, decimal_cmp, deterministic=True)


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    register_functions(conn)


def ledger_path(root: Path, run_id: str) -> Path:
    return paths.run_dir(root, run_id) / LEDGER_NAME


def create(root: Path, run_id: str) -> sqlite3.Connection:
    """実行フォルダーと台帳を作り、スキーマを作成して、書き込み用の接続を返す。"""
    run_dir = paths.run_dir(root, run_id)
    paths.ensure_in_data(root, run_dir.parent)
    run_dir.mkdir(parents=True, exist_ok=False)
    paths.ensure_in_data(root, run_dir)
    conn = sqlite3.connect(run_dir / LEDGER_NAME, isolation_level=None)
    _configure(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def open_rw(root: Path, run_id: str) -> sqlite3.Connection:
    """既存の台帳を書き込み用に開く。台帳がなければ E_REF_NOT_FOUND。"""
    path = ledger_path(root, run_id)
    if not path.is_file():
        raise KizashiError(
            "E_REF_NOT_FOUND",
            "cli",
            f"実行の台帳がない：{run_id}",
            affected_ids=[run_id],
            next_action="実行IDを確かめる。",
        )
    paths.ensure_in_data(root, path)
    conn = sqlite3.connect(path, isolation_level=None)
    _configure(conn)
    return conn


def open_ro(root: Path, run_id: str) -> sqlite3.Connection:
    """既存の台帳を読み取り専用で開く（URI の mode=ro と PRAGMA query_only）。"""
    path = ledger_path(root, run_id)
    if not path.is_file():
        raise KizashiError(
            "E_REF_NOT_FOUND",
            "cli",
            f"実行の台帳がない：{run_id}",
            affected_ids=[run_id],
            next_action="実行IDを確かめる。",
        )
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = ON")
    register_functions(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """1つのトランザクション。例外が起きたら巻き戻す。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_id(conn: sqlite3.Connection, prefix: str) -> str:
    """接頭辞ごとの次の ID（S001、K0001 など。3.3）。"""
    table, column, width = _ID_TABLES[prefix]
    # 表と列の名前は上の固定の表から取る（利用者の入力を埋め込まない）。
    row = conn.execute(f"SELECT MAX(CAST(substr({column}, 2) AS INTEGER)) FROM {table}").fetchone()
    number = (row[0] or 0) + 1
    if number >= 10**width:
        raise KizashiError(
            "E_INTERNAL",
            "cli",
            f"{prefix} の ID が桁数（{width}桁）を超える。",
            next_action="開発側で原因を調べる。",
        )
    return f"{prefix}{number:0{width}d}"


def add_event(
    conn: sqlite3.Connection,
    target_id: str,
    kind: str,
    *,
    before: object = None,
    after: object = None,
    reason: str | None = None,
    result: object = None,
    check_hash: str | None = None,
) -> None:
    """events（追記専用）に1行を足す。"""
    if kind not in EVENT_KINDS:
        raise ValueError(f"未知の events.kind: {kind}")
    conn.execute(
        "INSERT INTO events (target_id, kind, before_json, after_json, reason, result_json,"
        " check_hash, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target_id,
            kind,
            None if before is None else canonical_json(before),
            None if after is None else canonical_json(after),
            reason,
            None if result is None else canonical_json(result),
            check_hash,
            now_iso(),
        ),
    )
