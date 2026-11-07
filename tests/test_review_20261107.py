"""2026-11-07 の2回目のレビュー（利用者が貼った）の指摘のうち、コードで確かめて直したものの回帰テスト。

- 2-1（RD-1・CK-4）：render が合格したときも、得られた強い語の警告（W7_STRONG_WORD）を最上位の warnings に返す
  （implementation_spec.md 3.2。check・record と同じ）。
- 2-2（CLI-1）：runs の行がない台帳（作った直後に止まった実行など）への check・render・公開の検査は、想定外の
  エラー（E_INTERNAL・終了コード5）にせず、E_REF_NOT_FOUND（終了コード1）にする（record・inspect と同じ）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import rec
from kizashi import cli, ledger, publish, render
from kizashi.errors import KizashiError
from test_check import prepare

RUN = "20261107-170000-zzzz"


def _hold_with_strong_word(repo: Path, run_id: str) -> str:
    lead = prepare(repo, run_id, conclusion=False)
    hypothesis = {
        "kind": "hypothesis",
        "lead_id": lead["lead"],
        "text": "異例の増え方に見えるが、翻訳の分量が増えたため、契約金額が増えた。",
        "withdraw_if": "仕様書で分量が同じと分かった場合。",
        "result": "kept",
    }
    rec(repo, run_id, hypothesis, name="h-strong.json")
    rec(
        repo,
        run_id,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "comparable",
            "hold_reason": "comparison",
        },
        name="k-hold.json",
    )
    return lead["lead"]


def test_rd1_render_returns_strong_word_warnings(repo: Path, synthetic_run: str) -> None:
    """RD-1・CK-4（2-1）：render の中の検査が合格しても、強い語の警告を戻り値の warnings に入れる。メモは書く。"""
    lead_id = _hold_with_strong_word(repo, synthetic_run)
    result = render.render_lead(repo, synthetic_run, lead_id)
    assert (repo / result["memo"]).is_file()
    assert [w["code"] for w in result.get("warnings", [])] == ["W7_STRONG_WORD"]


def test_rd1_render_cli_has_top_level_warnings(repo: Path, synthetic_run: str, capsys) -> None:
    """RD-1・CK-4（2-1）：CLI の出力では、warnings を data の外（最上位）に置き、終了コードは0（3.2）。"""
    lead_id = _hold_with_strong_word(repo, synthetic_run)
    capsys.readouterr()
    code = cli.main(["render", lead_id, "--run", synthetic_run])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert [w["code"] for w in payload["warnings"]] == ["W7_STRONG_WORD"]
    assert "warnings" not in payload["data"]


def _ledger_without_runs_row(repo: Path) -> None:
    ledger.create(repo, RUN).close()  # スキーマだけを作り、runs の行を書かない


@pytest.mark.parametrize(
    "argv",
    [["check", "L0001", "--run", RUN], ["render", "L0001", "--run", RUN], ["render", RUN]],
)
def test_cli1_ledger_without_runs_row_is_ref_not_found(repo: Path, capsys, argv) -> None:
    """CLI-1（2-2）：runs の行がない台帳は E_REF_NOT_FOUND（終了コード1）。E_INTERNAL（終了コード5）にしない。"""
    _ledger_without_runs_row(repo)
    code = cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["error"]["code"] == "E_REF_NOT_FOUND"


def test_cli1_publish_terms_without_runs_row_is_ref_not_found(repo: Path) -> None:
    """CLI-1（2-2）：公開の検査が氏名などの一覧を作るときも、runs の行がない台帳は E_REF_NOT_FOUND。"""
    _ledger_without_runs_row(repo)
    with pytest.raises(KizashiError) as exc:
        publish._terms(repo, [RUN])
    assert exc.value.code == "E_REF_NOT_FOUND"
