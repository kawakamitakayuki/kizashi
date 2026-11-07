"""D-095（51699c3 の実装レビューで再現された5点。利用者が直す判断を Claude Code に任せた）の回帰テスト。

- ①（IN-1・QC-1・QU-1）：書き込み先のファイルそのものが data/ の外へのリンクなら、書かずに止める（実装仕様3.4）。
- ②（CLI-1・QU-1）：探索SQL の結果に有限でない数（無限大・NaN）があれば、保存・返却の前に拒否する。CLI の出力は
  標準の JSON に限る（3.2）。
- ③（CF-1）：run_config の scope.filters の型の誤りは E_SCHEMA（3.7・6.3）。
- ④（HS-2・CLI-1）：構文の壊れた dataset.yaml では、check・record・render は E_RULE_VERSION（終了コード3。D-091①）。
- ⑤（CLI-1・KN-2）：inspect の候補の取材質問も、各欄を2,000字で切り、1ページ20件に分ける（3.2・4.3）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import RUN_CONFIG, calc_sum, put, rec
from kizashi import cli, inspect_cmd, quality, query, runner
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError


def strict_json(text: str) -> dict:
    """Infinity・NaN を受け付けない JSON の読み込み（標準の JSON）。"""

    def refuse(name: str) -> None:
        raise ValueError(f"標準の JSON にない値：{name}")

    return json.loads(text, parse_constant=refuse)


def write_sql(root: Path, name: str, sql: str) -> str:
    (root / "inbox" / name).write_text(sql, encoding="utf-8")
    return f"inbox/{name}"


# ---------------------------------------------------------------------------
# ①：書き込み先のファイルが data/ の外へのリンク
# ---------------------------------------------------------------------------


def test_qc1_quality_report_link_to_outside_is_refused(
    repo: Path, synthetic_run: str, tmp_path: Path
) -> None:
    """QC-1・IN-1（D-095①）：品質レポートのファイルが外へのリンクなら、外のファイルを上書きせずに止める。"""
    outside = tmp_path / "outside.md"
    outside.write_text("外のファイル", encoding="utf-8")
    report = repo / "data" / "runs" / synthetic_run / quality.REPORT_NAME
    report.unlink()
    os.symlink(outside, report)
    with pytest.raises(KizashiError) as exc:
        quality.write_report(repo, synthetic_run, "# 品質レポート\n")
    assert exc.value.code == "E_INTERNAL"
    assert outside.read_text(encoding="utf-8") == "外のファイル"


def test_qu1_accepted_link_to_outside_is_refused(
    repo: Path, synthetic_run: str, tmp_path: Path
) -> None:
    """QU-1・IN-1（D-095①）：accepted/ に同じ名前の、外の未作成のファイルへのリンクがあれば、同じ名前があるものとして
    拒否し、外にファイルを作らない。提案は inbox/ に残る。"""
    outside = tmp_path / "outside" / "created.sql"
    outside.parent.mkdir()
    accepted = repo / "data" / "runs" / synthetic_run / "accepted"
    os.symlink(outside, accepted / "q-link.sql")
    arg = write_sql(repo, "q-link.sql", "SELECT COUNT(*) FROM records")
    with pytest.raises(KizashiError) as exc:
        query.run_query(repo, synthetic_run, arg)
    assert exc.value.code == "E_QUERY_REJECTED"
    assert not outside.exists()
    assert (repo / arg).is_file()


# ---------------------------------------------------------------------------
# ②：有限でない数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", ["SELECT 1e999 AS x", "SELECT 1, -1e999 AS y"])
def test_qu1_non_finite_result_is_refused(repo: Path, synthetic_run: str, sql: str) -> None:
    """QU-1（D-095②）：結果に無限大があれば、保存も返却もせず E_QUERY_REJECTED（位置だけを書く）。"""
    arg = write_sql(repo, "q-inf.sql", sql)
    with pytest.raises(KizashiError) as exc:
        query.run_query(repo, synthetic_run, arg)
    assert exc.value.code == "E_QUERY_REJECTED"
    assert "結果の1行目" in exc.value.message
    run = repo / "data" / "runs" / synthetic_run
    assert not list((run / "queries").glob("*q-inf*"))
    assert not (run / "accepted" / "q-inf.sql").exists()
    assert (repo / arg).is_file()


def test_cli1_query_output_is_standard_json(repo: Path, synthetic_run: str, capsys) -> None:
    """CLI-1（D-095②）：有限でない数の探索SQL でも、CLI の出力は標準の JSON（Infinity を含まない）。"""
    arg = write_sql(repo, "q-inf2.sql", "SELECT 1e999 AS x")
    capsys.readouterr()
    code = cli.main(["query", synthetic_run, arg])
    payload = strict_json(capsys.readouterr().out)
    assert code == 1
    assert payload["error"]["code"] == "E_QUERY_REJECTED"


def test_cli1_success_output_refuses_non_finite(
    repo: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-1（D-095②）：操作の戻り値に有限でない数が混じっても、出力は標準の JSON（E_INTERNAL）。"""
    monkeypatch.setitem(cli.HANDLERS, "inspect", lambda args: {"value": float("inf")})
    code = cli.main(["inspect", "20261105-101500-a3f2"])
    payload = strict_json(capsys.readouterr().out)
    assert code == 5 and payload["error"]["code"] == "E_INTERNAL"


