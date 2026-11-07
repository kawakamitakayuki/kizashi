"""kizashi run の新しい実行と再開（implementation_spec.md 4.1。plan・fetch・normalize・quality・
reconcile_import・reconcile_source・detect）。

新しい実行（start_run）：
1. 設定ファイル（6.2 の run_config）を検査する。
2. 実行IDを発行し、実行フォルダーと台帳を作る。runs に設定・版・profile を記録し、running にする。
3. 工程を順に行う。各工程は1つのトランザクションで確定し（fetch は資料ごと）、runs.stages_json に
   状態（done / failed）と入力のハッシュを記録する。
4. すべて終われば completed。取得予定の一部が取れない、または一部の検出SQLが動かなければ partial（欠けた
   ものを stop_reason に書く）。それ以外の失敗は failed。一時的な通信の失敗は failed とし、stop_reason の
   retryable を true にする。
5. completed・partial なら、設定ファイルを accepted/ に写し、inbox/ から消す。

再開（resume_run。kizashi run --resume <run_id>）：
- 対象は、状態が running（強制終了で残ったもの）か、stop_reason が一時的な通信の失敗の failed だけ。
  partial・completed と、ほかの failed は E_NOT_RESUMABLE。
- 終わった工程は、入力のハッシュをいまの台帳と dataset.yaml から計算し直し、同じなら飛ばす。違えば
  E_INPUT_CHANGED で止め、新しい実行を求める。規則の版（実装仕様3.5 の照合するもの）が開始時と違っても、
  取得済みの原本（data/raw/ の CSV・PDF・公表ページの HTML）がないか中身の SHA-256 が記録と違っても
  E_INPUT_CHANGED（途中から違う規則・違う原本で続けないため）。拒んだときは台帳を変えない。
- 取得は、取得済みの資料を取り直さない。取得済みでない予定の資料（planned・failed）を取り直す。公表ページを
  取得済みなら、保存した原本からリンクを読み直す（通信しない）。
- detect が終わっていれば候補を作り直さない。途中で止まった detect はトランザクションが確定していないので、
  最初からやり直す。
- events に resume を記録する。

照合の結果（不一致・照合不能）では実行を止めない。T6a・T6b の判定は check が行う（P0-6）。
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from kizashi import check, detect, fetcher, inbox, ledger, paths, quality, reconcile, versions
from kizashi.adapters import FAMILIES, Adapter, load_adapter
from kizashi.adapters.cao_procurement import SCOPE_OPS
from kizashi.errors import KizashiError
from kizashi.hashing import canonical_json, sha256_file, sha256_text

STAGES = (
    "plan",
    "fetch",
    "normalize",
    "quality",
    "reconcile_import",
    "reconcile_source",
    "detect",
)
# 規則の版のうち、照合するもの（実装仕様3.5）。再開でも開始時の記録と照らす。
COMPARED_VERSIONS = versions.COMPARED_VERSIONS
CONFIG_REQUIRED = frozenset(
    {"kind", "domain", "compare_years", "series_years", "question", "model"}
)
CONFIG_OPTIONAL = frozenset({"scope"})
# 氏名の検査（normalize の最初）の前に台帳に書く設定の項目（検査済みの分野と年度だけ。D-040①）。question・
# scope・model などの文字列は、検査を通ってから全文を書く。
CONFIG_BEFORE_NAME_CHECK = ("kind", "domain", "compare_years", "series_years")
PENDING = "pending"
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
        if not all(isinstance(item[k], str) for k in ("field", "op")):
            raise _schema("scope の filters の field と op は文字列で書く")  # D-095③
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
    client: httpx.Client | None = None  # 通信する工程（fetch）の前に作る
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
# 3. 工程と入力のハッシュ
#
# 入力のハッシュは、工程の始めに台帳・dataset.yaml・検出SQLから計算する。再開では、終わった工程の
# ハッシュを同じ関数で計算し直して、記録と照らす（4.1）。
# ---------------------------------------------------------------------------


def _hash(obj: object) -> str:
    return sha256_text(canonical_json(obj))


def _dataset_sources(ctx: RunContext) -> list[tuple[str, str]]:
    return ctx.conn.execute(
        "SELECT source_id, logical_key FROM sources WHERE kind = 'dataset' ORDER BY source_id"
    ).fetchall()


def _fetched_csv(ctx: RunContext) -> list[tuple[str, str, str]]:
    return ctx.conn.execute(
        "SELECT source_id, logical_key, sha256 FROM sources WHERE kind = 'dataset' AND status = 'fetched'"
        " AND logical_key LIKE '%|csv' ORDER BY source_id"
    ).fetchall()


def _fetched_datasets(ctx: RunContext) -> list[tuple[str, str, str]]:
    return ctx.conn.execute(
        "SELECT source_id, logical_key, sha256 FROM sources WHERE kind = 'dataset'"
        " AND status = 'fetched' ORDER BY source_id"
    ).fetchall()


def _records_input(ctx: RunContext) -> list:
    return ctx.conn.execute(
        "SELECT record_id, cells_sha256, status, status_reason FROM records ORDER BY record_id"
    ).fetchall()


def _plan_hash(ctx: RunContext) -> str:
    """設定と dataset.yaml。"""
    return _hash({"config": ctx.config, "dataset_yaml_sha256": ctx.adapter.yaml_sha256})


def _fetch_hash(ctx: RunContext) -> str:
    """資料の一覧（予定の論理資料）。"""
    return _hash(_dataset_sources(ctx))


def _normalize_hash(ctx: RunContext) -> str:
    """資料の SHA-256、dataset.yaml、parser_version（と母集団の絞り込み）。"""
    return _hash(
        {
            "sources": [[s, sha] for s, _, sha in _fetched_csv(ctx)],
            "dataset_yaml_sha256": ctx.adapter.yaml_sha256,
            "parser_version": ctx.adapter.parser_version,
            "scope": ctx.config.get("scope"),
        }
    )


def _quality_hash(ctx: RunContext) -> str:
    """前の工程の出力（レコード）。"""
    return _hash(_records_input(ctx))


def _reconcile_import_hash(ctx: RunContext) -> str:
    return _hash({"records": _records_input(ctx), "sources": _fetched_csv(ctx)})


def _reconcile_source_hash(ctx: RunContext) -> str:
    """CSV と PDF の SHA-256。"""
    return _hash(_fetched_datasets(ctx))


def _detect_hash(ctx: RunContext) -> str:
    """前の工程の出力（レコードと分野の表、品質検査を通った資料）と、検出SQL・dataset.yaml のハッシュ、
    比べる年度と系列の年度。"""
    table = ctx.adapter.module.RECORD_TABLE  # 分野アダプタの定数
    contracts = ctx.conn.execute(f"SELECT * FROM {table} ORDER BY record_id").fetchall()
    return _hash(
        {
            "records": _records_input(ctx),
            "contracts_sha256": _hash([list(r) for r in contracts]),
            "sources": _fetched_datasets(ctx),
            "quality_sources": sorted(ctx.counts.get("quality", {})),
            "sql_sha256": versions.sql_sha256(ctx.root, ctx.adapter.domain),
            "dataset_yaml_sha256": ctx.adapter.yaml_sha256,
            "compare_years": ctx.config["compare_years"],
            "series_years": ctx.config["series_years"],
        }
    )


def _plan(ctx: RunContext) -> None:
    adapter = ctx.adapter
    input_hash = _plan_hash(ctx)
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


def _save_budget(ctx: RunContext) -> None:
    _update_run(ctx, limits_json=canonical_json(ctx.budget.to_json()))


def _fetch_targets(ctx: RunContext) -> list[tuple[str, str, str, str | None]]:
    """取得する資料：予定のままのもの（planned）と、前に取れなかったもの（failed。再開のとき）。"""
    return ctx.conn.execute(
        "SELECT source_id, logical_key, status, failure_reason FROM sources WHERE kind = 'dataset'"
        " AND status IN ('planned', 'failed') ORDER BY source_id"
    ).fetchall()


def _fetch(ctx: RunContext) -> None:
    """公表ページのリンクから予定の資料を取得する。取得済みの資料は取り直さない（再開のとき）。"""
    adapter = ctx.adapter
    assert ctx.client is not None
    input_hash = _fetch_hash(ctx)
    targets = [(s, k) for s, k, _, _ in _fetch_targets(ctx)]
    source_cfg = adapter.config["source"]
    allowed = list(adapter.config["fetch"]["allowed_hosts"])
    page = source_cfg["page"]
    transient: list[str] = []

    index_row = ctx.conn.execute(
        "SELECT source_id, status, sha256, final_url FROM sources WHERE kind = 'index' AND url = ?"
        " ORDER BY source_id LIMIT 1",
        (page,),
    ).fetchone()
    html: str | None = None
    page_url = page
    if index_row is not None and index_row[1] == "fetched":
        # 再開：取得済みの公表ページは取り直さず、保存した原本からリンクを読み直す。
        index_path = paths.raw_dir(ctx.root) / f"{index_row[2]}.html"
        html = index_path.read_bytes().decode("utf-8", errors="replace")
        page_url = index_row[3] or page
    else:
        if index_row is None:
            with ledger.transaction(ctx.conn):
                index_id = ledger.next_id(ctx.conn, "S")
                ctx.conn.execute(
                    "INSERT INTO sources (source_id, kind, url, status, terms_note)"
                    " VALUES (?, 'index', ?, 'planned', ?)",
                    (index_id, page, source_cfg.get("terms_note")),
                )
        else:
            index_id = index_row[0]
        try:
            index = fetcher.fetch_to_raw(
                ctx.root, ctx.client, page, fmt="html", allowed_hosts=allowed, budget=ctx.budget
            )
        except KizashiError as exc:
            with ledger.transaction(ctx.conn):
                fetcher.register_failure(
                    ctx.conn, index_id, status="failed", reason=fetcher.error_reason(exc)
                )
                for source_id, _ in targets:
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
                fetcher.register_fetched(
                    ctx.conn, index_id, index, referrer_url=None, encoding=None
                )
                _save_budget(ctx)
            html = index.path.read_bytes().decode("utf-8", errors="replace")
            page_url = index.final_url
    if html is not None:
        _fetch_datasets(ctx, targets, page_url, html, allowed, transient)

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
                next_action=f"時間をおいて kizashi run --resume {ctx.run_id} で続ける。",
            ),
        )
    with ledger.transaction(ctx.conn):
        _set_stage(ctx, "fetch", "done", input_hash)


def _fetch_datasets(
    ctx: RunContext,
    planned: list[tuple[str, str]],
    page_url: str,
    html: str,
    allowed: list[str],
    transient: list[str],
) -> None:
    adapter = ctx.adapter
    links = fetcher.dataset_links(adapter, page_url, html)

    assert ctx.client is not None
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
            # 候補の URL は書かない。初回の run では氏名の一覧がまだなく、URL を氏名で照らせないため（D-074）。
            # 原因は保存した公表ページの原本で確かめる
            failure = (
                "failed",
                (
                    f"公表ページに同じ論理資料（{source_id}・{logical_key}）のリンクが候補{len(urls)}個ある"
                    "（候補の URL は書かない。保存した公表ページの原本で確かめる）"
                ),
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


def config_sha256(config: object) -> str:
    """設定の正規化JSON（キーの順と空白を問わず、数値の型を区別する）の SHA-256。"""
    return sha256_text(canonical_json(config))


def pending_config(config: dict) -> dict:
    """氏名の検査の前に台帳に書く設定：検査済みの分野・年度と、全文のハッシュだけ（D-040①）。"""
    return {
        **{k: config[k] for k in CONFIG_BEFORE_NAME_CHECK},
        "name_check": PENDING,
        "config_sha256": config_sha256(config),
    }


def is_pending(stored: dict) -> bool:
    """台帳の設定が、氏名の検査の前のもの（全文を保存していない）か。"""
    return stored.get("name_check") == PENDING


def _save_checked_config(ctx: RunContext) -> None:
    """氏名の検査を通った設定の全文を台帳に書き、検査済みを events に残す（同じトランザクション。D-040①）。"""
    row = ctx.conn.execute(
        "SELECT config_json FROM runs WHERE run_id = ?", (ctx.run_id,)
    ).fetchone()
    if not is_pending(json.loads(row[0])):
        return  # すでに保存した（検査の後に止まった実行の再開）
    with ledger.transaction(ctx.conn):
        _update_run(ctx, config_json=canonical_json(ctx.config))
        ledger.add_event(
            ctx.conn,
            ctx.run_id,
            "check",
            after={
                "target": "run_config",
                "check": "V7_PERSONAL_NAME",
                "result": "passed",
                "config_sha256": config_sha256(ctx.config),
            },
        )


def _check_config_names(ctx: RunContext) -> None:
    """実行設定の文字列（question、scope の指示と条件の値など）に氏名の検査を当てる（D-037）。

    氏名の一覧は取得した原本から作るので、取得の後・正規化の前に行う。当たれば E_V7 で正規化を止める。
    取り込まない列の値（担当官の所在地・相手方の住所。D-032）も照らす（D-090①）。取得済みの CSV が1本もなく
    一覧を作れなければ、検査済みにせず E_V7（D-090②）。
    """
    terms = check.sensitive_terms(ctx.conn, ctx.root, ctx.adapter, "normalize")
    hits = terms.hits(dict(check.string_leaves(ctx.config)))
    if hits:
        raise KizashiError(
            "E_V7",
            "normalize",
            "実行設定に氏名がある：" + "、".join(hits),
            affected_ids=[ctx.run_id],
            next_action="記者の指示から氏名を除いた設定を inbox/ に書き直し、新しい実行を始める。",
        )


def _normalize(ctx: RunContext) -> None:
    adapter = ctx.adapter
    sources = _fetched_csv(ctx)
    scope = ctx.config.get("scope")
    input_hash = _normalize_hash(ctx)
    record_counts: dict[str, dict] = {}
    try:
        _check_config_names(ctx)
        _save_checked_config(ctx)
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
    input_hash = _quality_hash(ctx)
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


def _reconcile_import(ctx: RunContext) -> None:
    """取り込みの照合（T6a）。資料ごとに照合の計算を1つ登録する。"""
    input_hash = _reconcile_import_hash(ctx)
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
    input_hash = _reconcile_source_hash(ctx)
    try:
        with ledger.transaction(ctx.conn):
            ctx.counts["reconcile_source"] = reconcile.reconcile_source(
                ctx.conn, ctx.root, ctx.adapter
            )
            _update_run(ctx, counts_json=canonical_json(ctx.counts))
            _set_stage(ctx, "reconcile_source", "done", input_hash)
    except KizashiError as exc:
        raise _fail_stage(ctx, "reconcile_source", input_hash, exc) from exc


def _detect(ctx: RunContext) -> None:
    """検出（4.8）。候補・自動の基礎集計・同じ区分の件数・年度の系列を1つのトランザクションで登録する。
    検出SQLごとの結果（done / not_run）を runs.counts_json に検出SQLの名前で入れる。"""
    input_hash = _detect_hash(ctx)
    try:
        with ledger.transaction(ctx.conn):
            outcome = detect.run_screens(
                ctx.conn,
                ctx.root,
                ctx.adapter,
                ctx.config,
                checked_sources=set(ctx.counts.get("quality", {})),
            )
            counts = {**ctx.counts, **outcome.counts}
            _update_run(ctx, counts_json=canonical_json(counts))
            _set_stage(ctx, "detect", "done", input_hash)
        ctx.counts = counts
    except KizashiError as exc:
        raise _fail_stage(ctx, "detect", input_hash, exc) from exc
    except sqlite3.Error as exc:
        # 分野の SQL の構文・実行時のエラーも、分野の SQL の誤りとして detect を failed にする（トランザクションは
        # 巻き戻り、候補を残さない。4.8 の末尾。D-038⑨。1-8）
        error = KizashiError(
            "E_INTERNAL",
            "detect",
            f"分野の SQL（検出・母集団・系列）を実行できない（{type(exc).__name__}：{exc}）。",
            affected_ids=[ctx.run_id],
            next_action="開発側で datasets/<分野>/ の SQL を直す。調査のセッションでは直さず、記者に伝える。",
        )
        raise _fail_stage(ctx, "detect", input_hash, error) from exc


STAGE_FUNCS: dict[str, Callable[[RunContext], None]] = {
    "plan": _plan,
    "fetch": _fetch,
    "normalize": _normalize,
    "quality": _quality,
    "reconcile_import": _reconcile_import,
    "reconcile_source": _reconcile_source,
    "detect": _detect,
}
STAGE_HASHES: dict[str, Callable[[RunContext], str]] = {
    "plan": _plan_hash,
    "fetch": _fetch_hash,
    "normalize": _normalize_hash,
    "quality": _quality_hash,
    "reconcile_import": _reconcile_import_hash,
    "reconcile_source": _reconcile_source_hash,
    "detect": _detect_hash,
}


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


def _detect_counts(ctx: RunContext) -> dict[str, dict]:
    """runs.counts_json のうち、検出SQLごとの結果（dataset.yaml の screens の名前）。"""
    names = ctx.adapter.config.get("screens") or {}
    return {name: ctx.counts[name] for name in sorted(names) if name in ctx.counts}


def _run_stages(ctx: RunContext, stages: list[str]) -> None:
    """工程を順に行う。工程の失敗は failed で確定し、KizashiError にして投げる。"""
    try:
        for stage in stages:
            STAGE_FUNCS[stage](ctx)
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
            f"実行 {ctx.run_id} は {failed.stage} で止まった（failed）：{err.message}",
            affected_ids=[ctx.run_id, *[a for a in err.affected_ids if a != ctx.run_id]],
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


def _complete(ctx: RunContext, accept: Callable[[], str]) -> dict:
    """状態（completed / partial）を確定し、設定ファイルを accepted/ に写して、結果を返す。"""
    missing = [
        s for s in _sources_summary(ctx) if s["kind"] == "dataset" and s["status"] != "fetched"
    ]
    screens = _detect_counts(ctx)
    not_run = [
        {"screen": name, "missing": entry.get("missing", [])}
        for name, entry in screens.items()
        if entry.get("state") == "not_run"
    ]
    reasons: dict = {}
    if missing:
        reasons["missing"] = [
            {k: s[k] for k in ("source_id", "logical_key", "status", "failure_reason")}
            for s in missing
        ]
    if not_run:
        reasons["screens_not_run"] = not_run
    state = "partial" if reasons else "completed"
    stop_reason = reasons or None
    _finish(ctx, state, stop_reason)
    accepted = accept()
    source_check = ctx.counts.get("reconcile_source", {})
    not_checked = [
        f"{sid}（{entry.get('error') or entry.get('reason')}）"
        for sid, entry in source_check.items()
        if "error" in entry or entry.get("state") == "not_run"
    ]
    leads = ctx.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    message = (
        "取得・正規化・品質検査・照合（reconcile_import・reconcile_source）・検出（detect）を終えた。"
        f"候補は kizashi inspect {ctx.run_id} で読む。"
    )
    if not_run:
        message += (
            "資料がそろわず実行しなかった検出SQLがある（候補なしではない）："
            + "、".join(f"{n['screen']}（{'・'.join(n['missing'])}）" for n in not_run)
            + "。"
        )
    if not_checked:
        message += "原本照合ができなかった資料がある（照合不能として記録した）：" + "、".join(
            not_checked
        )
    return {
        "run_id": ctx.run_id,
        "state": state,
        "stop_reason": stop_reason,
        "stages": ctx.stages,
        "sources": _sources_summary(ctx),
        "records": ctx.counts.get("records", {}),
        "quality_report": f"data/runs/{ctx.run_id}/{quality.REPORT_NAME}",
        "reconcile_import": ctx.counts.get("reconcile_import", {}),
        "reconcile_source": source_check,
        "detect": screens,
        "leads": leads,
        "config_accepted": accepted,
        "message": message,
    }


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def start_run(
    root: Path, domain: str, config_arg: str, *, transport: httpx.BaseTransport | None = None
) -> dict:
    """新しい実行（plan〜detect）。completed・partial なら結果を返し、failed なら KizashiError を投げる。"""
    adapter = load_adapter(root, domain)
    config_path = paths.resolve_inbox_file(root, config_arg, (".json",))
    config = validate_run_config(_load_config(config_path), adapter)

    paths.make_dir_in_data(root, paths.runs_dir(root))  # 作る前に data/ の中かを確かめる（D-092⑥）
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
                    canonical_json(pending_config(config)),  # 全文は氏名の検査の後（D-040①）
                    os.environ.get("KIZASHI_PROFILE"),
                    canonical_json(versions.rule_versions(root, domain)),
                    canonical_json(versions.knowledge_hashes(root)),
                    canonical_json(ctx.budget.to_json()),
                    ledger.now_iso(),
                ),
            )
            # 設定ファイルの場所を残す（再開して成功したときに accepted/ に写すため）。
            ledger.add_event(
                conn,
                run_id,
                "state_change",
                before={"state": None},
                after={"state": "running", "config_file": _relative(root, config_path)},
            )
        _run_stages(ctx, list(STAGES))
        return _complete(ctx, lambda: _accept_checked_config(ctx))
    finally:
        client.close()
        conn.close()


# ---------------------------------------------------------------------------
# 再開（4.1 の「再開」。kizashi run --resume <run_id>）
# ---------------------------------------------------------------------------


# 索引ページ（kind='index'）の原本の拡張子。run の公表ページは HTML、fetch --index は PDF のこともある（1-9）
INDEX_EXTENSIONS = ("html", "pdf")


def _not_resumable(run_id: str, why: str) -> KizashiError:
    return KizashiError(
        "E_NOT_RESUMABLE",
        "run",
        f"実行 {run_id} は再開できない：{why}",
        affected_ids=[run_id],
        next_action="再開できるのは running か、一時的な通信の失敗で止まった failed だけ。"
        "同じ設定で新しい実行を作る（kizashi run <分野> --config inbox/<名前>.json）。",
    )


def _input_changed(run_id: str, why: str) -> KizashiError:
    return KizashiError(
        "E_INPUT_CHANGED",
        "run",
        f"実行 {run_id} の入力が変わったので再開しない：{why}",
        affected_ids=[run_id],
        next_action="同じ設定で新しい実行を作る（kizashi run <分野> --config inbox/<名前>.json）。",
    )


def _raw_problems(ctx: RunContext) -> list[str]:
    """再開で使い続ける原本（取得済みのデータの CSV・PDF と公表ページの HTML）のうち、data/raw/ にないか、
    中身の SHA-256 が sources の記録と違うもの（「資料ID（論理資料のキーか URL）：…」の形）。

    終わった工程の入力のハッシュは台帳の SHA-256 から計算するので、原本そのものはここで照らす（D-038⑫）。"""
    problems = []
    for source_id, kind, key, url, sha in ctx.conn.execute(
        "SELECT source_id, kind, logical_key, url, sha256 FROM sources"
        " WHERE kind IN ('dataset', 'index') AND status = 'fetched' ORDER BY source_id"
    ):
        # 索引ページは、run の公表ページ（HTML）のほか、fetch --index で取った PDF もある。保存したときの拡張子
        # （data/raw/<sha256>.<拡張子>）で探す（1-9）
        exts = INDEX_EXTENSIONS if kind == "index" else (str(key).rsplit("|", 1)[-1],)
        candidates = [paths.raw_dir(ctx.root) / f"{sha}.{ext}" for ext in exts]
        path = next((c for c in candidates if c.is_file() or c.is_symlink()), candidates[0])
        label = f"{source_id}（{key or url}）"
        if not sha or path.is_symlink() or not path.is_file():
            problems.append(f"{label}：保存した原本がない")
        elif sha256_file(path) != sha:
            problems.append(f"{label}：保存した原本の SHA-256 が取得したときと違う")
    return problems


def _same_config(path: Path, config: dict) -> bool:
    """inbox/ のファイルが、台帳の設定と同じ中身か。数値の型（2024 と 2024.0、true と 1）も区別する正規化
    JSON（キーの順と空白だけを問わない）で照らす（D-038⑬）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return canonical_json(data) == canonical_json(config)
    except (OSError, UnicodeDecodeError, ValueError):
        return False


