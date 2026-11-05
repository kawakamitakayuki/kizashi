"""kizashi run の新しい実行（implementation_spec.md 4.1。plan・fetch・normalize・quality・reconcile_import・
reconcile_source）。

1. 設定ファイル（6.2 の run_config）を検査する。
2. 実行IDを発行し、実行フォルダーと台帳を作る。runs に設定・版・profile を記録し、running にする。
3. 工程を順に行う。各工程は1つのトランザクションで確定し（fetch は資料ごと）、runs.stages_json に
   状態（done / failed）と入力のハッシュを記録する。
4. すべて終われば completed。取得予定の一部が取れなければ partial（欠けたものを stop_reason に書く）。
   それ以外の失敗は failed。一時的な通信の失敗は failed とし、stop_reason の retryable を true にする。
5. completed・partial なら、設定ファイルを accepted/ に写し、inbox/ から消す。

検出（detect）は P0-5 で足す。いまは実行せず、出力の stages_not_run に書く（実行したことにしない）。
照合の結果（不一致・照合不能）では実行を止めない。T6a・T6b の判定は check が行う（P0-6）。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import string
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from kizashi import fetcher, ledger, paths, quality, reconcile, versions
from kizashi.adapters import FAMILIES, Adapter, load_adapter
from kizashi.adapters.cao_procurement import SCOPE_OPS
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json, sha256_text

STAGES = ("plan", "fetch", "normalize", "quality", "reconcile_import", "reconcile_source")
STAGES_NOT_RUN = ("detect",)
CONFIG_REQUIRED = frozenset(
    {"kind", "domain", "compare_years", "series_years", "question", "model"}
)
CONFIG_OPTIONAL = frozenset({"scope"})
_RUN_ID_CHARS = string.ascii_lowercase + string.digits


# ---------------------------------------------------------------------------
# 1. run_config の検査（6.2・6.3。test_plan.md の CF-1）
# ---------------------------------------------------------------------------


def _schema(message: str) -> KizashiError:
    return KizashiError(
        "E_SCHEMA",
        "run",
        f"run_config の誤り：{message}",
        next_action="investigate の Skill の record-examples.md にある run_config の例に合わせて直す。",
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def validate_run_config(config: object, adapter: Adapter) -> dict:
    """run_config を検査する。誤りは E_SCHEMA。"""
    if not isinstance(config, dict):
        raise _schema("JSON のオブジェクトでない")
    unknown = set(config) - CONFIG_REQUIRED - CONFIG_OPTIONAL
    if unknown:
        raise _schema(f"知らない項目がある：{', '.join(sorted(unknown))}")
    missing = CONFIG_REQUIRED - set(config)
    if missing:
        raise _schema(f"必須の項目がない：{', '.join(sorted(missing))}")
    if config["kind"] != "run_config":
        raise _schema("kind が run_config でない")
    if config["domain"] != adapter.domain:
        raise _schema(f"domain が実行の分野（{adapter.domain}）と違う")

    compare = config["compare_years"]
    if not (isinstance(compare, list) and len(compare) == 2 and all(_is_int(y) for y in compare)):
        raise _schema("compare_years は2つの年度（整数）の配列にする")
    if compare[1] != compare[0] + 1:
        raise _schema("compare_years は連続した2年度にする（例 [2024, 2025]）")
    series = config["series_years"]
    if not (isinstance(series, list) and series and all(_is_int(y) for y in series)):
        raise _schema("series_years は年度（整数）の配列にする")
    if len(set(series)) != len(series):
        raise _schema("series_years に同じ年度が2つある")
    if not set(compare) <= set(series):
        raise _schema("series_years が compare_years の年度を含まない")
    for year in sorted(series):
        for family in FAMILIES:
            if adapter.file_spec(year, family) is None:
                raise _schema(f"{year}年度の {family} が dataset.yaml の files にない")
    if not _nonempty_str(config["question"]):
        raise _schema("question が空")
    if not _nonempty_str(config["model"]):
        raise _schema("model が空")

    if "scope" in config:
        _validate_scope(config["scope"], adapter)
    return config


def _validate_scope(scope: object, adapter: Adapter) -> None:
    fields = adapter.config.get("scope_fields") or {}
    if not isinstance(scope, dict) or set(scope) != {"instruction", "filters"}:
        raise _schema(
            "scope は instruction と filters の2つの項目にする（記者の指示があるときだけ書く）"
        )
    if not _nonempty_str(scope["instruction"]):
        raise _schema("scope の instruction（記者の指示の要約）が空")
    filters = scope["filters"]
    if not (isinstance(filters, list) and filters):
        raise _schema("scope の filters が空")
    for item in filters:
        if not isinstance(item, dict) or set(item) != {"field", "op", "value"}:
            raise _schema("scope の filters の要素は field・op・value の3つの項目にする")
        if item["field"] not in fields:
            raise _schema(
                f"scope の field が scope_fields にない：{item['field']!r}（使えるのは {', '.join(fields)}）"
            )
        if item["op"] not in SCOPE_OPS:
            raise _schema(
                f"scope の op が知らない演算：{item['op']!r}（使えるのは {', '.join(SCOPE_OPS)}）"
            )
        if not _nonempty_str(item["value"]):
            raise _schema("scope の value は空でない文字列にする")


def _load_config(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _schema(f"UTF-8 の JSON として読めない（{exc}）") from exc


# ---------------------------------------------------------------------------
# 2. 実行の登録
# ---------------------------------------------------------------------------


def new_run_id(now: datetime | None = None) -> str:
    """`YYYYMMDD-HHMMSS-` ＋英小文字と数字4桁（3.3）。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{''.join(secrets.choice(_RUN_ID_CHARS) for _ in range(4))}"