# ---------------------------------------------------------------------------
# ③：scope.filters の型の誤り
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        {"field": [], "op": "contains", "value": "食品"},
        {"field": {"x": 1}, "op": "contains", "value": "食品"},
        {"field": "title", "op": [], "value": "食品"},
        {"field": "title", "op": "contains", "value": ["食品"]},
    ],
)
def test_cf1_scope_filter_type_errors_are_schema(repo: Path, item: dict) -> None:
    """CF-1（D-095③）：field・op・value が文字列でなければ E_SCHEMA（想定外のエラーにしない）。"""
    config = {**RUN_CONFIG, "scope": {"instruction": "記者の指示", "filters": [item]}}
    with pytest.raises(KizashiError) as exc:
        runner.validate_run_config(config, load_adapter(repo, "cao-procurement"))
    assert exc.value.code == "E_SCHEMA"


# ---------------------------------------------------------------------------
# ④：構文の壊れた dataset.yaml
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["check", "L0001", "--run", "{run}"],
        ["render", "L0001", "--run", "{run}"],
        ["render", "{run}"],
        ["record", "{run}", "inbox/k.json"],
    ],
)
def test_hs2_broken_dataset_yaml_is_rule_version(
    repo: Path, synthetic_run: str, capsys, argv
) -> None:
    """HS-2・CLI-1（D-095④）：dataset.yaml を YAML として読めなければ、規則の版を照らせないので E_RULE_VERSION
    （終了コード3）。提案は inbox/ に残る。"""
    put(repo, "k.json", calc_sum("翻訳業務", 2024))
    path = repo / "datasets" / "cao-procurement" / "dataset.yaml"
    path.write_text("parser_version: [\n", encoding="utf-8")
    capsys.readouterr()
    code = cli.main([a.replace("{run}", synthetic_run) for a in argv])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["error"]["code"] == "E_RULE_VERSION"
    assert (repo / "inbox" / "k.json").is_file()


# ---------------------------------------------------------------------------
# ⑤：inspect の取材質問
# ---------------------------------------------------------------------------


def test_kn2_inspect_questions_are_excerpted_and_paged(repo: Path, synthetic_run: str) -> None:
    """CLI-1・KN-2（D-095⑤）：3,000字の質問21件は、各欄を2,000字で切って示し、1ページ20件で next_page を返す。"""
    long = "公表一覧の記載を確かめる。" + "あ" * 2990
    for i in range(21):
        rec(
            repo,
            synthetic_run,
            {
                "kind": "question",
                "lead_id": "L0001",
                "to": "内閣府の会計担当",
                "question": long,
                "documents": "契約書",
                "decision_impact": "記載が違えば記録する。",
                "purposes": ["verify_record"],
            },
            name=f"q{i}.json",
        )
    first = inspect_cmd.inspect(repo, "L0001", synthetic_run, page=1)
    assert len(first["questions"]) == 20
    assert first["next_page"] == 2
    question = first["questions"][0]["question"]
    assert question.startswith("公表一覧の記載を確かめる。")
    assert question.endswith(f"…（以下略。全{len(long)}字）")
    assert len(question) == 2000 + len(f"…（以下略。全{len(long)}字）")
    assert first["questions"][0]["purposes"] == ["verify_record"]
    second = inspect_cmd.inspect(repo, "L0001", synthetic_run, page=2)
    assert len(second["questions"]) == 1
    assert "next_page" not in second
