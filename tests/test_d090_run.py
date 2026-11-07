"""bf1bede の実装レビューの修正のうち、run の回帰テスト（1-8・1-9・1-16・D-091④）。

test_plan.md の DT-1（分野の SQL の誤りは E_INTERNAL で detect を failed）、RS-1・RS-2（再開）、CF-1（成功した
ときの設定ファイルの扱い）の回帰。期待値は implementation_spec.md の 4.1（新しい実行・再開。D-038⑬）と
4.8 の末尾（D-038⑨）、決定の D-091④から書き写した。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import RUN_CONFIG, URL, Site, evidence_pdf_bytes, ledger_of, write_config
from kizashi import fetcher, runner
from kizashi.errors import KizashiError

DATASET = Path("datasets") / "cao-procurement"


def run_row(repo: Path) -> dict:
    (run_dir,) = (repo / "data" / "runs").iterdir()
    conn = ledger_of(repo, run_dir.name)
    row = dict(conn.execute("SELECT * FROM runs").fetchone())
    row["leads"] = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    conn.close()
    return row


# ---------------------------------------------------------------------------
# 1-8（DT-1）：分野の SQL の構文・実行時のエラーは E_INTERNAL で detect を failed と記録する
# ---------------------------------------------------------------------------


def test_dt1_broken_screen_sql_fails_detect(repo: Path, site: Site) -> None:
    """DT-1（1-8）：検出SQL が構文の誤りで動かなければ、detect を failed とし（stages_json と stop_reason の
    工程が detect）、候補を残さない。"""
    (repo / DATASET / "screens" / "s3_reason_change.sql").write_text("SELEC broken", "utf-8")
    with pytest.raises(KizashiError) as exc:
        runner.start_run(repo, "cao-procurement", write_config(repo), transport=site.transport)
    assert exc.value.code == "E_INTERNAL"
    run = run_row(repo)
    assert run["state"] == "failed"
    assert json.loads(run["stages_json"])["detect"]["state"] == "failed"
    stop = json.loads(run["stop_reason"])
    assert (stop["stage"], stop["code"]) == ("detect", "E_INTERNAL")
    assert run["leads"] == 0


def test_dt1_screens_entry_not_a_mapping_fails_detect(repo: Path, site: Site) -> None:
    """DT-1（1-8）：dataset.yaml の screens.<名前> が requires を持つ形でなければ（配列で書くなど）、同じく
    E_INTERNAL で detect を failed にする（4.8 の末尾の「requires が方式の配列でない」）。"""
    path = repo / DATASET / "dataset.yaml"
    text = path.read_text(encoding="utf-8")
    old = "s3_reason_change: {requires: [zuikei]}"
    assert text.count(old) == 1
    path.write_text(text.replace(old, "s3_reason_change: [zuikei]"), encoding="utf-8")
    with pytest.raises(KizashiError) as exc:
        runner.start_run(repo, "cao-procurement", write_config(repo), transport=site.transport)
    assert exc.value.code == "E_INTERNAL"
    run = run_row(repo)
    assert json.loads(run["stages_json"])["detect"]["state"] == "failed"
    assert json.loads(run["stop_reason"])["stage"] == "detect"


# ---------------------------------------------------------------------------
# 1-9（RS-2）：再開の原本の検査は、保存したときの拡張子で探す（fetch --index の PDF）
# ---------------------------------------------------------------------------


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def test_rs2_resume_checks_index_pdf_by_its_extension(repo: Path) -> None:
    """RS-2（1-9）：止まった実行に fetch --index で PDF を足しても、再開の原本の検査が「原本がない」にしない。"""
    with pytest.raises(KizashiError) as info:
        runner.start_run(
            repo,
            "cao-procurement",
            write_config(repo),
            transport=Site({URL["z24_pdf"]: refuse}).transport,
        )
    run_id = info.value.affected_ids[0]
    index_pdf = "https://www.cao.go.jp/test/chotatsu_index.pdf"
    site = Site(
        {
            index_pdf: httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=evidence_pdf_bytes("spec_sample"),
            )
        }
    )
    fetched = fetcher.fetch_evidence(repo, run_id, index_pdf, "index", transport=site.transport)
    assert fetched["kind"] == "index"
    result = runner.resume_run(repo, run_id, transport=site.transport)
    assert result["state"] == "completed"


def test_rs2_resume_still_refuses_missing_index_pdf(repo: Path) -> None:
    """RS-2（1-9）：fetch --index の PDF の原本を消せば、これまでどおり E_INPUT_CHANGED（状態を変えない）。"""
    with pytest.raises(KizashiError) as info:
        runner.start_run(
            repo,
            "cao-procurement",
            write_config(repo),
            transport=Site({URL["z24_pdf"]: refuse}).transport,
        )
    run_id = info.value.affected_ids[0]
    index_pdf = "https://www.cao.go.jp/test/chotatsu_index.pdf"
    site = Site(
        {
            index_pdf: httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=evidence_pdf_bytes("spec_sample"),
            )
        }
    )
    fetched = fetcher.fetch_evidence(repo, run_id, index_pdf, "index", transport=site.transport)
    (repo / "data" / "raw" / f"{fetched['sha256']}.pdf").unlink()
    with pytest.raises(KizashiError) as exc:
        runner.resume_run(repo, run_id, transport=site.transport)
    assert exc.value.code == "E_INPUT_CHANGED"
    assert fetched["source_id"] in exc.value.message


# ---------------------------------------------------------------------------
# 1-16・D-091④（CF-1・RS-1）：新しい実行でも、成功したときに inbox/ の設定ファイルを開始時の検査済みの設定と
# 照らし、違えば・なければ・読めなければ検査済みの設定を accepted/ に書いて inbox/ を触らない
# ---------------------------------------------------------------------------


def _run_with_change(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch, change
) -> tuple[dict, Path, str]:
    config = write_config(repo)
    original = runner.STAGE_FUNCS["detect"]

    def detect_then_change(ctx):
        original(ctx)
        change(repo / config)

    monkeypatch.setitem(runner.STAGE_FUNCS, "detect", detect_then_change)
    result = runner.start_run(repo, "cao-procurement", config, transport=site.transport)
    return result, repo / result["config_accepted"], config


def test_cf1_changed_config_file_is_not_copied(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CF-1（D-091④）：実行中に問いを書き換えた設定ファイルは accepted/ に写さず、inbox/ に残す。accepted/ には
    開始時の検査済みの設定を書く。"""
    changed = {**RUN_CONFIG, "question": "途中で書き換えた問い"}
    result, accepted, config = _run_with_change(
        repo,
        site,
        monkeypatch,
        lambda p: p.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8"),
    )
    assert result["state"] == "completed"
    assert json.loads(accepted.read_text(encoding="utf-8")) == RUN_CONFIG
    assert json.loads((repo / config).read_text(encoding="utf-8")) == changed