@dataclass
class RunContext:
    root: Path
    run_id: str
    adapter: Adapter
    config: dict
    conn: sqlite3.Connection
    client: httpx.Client
    budget: fetcher.Budget = field(default_factory=fetcher.Budget)
    stages: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)


class _StageFailed(Exception):
    def __init__(self, stage: str, error: KizashiError) -> None:
        super().__init__(error.message)
        self.stage = stage
        self.error = error


def _create_ledger(root: Path) -> tuple[str, sqlite3.Connection]:
    for _ in range(20):
        run_id = new_run_id()
        if not paths.run_dir(root, run_id).exists():
            return run_id, ledger.create(root, run_id)
    raise KizashiError(
        "E_INTERNAL", "run", "実行IDを発行できなかった。", next_action="開発側で原因を調べる。"
    )


def _update_run(ctx: RunContext, **columns: object) -> None:
    sets = ", ".join(f"{name} = ?" for name in columns)
    ctx.conn.execute(f"UPDATE runs SET {sets} WHERE run_id = ?", (*columns.values(), ctx.run_id))


def _set_stage(
    ctx: RunContext, stage: str, state: str, input_hash: str, error: KizashiError | None = None
) -> None:
    entry: dict = {"state": state, "input_hash": input_hash, "at": ledger.now_iso()}
    if error is not None:
        entry["error"] = {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        }
    ctx.stages[stage] = entry
    _update_run(ctx, stages_json=canonical_json(ctx.stages))
    ledger.add_event(
        ctx.conn,
        ctx.run_id,
        "stage",
        after={"stage": stage, **entry},
        reason=None if error is None else f"{error.code}: {error.message}",
    )


def _fail_stage(ctx: RunContext, stage: str, input_hash: str, error: KizashiError) -> _StageFailed:
    with ledger.transaction(ctx.conn):
        _set_stage(ctx, stage, "failed", input_hash, error)
    return _StageFailed(stage, error)


# ---------------------------------------------------------------------------
# 3. 工程
# ---------------------------------------------------------------------------