def _config_file(conn: sqlite3.Connection, run_id: str) -> str | None:
    """開始時の設定ファイルの場所（最初の state_change の events の config_file）。"""
    row = conn.execute(
        "SELECT after_json FROM events WHERE target_id = ? AND kind = 'state_change'"
        " ORDER BY event_id LIMIT 1",
        (run_id,),
    ).fetchone()
    name = json.loads(row[0]).get("config_file") if row and row[0] else None
    return name if isinstance(name, str) else None


def _resume_config(root: Path, conn: sqlite3.Connection, run_id: str, stored: dict) -> dict:
    """再開で使う設定。全文を保存してあれば台帳の設定（D-038⑬）。氏名の検査の前に止まった実行（全文を保存して
    いない）は、inbox/ の開始時の設定ファイルを読み、正規化JSONのハッシュが開始時と同じことを確かめる。ファイルが
    なければ E_NOT_RESUMABLE、中身か数値の型が違えば E_INPUT_CHANGED（D-040①）。"""
    if not is_pending(stored):
        return stored
    name = _config_file(conn, run_id)
    try:
        if name is None:
            raise KizashiError("E_INBOX_PATH", "run", "設定ファイルの場所の記録がない")
        path = paths.resolve_inbox_file(root, name, (".json",))
    except KizashiError:
        raise _not_resumable(
            run_id,
            f"氏名の検査の前に止まった実行で、開始時の設定ファイル（{name}）が inbox/ にない"
            "（台帳には設定の全文を保存していない）",
        ) from None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        data = None
    if data is None or config_sha256(data) != stored.get("config_sha256"):
        raise _input_changed(
            run_id,
            f"設定ファイル（{name}）の中身が開始時と違う（キーの順と空白のほかに、値か数値の型が違う）",
        )
    return data


