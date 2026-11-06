"""実行の概要（kizashi render <run_id>。test_plan.md の RD-3、SC-1 の実行の概要。implementation_spec.md 4.7・
10.3、設計書4.2）。

期待値は合成データ（tests/fixtures/make_cao_fixtures.py）から手で数えたもの：
- 比べた組の数は test_detect.py の DT-5 と同じ（S1 11組・比較できない6組・候補13件、S2 17組・候補2件、
  S3 5組・候補1件。合計33組）。
- 件名に「調査研究」を含む行は、令和6年度の随意契約（S003）と令和7年度の随意契約（S007）に1行ずつ
  （「調査研究委託」）。競争入札（S001・S005）には0行。
- 令和7年度の競争入札の CSV が取れない実行では、S1・S2 が未実行（欠けた資料 kyoso:2025）で、S3 は5組・候補1件
  （DT-3 と同じ）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from conftest import URL, Site, ledger_of, write_config
from kizashi import cli, detect, render, runner
from kizashi.errors import KizashiError


def start(repo: Path, site: Site, **overrides: object) -> str:
    result = runner.start_run(
        repo, "cao-procurement", write_config(repo, **overrides), transport=site.transport
    )
    return result["run_id"]


def section(text: str, heading: str) -> str:
    """「## <見出し>」から次の「## 」の前まで。"""
    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE
    )
    assert match is not None, heading
    return match.group(1)


def summary_text(repo: Path, run_id: str, **kw: object) -> tuple[dict, str]:
    result = render.render_run(repo, run_id, **kw)
    return result, (repo / result["summary"]).read_text(encoding="utf-8")


def test_rd3_summary_sections_and_counts(repo: Path, synthetic_run: str) -> None:
    """RD-3：実行の概要の5節がそろい、検出SQLごとの比べた組の数と候補の数が出る。番号を進め、上書きしない。"""
    result, text = summary_text(repo, synthetic_run)
    assert result["summary"] == f"data/runs/{synthetic_run}/memos/run_summary.v1.md"
    assert text.startswith(
        "# 実行の概要：同種の業務で、契約金額・契約方式・随意契約の理由が年度間で変わったものはあるか\n"
    )
    assert f"> 調査用・記者未確認。実行ID：{synthetic_run}　状態：completed（完了）" in text
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert headings == ["取得", "処理の範囲", "品質レポートの要約", "検出", "候補"]
    detect_part = section(text, "検出")
    assert "| s1_amount_change | 実行済み | 11 | 6 | 13 |" in detect_part
    assert "| s2_method_change | 実行済み | 17 | 0 | 2 |" in detect_part
    assert "| s3_reason_change | 実行済み | 5 | 0 | 1 |" in detect_part
    assert "- 検出の候補の総数：16件" in detect_part
    assert "該当なし" not in detect_part and "候補なし" not in text
    assert "- 母集団の絞り込み：なし" in section(text, "処理の範囲")
    assert "調べずに残した候補：16件" in section(text, "候補")
    again, _ = summary_text(repo, synthetic_run)
    assert again["summary_no"] == 2
    assert (repo / result["summary"]).read_text(encoding="utf-8") == text  # v1 は書き換えない
    conn = ledger_of(repo, synthetic_run)
    events = conn.execute("SELECT after_json FROM events WHERE kind = 'render'").fetchall()
    conn.close()
    assert [json.loads(e[0])["summary_no"] for e in events] == [1, 2]


def test_rd3_zero_leads_says_none_among_compared(
    repo: Path, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RD-3：すべての検出SQLが実行され、候補が0件なら「比べられたN組の中で該当なし」と書く（N は33組）。"""
    original = detect.execute_sql

    def no_rows(conn, path, params):
        if path.parent.name != "screens":
            return original(conn, path, params)
        sql = path.read_text(encoding="utf-8").rstrip().rstrip(";")
        return conn.execute(f"SELECT * FROM ({sql}\n) WHERE 0", params)

    monkeypatch.setattr(detect, "execute_sql", no_rows)
    run_id = start(repo, site)
    _, text = summary_text(repo, run_id)
    detect_part = section(text, "検出")
    assert "- 検出の候補：比べられた33組の中で該当なし（検出SQLのすべてで0件）。" in detect_part
    assert (
        "| s1_amount_change | 実行済み | 11 | 6 | 0（比べられた11組の中で該当なし） |"
        in detect_part
    )
    assert "候補なし" not in text


def test_rd3_partial_and_not_run_are_not_none(repo: Path) -> None:
    """RD-3：partial の実行は「候補なし」「該当なし」とまとめない。未実行の検出SQLは比べた組の数の代わりに
    「未実行」と欠けた資料を書く。取れなかった資料は取得の節に理由と一緒に出る。"""
    run_id = start(repo, Site({URL["k25_csv"]: httpx.Response(404)}))
    _, text = summary_text(repo, run_id)
    assert "状態：partial（一部が欠けた）" in text
    detect_part = section(text, "検出")
    assert "| s1_amount_change | 未実行（欠けた資料：kyoso:2025） | — | — | — | — |" in detect_part
    assert "| s2_method_change | 未実行（欠けた資料：kyoso:2025） | — | — | — | — |" in detect_part
    assert "| s3_reason_change | 実行済み | 5 | 0 | 1 |" in detect_part
    assert "- 検出の候補の総数：1件" in detect_part
    assert "「候補なし」とは言えない" in detect_part
    assert "比べられた33組" not in text and "候補なし。" not in text
    fetch_part = section(text, "取得")
    assert re.search(
        r"\| S005 \| 令和7年度（2025年度） \| 競争入札 \| CSV \| 取得できなかった \| .*404",
        fetch_part,
    )
    scope_part = section(text, "処理の範囲")
    assert "- 検出SQL s1_amount_change：未実行（欠けた資料：kyoso:2025）" in scope_part


def test_sc1_summary_shows_instruction_conditions_and_excluded_rows(repo: Path, site: Site) -> None:
    """SC-1（実行の概要）：絞り込みがあれば、処理の範囲に記者の指示、条件、資料ごとの除いた行の数を出す
    （件名に「調査研究」を含む行：S003 と S007 に1行ずつ）。"""
    scope = {
        "instruction": "記者の指示：調査研究の委託を除く",
        "filters": [{"field": "title", "op": "not_contains", "value": "調査研究"}],
    }
    run_id = start(repo, site, scope=scope)
    _, text = summary_text(repo, run_id)
    part = section(text, "処理の範囲")
    assert "- 記者の指示：記者の指示：調査研究の委託を除く" in part
    assert "- 条件：title が「調査研究」を含まない" in part
    for source_id, excluded in (("S001", 0), ("S003", 1), ("S005", 0), ("S007", 1)):
        assert f"| {source_id} | {excluded} |" in part
    assert "絞り込む前の全行で行った" in part


def test_rd3_cli_render_run_and_rule_version(repo: Path, synthetic_run: str, capsys) -> None:
    """render <run_id> は CLI から呼べる。規則の版が違えば E_RULE_VERSION（終了コード3。HS-2 の実行の概要）。"""
    code = cli.main(["render", synthetic_run])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["data"]["summary_no"] == 1
    conn = ledger_of(repo, synthetic_run)
    conn.execute(
        "UPDATE runs SET rule_versions_json = json_set(rule_versions_json, '$.rules_version', '0')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(KizashiError) as info:
        render.render_run(repo, synthetic_run)
    assert info.value.code == "E_RULE_VERSION" and info.value.exit_code == 3
    assert not (repo / "data" / "runs" / synthetic_run / "memos" / "run_summary.v2.md").exists()