def _plan(ctx: RunContext) -> None:
    adapter = ctx.adapter
    input_hash = sha256_text(
        canonical_json({"config": ctx.config, "dataset_yaml_sha256": adapter.yaml_sha256})
    )
    formats = ["csv"] + (
        ["pdf"] if adapter.config["reconcile"].get("source_check") == "pdf" else []
    )
    page = adapter.config["source"]["page"]
    terms = adapter.config["source"].get("terms_note")
    with ledger.transaction(ctx.conn):
        for year in sorted(ctx.config["series_years"]):
            for family in FAMILIES:
                for fmt in formats:
                    # URL は fetch で公表ページのリンクから決める。予定の段階では公表ページの URL を置く。
                    ctx.conn.execute(
                        "INSERT INTO sources (source_id, logical_key, kind, url, referrer_url, status,"
                        " period, terms_note) VALUES (?, ?, 'dataset', ?, ?, 'planned', ?, ?)",
                        (
                            ledger.next_id(ctx.conn, "S"),
                            adapter.logical_key(year, family, fmt),
                            page,
                            page,
                            str(year),
                            terms,
                        ),
                    )
        _set_stage(ctx, "plan", "done", input_hash)


def _planned_sources(ctx: RunContext) -> list[tuple[str, str]]:
    return ctx.conn.execute(
        "SELECT source_id, logical_key FROM sources WHERE kind = 'dataset' AND status = 'planned'"
        " ORDER BY source_id"
    ).fetchall()


def _save_budget(ctx: RunContext) -> None:
    _update_run(ctx, limits_json=canonical_json(ctx.budget.to_json()))


def _fetch(ctx: RunContext) -> None:
    adapter = ctx.adapter
    planned = _planned_sources(ctx)
    input_hash = sha256_text(canonical_json(planned))
    source_cfg = adapter.config["source"]
    allowed = list(adapter.config["fetch"]["allowed_hosts"])
    page = source_cfg["page"]
    transient: list[str] = []

    with ledger.transaction(ctx.conn):
        index_id = ledger.next_id(ctx.conn, "S")
        ctx.conn.execute(
            "INSERT INTO sources (source_id, kind, url, status, terms_note) VALUES (?, 'index', ?, 'planned', ?)",
            (index_id, page, source_cfg.get("terms_note")),
        )
    try:
        index = fetcher.fetch_to_raw(
            ctx.root, ctx.client, page, fmt="html", allowed_hosts=allowed, budget=ctx.budget
        )
    except KizashiError as exc:
        with ledger.transaction(ctx.conn):
            fetcher.register_failure(
                ctx.conn, index_id, status="failed", reason=fetcher.error_reason(exc)
            )
            for source_id, _ in planned:
                fetcher.register_failure(
                    ctx.conn,
                    source_id,
                    status="failed",
                    reason=f"公表ページ（{index_id}）を取得できない：{fetcher.error_reason(exc)}",
                )
            _save_budget(ctx)
        if exc.retryable:
            transient.append(index_id)
    else:
        with ledger.transaction(ctx.conn):
            fetcher.register_fetched(ctx.conn, index_id, index, referrer_url=None, encoding=None)
            _save_budget(ctx)
        html = index.path.read_bytes().decode("utf-8", errors="replace")
        _fetch_datasets(ctx, planned, index, html, allowed, transient)

    if transient:
        raise _fail_stage(
            ctx,
            "fetch",
            input_hash,
            KizashiError(
                "E_FETCH_FAILED",
                "fetch",
                f"一時的な通信の失敗で取得できなかった資料がある：{', '.join(transient)}",
                affected_ids=[ctx.run_id, *transient],
                retryable=True,
                next_action=f"時間をおいて kizashi run --resume {ctx.run_id} で続ける（P0-5 で実装）。",
            ),
        )
    with ledger.transaction(ctx.conn):
        _set_stage(ctx, "fetch", "done", input_hash)


