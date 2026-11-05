"""kizashi run --resume のテスト（test_plan.md の RS-1・RS-2。implementation_spec.md 4.1 の「再開」）。

- 再開できるのは、状態が running（強制終了で残ったもの）か、stop_reason が一時的な通信の失敗の failed だけ。
- 終わった工程は、入力のハッシュがいまと同じなら飛ばす。違えば E_INPUT_CHANGED。
- detect が終わっていれば候補を作り直さない。途中で止まった detect は最初からやり直す（二重にしない）。

強制終了は、工程の中で KeyboardInterrupt を起こしてまねる（Exception でないので failed にならず、
トランザクションは巻き戻る）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import URL, Site, ledger_of, write_config
from kizashi import cli, detect, runner
from kizashi.errors import KizashiError


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def transient_failed_run(repo: Path) -> tuple[str, str]:
    """令和6年度の随意契約の PDF だけが一時的な通信の失敗で取れず、failed（retryable）になった実行。"""
    config = write_config(repo)
    with pytest.raises(KizashiError) as info:
        runner.start_run(
            repo, "cao-procurement", config, transport=Site({URL["z24_pdf"]: refuse}).transport
        )
    assert (info.value.code, info.value.retryable) == ("E_FETCH_FAILED", True)
    return info.value.affected_ids[0], config


def crashed_run(repo: Path, monkeypatch: pytest.MonkeyPatch, site: Site, where: str) -> str:
    """where の場所で強制終了した（running のまま残った）実行。"""
    original = detect.execute_sql

    def crash_in_detect(conn, path, params):
        if path.parent.name == "populations" and path.stem == "s3_reason_change":
            raise KeyboardInterrupt  # S1・S2 の候補を登録した後、S3 の母集団の前
        return original(conn, path, params)

    def crash_at_finish(*args, **kwargs):
        raise KeyboardInterrupt  # 検出まで確定した後、実行の状態を確定する前

    with monkeypatch.context() as m:
        if where == "detect":
            m.setattr(detect, "execute_sql", crash_in_detect)
        else:
            m.setattr(runner, "_finish", crash_at_finish)
        with pytest.raises(KeyboardInterrupt):
            runner.start_run(repo, "cao-procurement", write_config(repo), transport=site.transport)
    (run_dir,) = (repo / "data" / "runs").iterdir()
    run_id = run_dir.name
    conn = ledger_of(repo, run_id)
    assert conn.execute("SELECT state FROM runs").fetchone()[0] == "running"
    conn.close()
    return run_id


def stage_events(repo: Path, run_id: str) -> list[str]:
    conn = ledger_of(repo, run_id)
    rows = conn.execute("SELECT kind, after_json FROM events ORDER BY event_id").fetchall()
    conn.close()
    return [json.loads(a)["stage"] for k, a in rows if k == "stage"]


def leads_and_calcs(repo: Path, run_id: str) -> tuple[list[tuple], int]:
    conn = ledger_of(repo, run_id)
    leads = [
        tuple(r)
        for r in conn.execute(
            "SELECT lead_id, screen_id, lead_key, change_category, calc_from, calc_to FROM leads"
            " ORDER BY lead_id"
        )
    ]
    n = conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0]
    conn.close()
    return leads, n


# ---------------------------------------------------------------------------
# RS-1：終わった工程を重複させず、止まった工程から続ける
# ---------------------------------------------------------------------------


def test_rs1_resume_after_transient_fetch_failure(repo: Path) -> None:
    """RS-1：一時的な通信の失敗で止まった実行を再開すると、取れなかった資料だけを取り直し、公表ページと
    取得済みの資料は取り直さない。plan は飛ばし、fetch から detect まで行って completed になる。"""
    run_id, config = transient_failed_run(repo)
    site = Site()
    result = runner.resume_run(repo, run_id, transport=site.transport)
    assert result["state"] == "completed" and result["run_id"] == run_id
    assert site.requests == [URL["z24_pdf"]]
    assert result["resumed"] == {
        "skipped": ["plan"],
        "ran": ["fetch", "normalize", "quality", "reconcile_import", "reconcile_source", "detect"],
    }
    assert stage_events(repo, run_id) == [
        "plan",
        "fetch",  # 1回目（failed）
        "fetch",
        "normalize",
        "quality",
        "reconcile_import",
        "reconcile_source",
        "detect",
    ]
    conn = ledger_of(repo, run_id)
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert (run["state"], run["stop_reason"]) == ("completed", None)
    # 1回目：公表ページ1、ほかの7本、令和6年度の随意契約の PDF 3回（再試行2回を含む）。再開で1回
    assert json.loads(run["limits_json"])["http_requests"] == 1 + 7 + 3 + 1
    source = conn.execute(
        "SELECT status, failure_reason FROM sources WHERE source_id = 'S004'"
    ).fetchone()
    assert (source["status"], source["failure_reason"]) == ("fetched", None)
    resume = conn.execute("SELECT * FROM events WHERE kind = 'resume'").fetchall()
    assert len(resume) == 1 and resume[0]["target_id"] == run_id
    assert json.loads(resume[0]["before_json"])["state"] == "failed"
    retried = json.loads(resume[0]["after_json"])["retry_sources"]
    assert [r["source_id"] for r in retried] == ["S004"]
    assert retried[0]["status"] == "failed" and "再試行" in retried[0]["failure_reason"]
    states = [
        (json.loads(e["before_json"])["state"], json.loads(e["after_json"])["state"])
        for e in conn.execute("SELECT * FROM events WHERE kind = 'state_change' ORDER BY event_id")
    ]
    assert states == [
        (None, "running"),
        ("running", "failed"),
        ("failed", "running"),
        ("running", "completed"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 16
    conn.close()
    # 成功したので、設定ファイルを accepted/ に写し、inbox/ から消す
    assert not (repo / config).exists()
    assert (repo / "data" / "runs" / run_id / "accepted" / "0001-run_config.json").is_file()
    assert result["config_accepted"] == f"data/runs/{run_id}/accepted/0001-run_config.json"


def test_rs1_detect_interrupted_is_redone_once(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RS-1：detect の途中で止めると、確定していない候補は残らない。再開で detect を最初からやり直し、
    候補は一度だけできる。照合までの工程は繰り返さない。"""
    run_id = crashed_run(repo, monkeypatch, site, "detect")
    leads, n_calcs = leads_and_calcs(repo, run_id)
    assert leads == [] and n_calcs == 8  # 照合の計算だけ
    resume_site = Site()
    result = runner.resume_run(repo, run_id, transport=resume_site.transport)
    assert result["state"] == "completed"
    assert resume_site.requests == []
    assert result["resumed"]["ran"] == ["detect"]
    leads, n_calcs = leads_and_calcs(repo, run_id)
    assert [lead[0] for lead in leads] == [f"L{n:04d}" for n in range(1, 17)]
    assert n_calcs == 8 + 72
    assert stage_events(repo, run_id).count("reconcile_source") == 1
    assert stage_events(repo, run_id).count("detect") == 1
    with pytest.raises(KizashiError) as info:  # completed になった実行は再開できない
        runner.resume_run(repo, run_id, transport=resume_site.transport)
    assert info.value.code == "E_NOT_RESUMABLE"


