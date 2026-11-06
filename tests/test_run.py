"""kizashi run（plan〜detect）のテスト（test_plan.md の CF-1、SC-1（取り込みでの除外まで）、
FT-1・FT-3 の実行の中での扱い、完了条件：completed・failed・氏名が台帳にない）。

期待値は implementation_spec.md 4.1・6.2・6.3 と、合成データから手で数えた値。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from conftest import (
    DATASET_DIR,
    DROPPED_WORDS,
    NAME_WORDS,
    PAGE,
    RUN_CONFIG,
    URL,
    Site,
    all_bytes_under,
    all_text_in_ledger,
    fixture_bytes,
    ledger_of,
    write_config,
)
from kizashi import inspect_cmd, runner
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError


def start(repo: Path, site: Site, **config: object) -> dict:
    return runner.start_run(
        repo, "cao-procurement", write_config(repo, **config), transport=site.transport
    )


# ---------------------------------------------------------------------------
# 完了条件：合成データで completed になり、品質レポートが出る
# ---------------------------------------------------------------------------


def test_run_completed_with_synthetic_data(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """令和6・7年度の4ファイル（と PDF）で completed。工程は plan〜detect が done（検出の中身は test_detect.py）。"""
    monkeypatch.setenv("KIZASHI_PROFILE", "investigate")
    monkeypatch.setenv("KIZASHI_CLAUDE_VERSION", "2.1.318 (Claude Code)")
    result = start(repo, site)
    run_id = result["run_id"]
    assert result["state"] == "completed"
    assert result["stop_reason"] is None
    assert list(result["stages"]) == [
        "plan",
        "fetch",
        "normalize",
        "quality",
        "reconcile_import",
        "reconcile_source",
        "detect",
    ]
    assert all(
        s["state"] == "done" and len(s["input_hash"]) == 64 for s in result["stages"].values()
    )
    assert "stages_not_run" not in result
    assert result["leads"] == 16
    assert {name: entry["state"] for name, entry in result["detect"].items()} == {
        "s1_amount_change": "done",
        "s2_method_change": "done",
        "s3_reason_change": "done",
    }
    assert (repo / result["quality_report"]).is_file()

    conn = ledger_of(repo, run_id)
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert run["state"] == "completed" and run["profile"] == "investigate" and run["finished_at"]
    assert json.loads(run["config_json"]) == RUN_CONFIG
    # 設定の全文は氏名の検査を通ってから保存し、同じトランザクションで検査済みを events に残す（D-040①）
    (checked,) = conn.execute(
        "SELECT * FROM events WHERE kind = 'check' AND target_id = ?", (run_id,)
    ).fetchall()
    assert json.loads(checked["after_json"]) == {
        "target": "run_config",
        "check": "V7_PERSONAL_NAME",
        "result": "passed",
        "config_sha256": hashlib.sha256(
            json.dumps(
                RUN_CONFIG, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
    }
    versions = json.loads(run["rule_versions_json"])
    assert (
        versions["dataset_yaml_sha256"]
        == hashlib.sha256((DATASET_DIR / "dataset.yaml").read_bytes()).hexdigest()
    )
    # screens/*.sql、populations/*.sql、series/*.sql を、パスの順（populations/・screens/・series/）に
    # 連結したハッシュ（3.5）
    sql_files = [
        DATASET_DIR / sub / f"{name}.sql"
        for sub in ("populations", "screens", "series")
        for name in ("s1_amount_change", "s2_method_change", "s3_reason_change")
    ]
    assert (
        versions["sql_sha256"]
        == hashlib.sha256(b"".join(p.read_bytes() for p in sql_files)).hexdigest()
    )
    # 規則の版（implementation_spec.md 3.5、共通ルール11）。2：照合を足し Q13 の結果が変わった。
    # 3：D-033〜D-035 で引用・文書値・取得・V7 の検査の結果が変わった。4：D-036 で引用と V7 が変わった。
    # 5：D-037 で探索SQL と実行設定にも氏名の検査を当てた。6：P0-5 で検出（detect）を足した。
    # 7：D-038 で系列を系列のSQLで数え、再開で原本を照らし、引用の符号の境目を直した。
    # 8：D-040 で実行設定の全文の保存を氏名の検査の後にし、探索SQL の結果とエラーの文も検査した。
    # 9：P0-6 で check（V1〜V4・V8）と render（候補のメモ）を足した。10：D-041 の修正。11：D-042 の修正。
    # 12：D-043 の修正。13：D-044 の修正
    assert (versions["rules_version"], versions["parser_version"]) == ("14", "cao-procurement/1")
    assert versions["claude_code_version"] == "2.1.318 (Claude Code)"
    assert {"code_version", "git_commit"} <= set(versions)
    # 1ページ＋8ファイル（2年度×2方式×CSV・PDF）
    assert json.loads(run["limits_json"])["http_requests"] == 9
    sources = conn.execute(
        "SELECT source_id, logical_key, kind, status, url, sha256 FROM sources ORDER BY source_id"
    ).fetchall()
    assert [(s["source_id"], s["logical_key"], s["kind"], s["status"]) for s in sources] == [
        ("S001", "cao-procurement|2024|kyoso|csv", "dataset", "fetched"),
        ("S002", "cao-procurement|2024|kyoso|pdf", "dataset", "fetched"),
        ("S003", "cao-procurement|2024|zuikei|csv", "dataset", "fetched"),
        ("S004", "cao-procurement|2024|zuikei|pdf", "dataset", "fetched"),
        ("S005", "cao-procurement|2025|kyoso|csv", "dataset", "fetched"),
        ("S006", "cao-procurement|2025|kyoso|pdf", "dataset", "fetched"),
        ("S007", "cao-procurement|2025|zuikei|csv", "dataset", "fetched"),
        ("S008", "cao-procurement|2025|zuikei|pdf", "dataset", "fetched"),
        ("S009", None, "index", "fetched"),
    ]
    assert sources[0]["url"] == URL["k24_csv"]
    assert sources[0]["sha256"] == hashlib.sha256(fixture_bytes("kyoso_2024_mini.csv")).hexdigest()
    assert sources[8]["url"] == PAGE
    # レコードの件数（合成データの行数：12・9・12・7）
    counts = dict(
        conn.execute("SELECT source_id, COUNT(*) FROM records GROUP BY source_id").fetchall()
    )
    assert counts == {"S001": 12, "S003": 9, "S005": 12, "S007": 7}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM procurement_contracts WHERE record_status = 'adopted'"
        ).fetchone()[0]
        == 40
    )
    events = [
        (e["kind"], json.loads(e["after_json"]).get("stage"))
        for e in conn.execute("SELECT * FROM events ORDER BY event_id")
    ]
    assert events == [
        ("state_change", None),
        ("stage", "plan"),
        ("stage", "fetch"),
        ("check", None),  # 実行設定の氏名の検査を通り、全文を保存した（D-040①。normalize の最初）
        ("stage", "normalize"),
        ("stage", "quality"),
        ("stage", "reconcile_import"),
        ("stage", "reconcile_source"),
        ("stage", "detect"),
        ("state_change", None),
    ]
    conn.close()
    # 設定ファイルは accepted/ に写して inbox/ から消す
    assert not (repo / "inbox" / "run-config.json").exists()
    accepted = repo / "data" / "runs" / run_id / "accepted" / "0001-run_config.json"
    assert json.loads(accepted.read_text(encoding="utf-8")) == RUN_CONFIG
    assert result["config_accepted"] == f"data/runs/{run_id}/accepted/0001-run_config.json"


def test_run_no_names_or_dropped_values_anywhere(repo: Path, site: Site) -> None:
    """完了条件・NR-5：氏名の語と取り込まない列の値が、台帳・品質レポート・実行フォルダーのどこにもない。"""
    result = start(repo, site)
    conn = ledger_of(repo, result["run_id"])
    text = all_text_in_ledger(conn)
    conn.close()
    files = all_bytes_under(repo / "data" / "runs")
    for word in NAME_WORDS + DROPPED_WORDS:
        assert word not in text, word
        assert word.encode("utf-8") not in files, word


# ---------------------------------------------------------------------------
# FT-1・FT-3：取得の失敗は sources に理由とともに残し、ハッシュを求めない。実行は partial
# ---------------------------------------------------------------------------


def test_run_partial_with_invalid_files(repo: Path) -> None:
    """FT-1：HTML のエラーページ、空のファイル、期待と違う年度のファイルを取得済みにしない。"""
    site = Site(
        {
            URL["k25_csv"]: httpx.Response(
                200, content=fixture_bytes("kyoso_2024_mini.csv")
            ),  # 年度が違う
            URL["z25_csv"]: httpx.Response(
                200, content=b"<!DOCTYPE html><html><body>error</body></html>"
            ),
            URL["z24_pdf"]: httpx.Response(200, content=b""),
            URL["k24_pdf"]: httpx.Response(404),  # FT-3
        }
    )
    result = start(repo, site)
    assert result["state"] == "partial"
    conn = ledger_of(repo, result["run_id"])
    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM sources")}
    expect = {
        "S002": ("failed", "E_FETCH_FAILED", "HTTP 404"),
        "S004": ("failed", "E_FETCH_INVALID", "空のファイル"),
        "S005": ("failed", "E_FETCH_INVALID", "2025年度"),
        "S007": ("failed", "E_FETCH_INVALID", "HTML のページ"),
    }
    for sid, (status, code, why) in expect.items():
        row = rows[sid]
        assert row["status"] == status
        assert row["failure_reason"].startswith(code) and why in row["failure_reason"]
        assert row["sha256"] is None and row["size_bytes"] is None
    for sid in ("S001", "S003", "S006", "S008"):
        assert rows[sid]["status"] == "fetched"
    # data/raw には、取得できた4つと公表ページだけがある。失敗した中身と一時ファイルは残らない
    ext = {"S001": "csv", "S003": "csv", "S006": "pdf", "S008": "pdf", "S009": "html"}
    assert {p.name for p in (repo / "data" / "raw").iterdir()} == {
        f"{rows[s]['sha256']}.{e}" for s, e in ext.items()
    }
    run = conn.execute("SELECT state, stop_reason FROM runs").fetchone()
    missing = json.loads(run["stop_reason"])["missing"]
    assert [m["source_id"] for m in missing] == ["S002", "S004", "S005", "S007"]
    # 品質検査は取得できた CSV（S001・S003）だけで行う
    assert set(
        json.loads(conn.execute("SELECT counts_json FROM runs").fetchone()[0])["quality"]
    ) == {"S001", "S003"}
    conn.close()


def test_run_partial_when_links_missing_or_disallowed(repo: Path, site: Site) -> None:
    """FT-2・FT-3：令和5年度は、許可リスト外・HTTPS でないリンクを取得せず failed、PDF はリンクがなく非公表。"""
    result = start(repo, site, series_years=[2023, 2024, 2025])
    assert result["state"] == "partial"
    conn = ledger_of(repo, result["run_id"])
    rows = {
        r["logical_key"]: r for r in conn.execute("SELECT * FROM sources WHERE kind = 'dataset'")
    }
    k23 = rows["cao-procurement|2023|kyoso|csv"]
    z23 = rows["cao-procurement|2023|zuikei|csv"]
    assert (k23["status"], k23["failure_reason"].split(":")[0]) == ("failed", "E_HOST_NOT_ALLOWED")
    assert (z23["status"], z23["failure_reason"].split(":")[0]) == ("failed", "E_HOST_NOT_ALLOWED")
    assert (
        k23["url"]
        == "https://example.com/chotatsu/kohyo/tekiseika/5nendo/20241105-2_kohyo_kyoso.csv"
    )
    for key in ("cao-procurement|2023|kyoso|pdf", "cao-procurement|2023|zuikei|pdf"):
        assert rows[key]["status"] == "not_published"
        assert rows[key]["sha256"] is None
    assert not any("example.com" in u or u.startswith("http://") for u in site.requests)
    conn.close()


def test_run_transient_failure_is_failed_and_retryable(repo: Path) -> None:
    """FT-3：一時的な通信の失敗が残れば、取得の工程を failed にし、実行は failed（retryable）。正規化に進まない。"""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    site = Site({URL["z24_pdf"]: refuse})
    config = write_config(repo)
    with pytest.raises(KizashiError) as info:
        runner.start_run(repo, "cao-procurement", config, transport=site.transport)
    err = info.value
    assert (err.code, err.retryable) == ("E_FETCH_FAILED", True)
    run_id = err.affected_ids[0]
    assert "S004" in err.affected_ids
    conn = ledger_of(repo, run_id)
    run = conn.execute("SELECT state, stop_reason, stages_json FROM runs").fetchone()
    assert run["state"] == "failed"
    assert json.loads(run["stop_reason"]) | {"message": ""} == {
        "stage": "fetch",
        "code": "E_FETCH_FAILED",
        "retryable": True,
        "message": "",
    }
    assert json.loads(run["stages_json"])["fetch"]["state"] == "failed"
    source = conn.execute(
        "SELECT status, sha256, failure_reason FROM sources WHERE source_id = 'S004'"
    ).fetchone()
    assert (source["status"], source["sha256"]) == ("failed", None)
    assert "再試行" in source["failure_reason"]
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    conn.close()
    assert site.requests.count(URL["z24_pdf"]) == 3
    assert (repo / config).is_file()


# ---------------------------------------------------------------------------
# CF-1：run_config の検査
# ---------------------------------------------------------------------------

SCOPE_OK = {
    "instruction": "記者の指示：食品安全委員会事務局の契約に絞る",
    "filters": [{"field": "officer_unit", "op": "contains", "value": "食品安全委員会"}],
}


@pytest.fixture
def adapter(repo: Path):
    return load_adapter(repo, "cao-procurement")


def test_cf1_examples_pass(adapter) -> None:
    """CF-1：record-examples.md の2つの例（scope なし・あり）の形が検査を通る。"""
    runner.validate_run_config({**RUN_CONFIG, "series_years": [2023, 2024, 2025]}, adapter)
    runner.validate_run_config(
        {**RUN_CONFIG, "series_years": [2023, 2024, 2025], "scope": SCOPE_OK}, adapter
    )
    runner.validate_run_config(
        {**RUN_CONFIG, "compare_years": [2025, 2026], "series_years": [2025, 2026]}, adapter
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"compare_years": [2024, 2026]},  # 連続していない
        {"compare_years": [2025, 2024]},  # 逆順
        {"compare_years": [2024]},
        {"compare_years": ["2024", "2025"]},
        {"compare_years": [2024, True]},
        {"series_years": [2024]},  # compare_years を含まない
        {"series_years": [2023, 2024]},
        {"series_years": [2024, 2024, 2025]},
        {"series_years": [2022, 2024, 2025]},  # dataset.yaml にない年度
        {"series_years": []},
        {"scope": {**SCOPE_OK, "instruction": ""}},
        {"scope": {**SCOPE_OK, "instruction": "   "}},
        {
            "scope": {
                "instruction": "x",
                "filters": [{"field": "industry", "op": "contains", "value": "清掃"}],
            }
        },
        {
            "scope": {
                "instruction": "x",
                "filters": [{"field": "title", "op": "regex", "value": "清掃"}],
            }
        },
        {
            "scope": {
                "instruction": "x",
                "filters": [{"field": "title", "op": "contains", "value": ""}],
            }
        },
        {"scope": {"instruction": "x", "filters": []}},
        {"scope": {"instruction": "x"}},
        {"scope": None},
        {"exclude": []},  # 知らない項目（D-013 で scope に替えた）
        {"kind": "calc"},
        {"domain": "prtr"},
        {"question": ""},
        {"model": ""},
    ],
)
def test_cf1_invalid_configs(adapter, changes: dict) -> None:
    """CF-1：連続しない年度、compare_years を含まない系列、空の instruction、知らない項目・演算は E_SCHEMA。"""
    config = {**RUN_CONFIG, **changes}
    with pytest.raises(KizashiError) as info:
        runner.validate_run_config(config, adapter)
    assert info.value.code == "E_SCHEMA"


@pytest.mark.parametrize(
    "missing", ["kind", "domain", "compare_years", "series_years", "question", "model"]
)
def test_cf1_required_fields(adapter, missing: str) -> None:
    """CF-1：必須の項目がなければ E_SCHEMA。"""
    config = {k: v for k, v in RUN_CONFIG.items() if k != missing}
    with pytest.raises(KizashiError) as info:
        runner.validate_run_config(config, adapter)
    assert info.value.code == "E_SCHEMA"


def test_cf1_bad_config_creates_no_run(repo: Path, site: Site) -> None:
    """CF-1：検査に通らない設定では実行を作らず、ファイルを inbox/ に残す。通信もしない。"""
    config = write_config(repo, compare_years=[2024, 2026])
    with pytest.raises(KizashiError) as info:
        runner.start_run(repo, "cao-procurement", config, transport=site.transport)
    assert info.value.code == "E_SCHEMA"
    assert not (repo / "data" / "runs").exists() or list((repo / "data" / "runs").iterdir()) == []
    assert (repo / config).is_file()
    assert site.requests == []
    (repo / "inbox" / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(KizashiError) as info:
        runner.start_run(repo, "cao-procurement", "inbox/broken.json", transport=site.transport)
    assert info.value.code == "E_SCHEMA"


# ---------------------------------------------------------------------------
# SC-1：母集団の絞り込み（取り込みでの除外まで）
# ---------------------------------------------------------------------------


def statuses(repo: Path, run_id: str) -> dict[str, tuple[str, str | None, str]]:
    conn = ledger_of(repo, run_id)
    rows = conn.execute(
        "SELECT r.record_id, r.status, r.status_reason, p.record_status FROM records r"
        " JOIN procurement_contracts p ON p.record_id = r.record_id"
    ).fetchall()
    conn.close()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def test_sc1_no_scope_excludes_nothing(repo: Path, site: Site) -> None:
    """SC-1：scope がなければ除く行は0。"""
    result = start(repo, site)
    assert all(v == ("adopted", None, "adopted") for v in statuses(repo, result["run_id"]).values())
    assert all(c["out_of_scope"] == 0 for c in result["records"].values())


def test_sc1_officer_unit_any_element(repo: Path, site: Site) -> None:
    """SC-1：所属に「食品安全委員会」を含む行だけを残す。所属の配列のどれかが合えば残る（S001 の NO 4）。"""
    result = start(repo, site, scope=SCOPE_OK)
    st = statuses(repo, result["run_id"])
    kept = sorted(rid for rid, v in st.items() if v[0] == "adopted")
    # 手で数えた残る行：S001 の NO 4（2人目の所属）・NO 12、S003 の NO 4（2人目の所属）
    assert kept == ["S001:000004", "S001:000012", "S003:000004"]
    for rid, value in st.items():
        if rid not in kept:
            assert value == ("excluded", "out_of_scope", "excluded"), rid
    assert {s: c["out_of_scope"] for s, c in result["records"].items()} == {
        "S001": 10,
        "S003": 8,
        "S005": 12,
        "S007": 7,
    }
    assert {s: c["adopted"] for s, c in result["records"].items()} == {
        "S001": 2,
        "S003": 1,
        "S005": 0,
        "S007": 0,
    }
    conn = ledger_of(repo, result["run_id"])
    counts = json.loads(conn.execute("SELECT counts_json FROM runs").fetchone()[0])
    conn.close()
    assert counts["scope"]["instruction"] == SCOPE_OK["instruction"]
    assert counts["scope"]["excluded"] == {"S001": 10, "S003": 8, "S005": 12, "S007": 7}
    # 品質検査は全行で行う（除いた行も数える）
    assert counts["quality"]["S001"]["Q13"]["contracts"] == 12
    assert counts["quality"]["S005"]["Q08"]["groups"] == 1


def test_sc1_title_not_contains_and(repo: Path, site: Site) -> None:
    """SC-1：条件はすべてを満たす行を残す（AND）。件名に「購入」を含まない、を加えると S001 の NO 4 が外れる。"""
    scope = {
        "instruction": "記者の指示：食品安全委員会事務局の契約で、物品の購入を除く",
        "filters": [
            *SCOPE_OK["filters"],
            {"field": "title", "op": "not_contains", "value": "購入"},
        ],
    }
    result = start(repo, site, scope=scope)
    kept = sorted(rid for rid, v in statuses(repo, result["run_id"]).items() if v[0] == "adopted")
    assert kept == ["S001:000012", "S003:000004"]


@pytest.mark.parametrize(
    ("raw", "kind", "op", "value", "expected"),
    [
        (
            '["内閣府大臣官房会計担当参事官", "食品安全委員会事務局総務課長"]',
            "json_array",
            "contains",
            "食品安全委員会",
            True,
        ),
        (
            '["内閣府大臣官房会計担当参事官", "食品安全委員会事務局総務課長"]',
            "json_array",
            "not_contains",
            "食品安全委員会",
            False,
        ),
        ('["内閣衛星情報センター所長"]', "json_array", "not_contains", "参事官", True),
        (
            '["内閣府大臣官房会計担当参事官"]',
            "json_array",
            "equals",
            "内閣府大臣官房会計担当参事官",
            True,
        ),
        (
            '["内閣府 大臣官房会計担当参事官"]',
            "json_array",
            "equals",
            "内閣府大臣官房会計担当参事官",
            True,
        ),
        ('["内閣府大臣官房会計担当参事官"]', "json_array", "not_equals", "内閣府", True),
        ("[]", "json_array", "contains", "内閣府", False),
        ("[]", "json_array", "not_contains", "内閣府", True),
        ("システム保守業務", None, "contains", "ｼｽﾃﾑ", True),  # NFKC で比べる
        ("kyoso", None, "equals", "kyoso", True),
        ("kyoso", None, "not_equals", "zuikei", True),
        (None, None, "equals", "open_call", False),  # 競争入札の理由の分類（NULL）
        (None, None, "not_equals", "open_call", True),
    ],
)
def test_sc1_filter_semantics(raw, kind, op, value, expected) -> None:
    """SC-1：NFKC・空白除去の後に比べる。json_array はどれかの要素で判定し、not_ はどの要素も満たさないとき。"""
    from kizashi.adapters.cao_procurement import filter_matches

    assert filter_matches(raw, kind, op, value) is expected


# ---------------------------------------------------------------------------
# D-037：実行設定の氏名は、取得の後・正規化の前に検査する
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"question": "架空　太郎が担当した契約は、年度間で変わったか"},
        {"scope": {**SCOPE_OK, "instruction": "記者の指示：架空太郎の担当の契約に絞る"}},
        {
            "scope": {
                **SCOPE_OK,
                "filters": [{"field": "title", "op": "contains", "value": "架空 太郎"}],
            }
        },
    ],
)
def test_run_config_with_personal_name_fails_before_normalize(
    repo: Path, site: Site, changes: dict
) -> None:
    """D-037：run_config の文字列（question・scope の指示と条件の値）に氏名があれば、取得の後・正規化の前に
    E_V7 で実行を failed にする。行を書かず、設定は inbox/ に残す。エラーに氏名を書かない。"""
    config = write_config(repo, **changes)
    with pytest.raises(KizashiError) as info:
        runner.start_run(repo, "cao-procurement", config, transport=site.transport)
    err = info.value
    assert (err.code, err.retryable) == ("E_V7", False)
    assert "V7_PERSONAL_NAME" in err.message
    assert "架空" not in err.message and "太郎" not in err.message
    conn = ledger_of(repo, err.affected_ids[0])
    run = conn.execute("SELECT state, stop_reason, stages_json FROM runs").fetchone()
    stop = json.loads(run["stop_reason"])
    assert (run["state"], stop["stage"], stop["code"]) == ("failed", "normalize", "E_V7")
    assert "架空" not in stop["message"] and "太郎" not in stop["message"]
    stages = json.loads(run["stages_json"])
    assert (stages["fetch"]["state"], stages["normalize"]["state"]) == ("done", "failed")
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    # D-040①：検査の前は、設定の全文（question・scope・model）を台帳に書かない。開始時に書くのは、型を区別
    # する正規化JSONのハッシュと、検査済みの分野・年度だけ。検査済みの記録もない
    stored = json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0])
    full = json.loads((repo / config).read_text(encoding="utf-8"))
    assert stored == {
        "kind": "run_config",
        "domain": "cao-procurement",
        "compare_years": [2024, 2025],
        "series_years": [2024, 2025],
        "name_check": "pending",
        "config_sha256": hashlib.sha256(
            json.dumps(full, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
    }
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'check'").fetchone()[0] == 0
    conn.close()
    assert (repo / config).is_file()
    shown = inspect_cmd.inspect(repo, err.affected_ids[0], err.affected_ids[0])
    assert shown["question"] is None and shown["scope"] is None
    assert "保存していない" in shown["config_note"]
    # 台帳のファイル（WAL を含む）のどこにも氏名の語がない
    run_dir = repo / "data" / "runs" / err.affected_ids[0]
    for file in run_dir.glob("ledger.sqlite*"):
        data = file.read_bytes()
        for word in ("架空", "太郎", "テスト用のモデル名"):
            assert word.encode("utf-8") not in data, (file.name, word)
