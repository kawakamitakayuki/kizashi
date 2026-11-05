"""探索用の読み取り専用 SQL（implementation_spec.md 2章の query.py、4.4。設計書3.4）。

- 台帳を file:<台帳>?mode=ro で開き、PRAGMA query_only = ON にする。
- SQLite の authorizer で、SELECT と読み取り（READ）、関数（拡張の読み込みを除く）、再帰の CTE だけを
  許す。INSERT・UPDATE・DELETE・CREATE・DROP・ATTACH（VACUUM INTO を含む）・PRAGMA などは拒否する。
- 1つの文だけを実行する。文字列とコメントを除いたうえで ; の区切りを数え、SELECT か WITH で始まる
  ことを確かめる。sqlite3 の execute も2つ目の文を拒否する。
- 結果は最大50行。実行時間が10秒を超えたら中断する（set_progress_handler）。
- 成功したら SQL と結果を queries/ に保存し、events に記録し、SQL を accepted/<名前> に写して inbox/
  から消す。結果は citable: false（メモの数値に使えない）。
- 実行の前に、SQL の全文（文字列・コメントを含む）に V7 の氏名の検査を当て、当たれば E_V7 で拒否する。
  accepted/ にも queries/ にも保存しない（D-037）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

from kizashi import check, inbox, ledger, limits, paths
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError

STAGE = "query"
PROGRESS_STEPS = 1000

_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)
_DENIED_FUNCTIONS = frozenset({"load_extension"})


def _rejected(message: str, next_action: str = "") -> KizashiError:
    return KizashiError(
        "E_QUERY_REJECTED",
        STAGE,
        message,
        next_action=next_action or "読み取りの SELECT（WITH を含む）を1つだけ書く。",
    )


def skeleton(sql: str) -> str:
    """文字列・引用符付きの名前・コメントを取り除いた SQL（区切りと先頭の語を見るため）。

    閉じていない文字列やコメントは E_QUERY_REJECTED。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    closers = {"'": "'", '"': '"', "`": "`", "[": "]"}
    while i < n:
        ch = sql[i]
        if ch in closers:
            close = closers[ch]
            j = i + 1
            while True:
                j = sql.find(close, j)
                if j == -1:
                    raise _rejected("閉じていない文字列か名前がある。")
                if close in "'\"`" and sql.startswith(close * 2, j):
                    j += 2  # '' などのエスケープ
                    continue
                break
            out.append("''")
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                raise _rejected("閉じていないコメントがある。")
            i = j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def check_single_select(sql: str) -> None:
    body = skeleton(sql).strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    if not body:
        raise _rejected("SQL が空である。")
    if ";" in body:
        raise _rejected("2つ以上の文がある。")
    first = body.split(None, 1)[0].lower()
    if first not in ("select", "with"):
        raise _rejected(f"SELECT か WITH で始まる文だけを実行する（先頭：{first[:20]}）。")
    complete = sql if sql.rstrip().endswith(";") else sql + "\n;"
    if not sqlite3.complete_statement(complete):
        raise _rejected("SQL の文として閉じていない。")


def _authorizer(
    action: int, arg1: str | None, arg2: str | None, db: str | None, trig: str | None
) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _cell(value: object) -> object:
    if isinstance(value, bytes):
        return f"<BLOB {len(value)} bytes>"
    return value