def _fetch_datasets(
    ctx: RunContext,
    planned: list[tuple[str, str]],
    index: fetcher.Download,
    html: str,
    allowed: list[str],
    transient: list[str],
) -> None:
    adapter = ctx.adapter
    source_cfg = adapter.config["source"]
    offset = int(source_cfg["fiscal_year_from_reiwa"])
    links: dict[str, list[str]] = {}
    for link in fetcher.discover_links(index.final_url, html, source_cfg["link_pattern"]):
        key = adapter.logical_key(int(link["reiwa"]) + offset, link["family"], link["format"])
        links.setdefault(key, []).append(link["url"])

    page_url = index.final_url
    stop: KizashiError | None = None
    for source_id, logical_key in planned:
        _, year, family, fmt = logical_key.split("|")
        urls = links.get(logical_key, [])
        spec = adapter.file_spec(int(year), family)
        assert spec is not None
        failure: tuple[str, str, str | None] | None = None  # (状態, 理由, URL)
        download: fetcher.Download | None = None
        if stop is not None:
            failure = ("failed", fetcher.error_reason(stop), None)
        elif not urls:
            failure = (
                "not_published",
                f"公表ページ（{page_url}）にこの論理資料のリンクがない",
                None,
            )
        elif len(urls) > 1:
            failure = (
                "failed",
                "公表ページに同じ論理資料のリンクが2つ以上ある：" + "、".join(urls),
                None,
            )
        else:
            validate = (
                (lambda data, spec=spec: adapter.module.validate_csv(data, spec))
                if fmt == "csv"
                else None
            )
            try:
                download = fetcher.fetch_to_raw(
                    ctx.root,
                    ctx.client,
                    urls[0],
                    fmt=fmt,
                    allowed_hosts=allowed,
                    budget=ctx.budget,
                    validate=validate,
                )
            except KizashiError as exc:
                failure = ("failed", fetcher.error_reason(exc), urls[0])
                if exc.retryable:
                    transient.append(source_id)
                if exc.code in ("E_LIMIT_HTTP", "E_LIMIT_BYTES"):
                    stop = exc
        # 資料ごとに確定する（4.1）。
        with ledger.transaction(ctx.conn):
            if download is not None:
                fetcher.register_fetched(
                    ctx.conn,
                    source_id,
                    download,
                    referrer_url=page_url,
                    encoding=spec.encoding if fmt == "csv" else None,
                )
            else:
                assert failure is not None
                status, reason, url = failure
                fetcher.register_failure(
                    ctx.conn,
                    source_id,
                    status=status,
                    reason=reason,
                    url=url,
                    referrer_url=page_url,
                )
            _save_budget(ctx)


def _fetched_csv(ctx: RunContext) -> list[tuple[str, str, str]]:
    return ctx.conn.execute(
        "SELECT source_id, logical_key, sha256 FROM sources WHERE kind = 'dataset' AND status = 'fetched'"
        " AND logical_key LIKE '%|csv' ORDER BY source_id"
    ).fetchall()


def _normalize(ctx: RunContext) -> None:
    adapter = ctx.adapter
    sources = _fetched_csv(ctx)
    scope = ctx.config.get("scope")
    input_hash = sha256_text(
        canonical_json(
            {
                "sources": [[s, sha] for s, _, sha in sources],
                "dataset_yaml_sha256": adapter.yaml_sha256,
                "parser_version": adapter.parser_version,
                "scope": scope,
            }
        )
    )
    record_counts: dict[str, dict] = {}
    try:
        with ledger.transaction(ctx.conn):
            for source_id, logical_key, sha in sources:
                _, year, family, _ = logical_key.split("|")
                spec = adapter.file_spec(int(year), family)
                assert spec is not None
                data = (paths.raw_dir(ctx.root) / f"{sha}.csv").read_bytes()
                counts = adapter.module.normalize_source(
                    ctx.conn,
                    adapter,
                    source_id=source_id,
                    logical_key=logical_key,
                    data=data,
                    spec=spec,
                )
                record_counts[source_id] = {"logical_key": logical_key, **counts, "out_of_scope": 0}
            if scope is not None:
                excluded = adapter.module.apply_scope(ctx.conn, adapter, scope)
                for source_id, n in excluded.items():
                    record_counts[source_id]["out_of_scope"] = n
                    record_counts[source_id]["adopted"] -= n
                    record_counts[source_id]["excluded"] += n
            ctx.counts["records"] = record_counts
            if scope is not None:
                ctx.counts["scope"] = {
                    "instruction": scope["instruction"],
                    "filters": scope["filters"],
                    "excluded": {s: c["out_of_scope"] for s, c in record_counts.items()},
                }
            _update_run(ctx, counts_json=canonical_json(ctx.counts))
            _set_stage(ctx, "normalize", "done", input_hash)
    except KizashiError as exc:
        raise _fail_stage(ctx, "normalize", input_hash, exc) from exc