def test_cf1_removed_config_file_is_not_an_error(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CF-1（D-091④）：実行中に設定ファイルを消しても、completed の後に E_INTERNAL にしない。accepted/ には
    開始時の検査済みの設定を書く。"""
    result, accepted, _ = _run_with_change(repo, site, monkeypatch, lambda p: p.unlink())
    assert result["state"] == "completed"
    assert json.loads(accepted.read_text(encoding="utf-8")) == RUN_CONFIG


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({**RUN_CONFIG, "compare_years": [2024.0, 2025]}, ensure_ascii=False),  # 数値の型
        "{not json",
    ],
)
def test_cf1_number_type_or_broken_json_counts_as_changed(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """CF-1（D-091④）：数値の型の違い（2024 と 2024.0）と、JSON として読めないファイルも「違う」とみなす。"""
    result, accepted, config = _run_with_change(
        repo, site, monkeypatch, lambda p: p.write_text(content, encoding="utf-8")
    )
    assert result["state"] == "completed"
    assert json.loads(accepted.read_text(encoding="utf-8")) == RUN_CONFIG
    assert (repo / config).read_text(encoding="utf-8") == content


def test_cf1_same_config_with_other_key_order_is_accepted(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CF-1（D-091④）：キーの順と空白だけが違うなら同じ中身として、ファイルを accepted/ に写して inbox/ から消す。"""
    reordered = json.dumps(dict(reversed(list(RUN_CONFIG.items()))), ensure_ascii=False, indent=4)
    result, accepted, config = _run_with_change(
        repo, site, monkeypatch, lambda p: p.write_text(reordered, encoding="utf-8")
    )
    assert result["state"] == "completed"
    assert accepted.read_text(encoding="utf-8") == reordered
    assert not (repo / config).exists()