def test_rs1_detect_done_is_not_redone(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RS-1：detect が確定した後に止まった実行は、再開で工程を1つも繰り返さず、候補を作り直さない。"""
    run_id = crashed_run(repo, monkeypatch, site, "finish")
    before = leads_and_calcs(repo, run_id)
    assert len(before[0]) == 16
    events_before = stage_events(repo, run_id)
    result = runner.resume_run(repo, run_id, transport=Site().transport)
    assert result["state"] == "completed"
    assert result["resumed"] == {
        "skipped": [
            "plan",
            "fetch",
            "normalize",
            "quality",
            "reconcile_import",
            "reconcile_source",
            "detect",
        ],
        "ran": [],
    }
    assert leads_and_calcs(repo, run_id) == before
    assert stage_events(repo, run_id) == events_before
    assert result["detect"]["s1_amount_change"]["leads"] == 13


# ---------------------------------------------------------------------------
# RS-2：入力が変わっていれば、また partial・completed・再試行できない failed では再開を拒む
# ---------------------------------------------------------------------------


def test_rs2_not_resumable_states(repo: Path, site: Site) -> None:
    """RS-2：completed・partial と、一時的な通信の失敗でない failed は E_NOT_RESUMABLE（終了コード3）。"""
    completed = runner.start_run(
        repo, "cao-procurement", write_config(repo), transport=site.transport
    )["run_id"]
    partial = runner.start_run(
        repo,
        "cao-procurement",
        write_config(repo),
        transport=Site({URL["k24_pdf"]: httpx.Response(404)}).transport,
    )
    assert partial["state"] == "partial"
    with pytest.raises(KizashiError) as info:  # 実行設定に氏名（E_V7）。再試行できない failed
        runner.start_run(
            repo,
            "cao-procurement",
            write_config(repo, question="架空太郎の契約に変化はあるか"),
            transport=site.transport,
        )
    failed = info.value.affected_ids[0]
    for run_id in (completed, partial["run_id"], failed):
        with pytest.raises(KizashiError) as info:
            runner.resume_run(repo, run_id, transport=site.transport)
        assert (info.value.code, info.value.exit_code) == ("E_NOT_RESUMABLE", 3)
    assert site.requests.count(URL["k24_csv"]) == 2  # 再開の試みで通信していない


def test_rs2_changed_dataset_yaml_is_input_changed(repo: Path) -> None:
    """RS-2：終わった工程（plan）の入力（dataset.yaml）が変わっていれば E_INPUT_CHANGED。実行は変えない。"""
    run_id, _ = transient_failed_run(repo)
    yaml_path = repo / "datasets" / "cao-procurement" / "dataset.yaml"
    yaml_path.write_text(yaml_path.read_text(encoding="utf-8") + "# 変更\n", encoding="utf-8")
    site = Site()
    with pytest.raises(KizashiError) as info:
        runner.resume_run(repo, run_id, transport=site.transport)
    assert (info.value.code, info.value.exit_code) == ("E_INPUT_CHANGED", 3)
    assert "plan" in info.value.message
    assert site.requests == []
    conn = ledger_of(repo, run_id)
    assert conn.execute("SELECT state FROM runs").fetchone()[0] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'resume'").fetchone()[0] == 0
    conn.close()


def test_rs2_changed_records_is_input_changed(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RS-2：終わった工程の入力（台帳のレコード）が変わっていれば E_INPUT_CHANGED。"""
    run_id = crashed_run(repo, monkeypatch, site, "finish")
    conn = ledger_of(repo, run_id)
    conn.execute("UPDATE records SET status_reason = 'changed' WHERE record_id = 'S001:000001'")
    conn.commit()
    conn.close()
    with pytest.raises(KizashiError) as info:
        runner.resume_run(repo, run_id, transport=site.transport)
    assert info.value.code == "E_INPUT_CHANGED"
    assert "quality" in info.value.message


def test_rs2_changed_screen_sql_is_input_changed(repo: Path) -> None:
    """RS-2：detect の前に止まった実行でも、検出SQL（規則の版）が開始時と違えば E_INPUT_CHANGED。"""
    run_id, _ = transient_failed_run(repo)
    sql = repo / "datasets" / "cao-procurement" / "screens" / "s1_amount_change.sql"
    sql.write_text(sql.read_text(encoding="utf-8") + "\n-- 変更\n", encoding="utf-8")
    with pytest.raises(KizashiError) as info:
        runner.resume_run(repo, run_id, transport=Site().transport)
    assert info.value.code == "E_INPUT_CHANGED"
    assert "sql_sha256" in info.value.message


RAW_CASES = {
    # (変える原本の資料を選ぶ条件, 変え方)
    "csv_changed": ("kind = 'dataset' AND logical_key LIKE '%|csv'", "change"),
    "csv_missing": ("kind = 'dataset' AND logical_key LIKE '%|csv'", "delete"),
    "pdf_changed": ("kind = 'dataset' AND logical_key LIKE '%|pdf'", "change"),
    "index_changed": ("kind = 'index'", "change"),
}


@pytest.mark.parametrize("case", sorted(RAW_CASES))
def test_rs2_changed_raw_file_is_input_changed(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """RS-2：再開で使い続ける原本（data/raw の CSV・PDF・公表ページの HTML）がないか、中身の SHA-256 が
    記録と違えば、状態を変える前に E_INPUT_CHANGED（資料ID を書く）。台帳は変えない（running のまま、
    resume の events も候補もできない）。通信もしない。"""
    run_id = crashed_run(repo, monkeypatch, site, "detect")  # 照合の後、検出の途中で止まった実行
    where, how = RAW_CASES[case]
    conn = ledger_of(repo, run_id)
    source_id, sha, kind, key = conn.execute(
        f"SELECT source_id, sha256, kind, logical_key FROM sources WHERE {where}"
        " AND status = 'fetched' ORDER BY source_id LIMIT 1"
    ).fetchone()
    conn.close()
    ext = "html" if kind == "index" else key.rsplit("|", 1)[1]
    raw = repo / "data" / "raw" / f"{sha}.{ext}"
    assert raw.is_file()
    if how == "change":
        raw.write_bytes(raw.read_bytes() + b"\n")
    else:
        raw.unlink()
    resume_site = Site()
    with pytest.raises(KizashiError) as info:
        runner.resume_run(repo, run_id, transport=resume_site.transport)
    assert (info.value.code, info.value.exit_code) == ("E_INPUT_CHANGED", 3)
    assert source_id in info.value.message
    assert ("ない" if how == "delete" else "SHA-256") in info.value.message
    assert resume_site.requests == []
    conn = ledger_of(repo, run_id)
    assert conn.execute("SELECT state FROM runs").fetchone()[0] == "running"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'resume'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    conn.close()


def test_rs1_resume_keeps_inbox_config_with_other_number_types(repo: Path) -> None:
    """RS-1（D-038⑬）：再開で成功したとき、inbox/ の設定ファイルを開始時の設定と、数値の型も保つ正規化 JSON
    で照らす。年度を 2024.0 に書き換えたファイルは別の中身として、台帳の設定を accepted/ に書き、inbox/ の
    ファイルは消さない。キーの順と空白だけが違うファイルは同じ中身として accepted/ に写して消す。"""
    run_id, config = transient_failed_run(repo)
    stored = json.loads((repo / config).read_text(encoding="utf-8"))
    changed = json.dumps({**stored, "compare_years": [2024.0, 2025]}, ensure_ascii=False)
    assert json.loads(changed) == stored  # Python の == では同じに見える
    (repo / config).write_text(changed, encoding="utf-8")
    result = runner.resume_run(repo, run_id, transport=Site().transport)
    assert result["state"] == "completed"
    assert (repo / config).read_text(encoding="utf-8") == changed  # 消さない
    accepted = repo / "data" / "runs" / run_id / "accepted" / "0001-run_config.json"
    assert json.loads(accepted.read_text(encoding="utf-8")) == stored
    assert json.loads(accepted.read_text(encoding="utf-8"))["compare_years"] == [2024, 2025]
    assert all(
        type(y) is int for y in json.loads(accepted.read_text(encoding="utf-8"))["compare_years"]
    )

    (repo / config).unlink()
    run_id, config = transient_failed_run(repo)
    reordered = json.dumps(dict(reversed(list(stored.items()))), ensure_ascii=False, indent=4)
    (repo / config).write_text(reordered, encoding="utf-8")
    result = runner.resume_run(repo, run_id, transport=Site().transport)
    assert result["state"] == "completed"
    assert not (repo / config).exists()
    assert result["config_accepted"] == f"data/runs/{run_id}/accepted/0001-run_config.json"


def test_rs2_cli_resume(repo: Path, site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI：kizashi run --resume <run_id> が再開を呼ぶ。completed の実行は E_NOT_RESUMABLE（終了コード3）。"""
    run_id = runner.start_run(
        repo, "cao-procurement", write_config(repo), transport=site.transport
    )["run_id"]
    capsys.readouterr()
    assert cli.main(["run", "--resume", run_id]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"]["code"] == "E_NOT_RESUMABLE"
    assert out["error"]["affected_ids"] == [run_id]