def _quality(ctx: RunContext) -> None:
    rows = ctx.conn.execute(
        "SELECT record_id, cells_sha256, status, status_reason FROM records ORDER BY record_id"
    ).fetchall()
    input_hash = sha256_text(canonical_json(rows))
    outcome = quality.run_quality(ctx.conn, ctx.root, ctx.run_id, ctx.adapter)
    quality.write_report(ctx.root, ctx.run_id, outcome.report_markdown)
    ctx.counts["quality"] = outcome.counts
    with ledger.transaction(ctx.conn):
        _update_run(ctx, counts_json=canonical_json(ctx.counts))
        if not outcome.failures:
            _set_stage(ctx, "quality", "done", input_hash)
    if outcome.failures:
        raise _fail_stage(
            ctx, "quality", input_hash, quality.failure_error(ctx.run_id, outcome.failures)
        )


def _records_input(ctx: RunContext) -> list:
    return ctx.conn.execute(
        "SELECT record_id, cells_sha256, status, status_reason FROM records ORDER BY record_id"
    ).fetchall()


def _reconcile_import(ctx: RunContext) -> None:
    """取り込みの照合（T6a）。資料ごとに照合の計算を1つ登録する。"""
    input_hash = sha256_text(
        canonical_json({"records": _records_input(ctx), "sources": _fetched_csv(ctx)})
    )
    try:
        with ledger.transaction(ctx.conn):
            ctx.counts["reconcile_import"] = reconcile.reconcile_import(
                ctx.conn, ctx.root, ctx.adapter
            )
            _update_run(ctx, counts_json=canonical_json(ctx.counts))
            _set_stage(ctx, "reconcile_import", "done", input_hash)
    except KizashiError as exc:
        raise _fail_stage(ctx, "reconcile_import", input_hash, exc) from exc


def _reconcile_source(ctx: RunContext) -> None:
    """原本照合（T6b）。CSV と PDF の組ごとに照合の計算を1つ登録する。入力のハッシュは CSV と PDF の SHA-256。"""
    pairs = ctx.conn.execute(
        "SELECT source_id, logical_key, sha256 FROM sources WHERE kind = 'dataset'"
        " AND status = 'fetched' ORDER BY source_id"
    ).fetchall()
    input_hash = sha256_text(canonical_json(pairs))
    try:
        with ledger.transaction(ctx.conn):
            ctx.counts["reconcile_source"] = reconcile.reconcile_source(
                ctx.conn, ctx.root, ctx.adapter
            )
            _update_run(ctx, counts_json=canonical_json(ctx.counts))
            _set_stage(ctx, "reconcile_source", "done", input_hash)
    except KizashiError as exc:
        raise _fail_stage(ctx, "reconcile_source", input_hash, exc) from exc


# ---------------------------------------------------------------------------
# 4〜5. 状態の確定と設定ファイルの移動
# ---------------------------------------------------------------------------


def _sources_summary(ctx: RunContext) -> list[dict]:
    rows = ctx.conn.execute(
        "SELECT source_id, kind, logical_key, status, failure_reason FROM sources ORDER BY source_id"
    ).fetchall()
    return [
        {"source_id": s, "kind": k, "logical_key": lk, "status": st, "failure_reason": fr}
        for s, k, lk, st, fr in rows
    ]


def _finish(ctx: RunContext, state: str, stop_reason: dict | None) -> None:
    with ledger.transaction(ctx.conn):
        _update_run(
            ctx,
            state=state,
            stop_reason=None if stop_reason is None else canonical_json(stop_reason),
            finished_at=ledger.now_iso(),
        )
        ledger.add_event(
            ctx.conn,
            ctx.run_id,
            "state_change",
            before={"state": "running"},
            after={"state": state},
        )


def _accept_config(ctx: RunContext, config_path: Path) -> str:
    accepted = paths.run_dir(ctx.root, ctx.run_id) / "accepted"
    accepted.mkdir(exist_ok=True)
    paths.ensure_in_data(ctx.root, accepted)
    number = len(list(accepted.glob("*.json"))) + 1
    target = accepted / f"{number:04d}-run_config.json"
    shutil.copy2(config_path, target)
    config_path.unlink()
    return target.relative_to(ctx.root).as_posix()