def _accept_checked_config(ctx: RunContext) -> str:
    """成功したとき（completed・partial）の設定ファイルの扱い。新しい実行も再開も同じ（D-038⑬・D-091④）。

    開始時の inbox/ のファイル（最初の state_change の config_file）を1回だけ読み、キーの順と空白を問わず数値の
    型を区別する正規化JSONで、検査済みの設定（ctx.config）と照らす。同じなら、読んだ中身を accepted/ に書いて
    inbox/ から消す（読んだ後に変わっていれば消さない）。違う・ない・JSON として読めないなら、検査済みの設定を
    accepted/ に書き、inbox/ のファイルは触らない。照らした後に inbox/ から読み直した中身を accepted/ に写さない
    （氏名などの検査を通る前の中身を保存しないため。1-16）。"""
    name = _config_file(ctx.conn, ctx.run_id)
    path: Path | None = None
    if name is not None:
        try:
            path = paths.resolve_inbox_file(ctx.root, name, (".json",))
        except KizashiError:
            path = None
    if path is not None:
        try:
            content = path.read_bytes()
            same = canonical_json(json.loads(content.decode("utf-8"))) == canonical_json(ctx.config)
        except (OSError, UnicodeDecodeError, ValueError):
            same = False
        if same:
            target = inbox.write_accepted(ctx.root, ctx.run_id, "run_config", content)
            try:
                if path.read_bytes() == content:
                    path.unlink()
            except OSError:
                pass  # 読んだ後に消えた・読めなくなったファイルは触らない
            return _relative(ctx.root, target)
    target = inbox.write_accepted(
        ctx.root, ctx.run_id, "run_config", json.dumps(ctx.config, ensure_ascii=False, indent=2)
    )
    return _relative(ctx.root, target)