def execute_readonly(root: Path, run_id: str, sql: str) -> tuple[list[str], list[list], bool]:
    """読み取り専用で1つの SELECT を実行し、(列名, 行, 50行で切ったか) を返す。"""
    check_single_select(sql)
    path = ledger.ledger_path(root, run_id)
    if not path.is_file():
        raise KizashiError(
            "E_REF_NOT_FOUND", STAGE, f"実行の台帳がない：{run_id}", affected_ids=[run_id]
        )
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + limits.QUERY_TIMEOUT_SEC
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_STEPS)
        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(limits.QUERY_ROW_LIMIT + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise KizashiError(
                    "E_QUERY_TIMEOUT",
                    STAGE,
                    f"実行時間が上限（{limits.QUERY_TIMEOUT_SEC}秒）を超えたので中断した。",
                    next_action="条件を絞るか、集計を小さくする。",
                ) from None
            raise _rejected(f"SQL を実行できない：{exc}") from None
        except (sqlite3.DatabaseError, sqlite3.ProgrammingError, sqlite3.Warning) as exc:
            raise _rejected(f"SQL を実行できない：{exc}") from None
        columns = [d[0] for d in cursor.description or ()]
    finally:
        conn.close()
    truncated = len(rows) > limits.QUERY_ROW_LIMIT
    return columns, [[_cell(v) for v in r] for r in rows[: limits.QUERY_ROW_LIMIT]], truncated


def _check_names(root: Path, run_id: str, sql: str) -> None:
    """SQL の全文に氏名の検査を当てる（D-037）。氏名の一覧は実行の原本から作り、保存しない。"""
    conn = ledger.open_ro(root, run_id)
    try:
        row = conn.execute("SELECT domain FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return  # 実行の記録がなければ、execute_readonly が型付きのエラーにする
        adapter = load_adapter(root, row[0])
        names = check.name_variants(adapter.module.name_list(conn, root, adapter))
    finally:
        conn.close()
    hits = check.name_hits({"SQL": sql}, names)
    if hits:
        raise KizashiError(
            "E_V7",
            STAGE,
            "探索SQL に氏名がある（実行せず、保存もしない）：" + "、".join(hits),
            next_action="氏名を書かずに SQL を書き直す（台帳に氏名はない。コメントにも書かない）。",
        )


def run_query(root: Path, run_id: str, sql_arg: str) -> dict:
    """kizashi query <run_id> inbox/<名前>.sql"""
    path = paths.resolve_inbox_file(root, sql_arg, (".sql",))
    name = path.name
    try:
        sql = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise _rejected("SQL ファイルを UTF-8 で読めない。") from None
    accepted = (
        inbox.accepted_dir(root, run_id) if ledger.ledger_path(root, run_id).is_file() else None
    )
    if accepted is not None and (accepted / name).exists():
        raise _rejected(
            f"accepted/ に同じ名前の探索SQL（{name}）がある。",
            next_action="別の名前（inbox/q-<番号>.sql）で書く。",
        )
    if accepted is not None:
        _check_names(root, run_id, sql)
    columns, rows, truncated = execute_readonly(root, run_id, sql)
    assert accepted is not None

    queries = paths.run_dir(root, run_id) / "queries"
    queries.mkdir(exist_ok=True)
    paths.ensure_in_data(root, queries)
    stem = f"{inbox.next_serial(queries):04d}-{path.stem}"
    sql_copy, result_file = queries / f"{stem}.sql", queries / f"{stem}.json"
    at = ledger.now_iso()
    result = {
        "query_file": name,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "citable": False,
        "at": at,
    }
    shutil.copy2(path, sql_copy)
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    conn = ledger.open_rw(root, run_id)
    try:
        with ledger.transaction(conn):
            ledger.add_event(
                conn,
                run_id,
                "query",
                after={
                    "query_file": name,
                    "saved": result_file.relative_to(paths.run_dir(root, run_id)).as_posix(),
                    "row_count": len(rows),
                    "truncated": truncated,
                },
            )
    finally:
        conn.close()
    shutil.copy2(path, accepted / name)
    path.unlink()
    return {
        "run_id": run_id,
        **{k: result[k] for k in ("query_file", "columns", "rows", "row_count", "truncated")},
        "citable": False,
        "saved": result_file.relative_to(paths.run_dir(root, run_id)).as_posix(),
        "message": "探索の結果はメモの数値に使えない（citable: false）。見つけた変化は calc で計算し、"
        "kind: lead で登録する（query_file に accepted/ の名前を書く）。",
    }