def start_run(
    root: Path, domain: str, config_arg: str, *, transport: httpx.BaseTransport | None = None
) -> dict:
    """新しい実行（plan〜reconcile_source）。completed・partial なら結果を返し、failed なら KizashiError を投げる。"""
    adapter = load_adapter(root, domain)
    config_path = paths.resolve_inbox_file(root, config_arg, (".json",))
    config = validate_run_config(_load_config(config_path), adapter)

    paths.runs_dir(root).mkdir(parents=True, exist_ok=True)
    paths.ensure_in_data(root, paths.runs_dir(root))
    run_id, conn = _create_ledger(root)
    client = fetcher.make_client(transport)
    ctx = RunContext(
        root=root, run_id=run_id, adapter=adapter, config=config, conn=conn, client=client
    )
    try:
        with ledger.transaction(conn):
            conn.execute(
                "INSERT INTO runs (run_id, domain, config_json, profile, state, rule_versions_json,"
                " knowledge_hashes_json, limits_json, started_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (
                    run_id,
                    domain,
                    canonical_json(config),
                    os.environ.get("KIZASHI_PROFILE"),
                    canonical_json(versions.rule_versions(root, domain)),
                    canonical_json(versions.knowledge_hashes(root)),
                    canonical_json(ctx.budget.to_json()),
                    ledger.now_iso(),
                ),
            )
            ledger.add_event(
                conn, run_id, "state_change", before={"state": None}, after={"state": "running"}
            )
        try:
            for stage in (
                _plan,
                _fetch,
                _normalize,
                _quality,
                _reconcile_import,
                _reconcile_source,
            ):
                stage(ctx)
        except _StageFailed as failed:
            err = failed.error
            _finish(
                ctx,
                "failed",
                {
                    "stage": failed.stage,
                    "code": err.code,
                    "retryable": err.retryable,
                    "message": err.message,
                },
            )
            raise KizashiError(
                err.code,
                "run",
                f"実行 {run_id} は {failed.stage} で止まった（failed）：{err.message}",
                affected_ids=[run_id, *[a for a in err.affected_ids if a != run_id]],
                retryable=err.retryable,
                next_action=err.next_action,
            ) from None
        except Exception as exc:
            _finish(
                ctx,
                "failed",
                {
                    "stage": "unknown",
                    "code": "E_INTERNAL",
                    "retryable": False,
                    "message": repr(exc),
                },
            )
            raise

        missing = [
            s for s in _sources_summary(ctx) if s["kind"] == "dataset" and s["status"] != "fetched"
        ]
        state = "partial" if missing else "completed"
        stop_reason = (
            {
                "missing": [
                    {k: s[k] for k in ("source_id", "logical_key", "status", "failure_reason")}
                    for s in missing
                ]
            }
            if missing
            else None
        )
        _finish(ctx, state, stop_reason)
        accepted = _accept_config(ctx, config_path)
        source_check = ctx.counts.get("reconcile_source", {})
        not_checked = [
            f"{sid}（{entry.get('error') or entry.get('reason')}）"
            for sid, entry in source_check.items()
            if "error" in entry or entry.get("state") == "not_run"
        ]
        message = (
            "取得・正規化・品質検査・照合（reconcile_import・reconcile_source）を終えた。"
            "検出（detect）は未実装のため実行していない（P0-5）。"
        )
        if not_checked:
            message += "原本照合ができなかった資料がある（照合不能として記録した）：" + "、".join(
                not_checked
            )
        return {
            "run_id": run_id,
            "state": state,
            "stop_reason": stop_reason,
            "stages": ctx.stages,
            "stages_not_run": list(STAGES_NOT_RUN),
            "sources": _sources_summary(ctx),
            "records": ctx.counts.get("records", {}),
            "quality_report": f"data/runs/{run_id}/{quality.REPORT_NAME}",
            "reconcile_import": ctx.counts.get("reconcile_import", {}),
            "reconcile_source": source_check,
            "config_accepted": accepted,
            "message": message,
        }
    finally:
        client.close()
        conn.close()