def resume_run(root: Path, run_id: str, *, transport: httpx.BaseTransport | None = None) -> dict:
    """止まった実行を再開する。終わった工程は入力のハッシュを確かめて飛ばし、残りの工程を行う。"""
    conn = ledger.open_rw(root, run_id)
    client: httpx.Client | None = None
    try:
        conn.row_factory = sqlite3.Row
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        conn.row_factory = None
        if run is None:
            raise _not_resumable(run_id, "台帳に実行の記録がない")
        prev_state = run["state"]
        stop = json.loads(run["stop_reason"]) if run["stop_reason"] else None
        if prev_state in ("completed", "partial"):
            raise _not_resumable(run_id, f"状態が {prev_state}")
        if prev_state == "failed" and not (
            isinstance(stop, dict) and stop.get("retryable") is True
        ):
            code = stop.get("code") if isinstance(stop, dict) else None
            raise _not_resumable(run_id, f"一時的な通信の失敗でない failed（{code}）")

        adapter = load_adapter(root, run["domain"])
        ctx = RunContext(
            root=root,
            run_id=run_id,
            adapter=adapter,
            config=_resume_config(root, conn, run_id, json.loads(run["config_json"])),
            conn=conn,
            budget=fetcher.Budget.from_json(json.loads(run["limits_json"])),
            stages=json.loads(run["stages_json"]),
            counts=json.loads(run["counts_json"]),
        )

        done = [s for s in STAGES if ctx.stages.get(s, {}).get("state") == "done"]
        if done != list(STAGES[: len(done)]):
            raise _not_resumable(run_id, f"終わった工程が順に並んでいない（{'・'.join(done)}）")
        changed = [s for s in done if STAGE_HASHES[s](ctx) != ctx.stages[s].get("input_hash")]
        if changed:
            raise _input_changed(
                run_id, f"終わった工程の入力のハッシュが違う（{'・'.join(changed)}）"
            )
        recorded = json.loads(run["rule_versions_json"])
        current = versions.rule_versions(root, run["domain"])
        drift = [k for k in COMPARED_VERSIONS if recorded.get(k) != current.get(k)]
        if drift:
            raise _input_changed(run_id, f"規則の版が開始時と違う（{'・'.join(drift)}）")
        raw = _raw_problems(ctx)
        if raw:
            raise _input_changed(run_id, "、".join(raw))

        remaining = [s for s in STAGES if s not in done]
        retry = (
            [
                {"source_id": s, "logical_key": k, "status": st, "failure_reason": fr}
                for s, k, st, fr in _fetch_targets(ctx)
                if st == "failed"
            ]
            if "fetch" in remaining
            else []
        )
        with ledger.transaction(conn):
            _update_run(ctx, state="running", stop_reason=None, finished_at=None)
            ledger.add_event(
                conn,
                run_id,
                "resume",
                before={"state": prev_state, "stop_reason": stop},
                after={"skipped": done, "remaining": remaining, "retry_sources": retry},
            )
            if prev_state != "running":
                ledger.add_event(
                    conn,
                    run_id,
                    "state_change",
                    before={"state": prev_state},
                    after={"state": "running"},
                )
        client = ctx.client = fetcher.make_client(transport)  # 入力の検査を通ってから作る
        _run_stages(ctx, remaining)
        result = _complete(ctx, lambda: _accept_checked_config(ctx))
        result["resumed"] = {"skipped": done, "ran": remaining}
        return result
    finally:
        if client is not None:
            client.close()
        conn.close()
