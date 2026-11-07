"""bf1bede の実装レビューの修正のうち、CLI・ID・accepted/・規則の版の回帰テスト（D-090・D-091）。

test_plan.md の CLI-1・CLI-2・IN-2・HS-2 の回帰。番号は decisions.md の「bf1bede の実装レビューの記録」の
表（1-n）と、決定の D-090・D-091。期待値は implementation_spec.md の 3.2・3.3・3.7・4.5 から書き写した。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import calc_sum, ledger_of, put
from kizashi import cli, inbox
from kizashi.errors import KizashiError

RUN = "20261105-101500-a3f2"


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = cli.main(argv)
    out, _ = capsys.readouterr()
    assert out.count("\n") == 1, f"標準出力が1行のJSONでない: {out!r}"
    return code, json.loads(out)


# ---------------------------------------------------------------------------
# 1-4（CLI-2）：ID の書式の数字は ASCII の 0〜9 だけ
# ---------------------------------------------------------------------------

NON_ASCII_DIGIT_IDS = [
    ["inspect", "L０００１", "--run", RUN],  # 全角の数字
    ["inspect", "S٠٠١", "--run", RUN],  # アラビア・インド数字
    ["inspect", "S004:００００４２", "--run", RUN],
    ["check", "L０００１", "--run", RUN],
    ["render", "L０００１", "--run", RUN],
    ["inspect", "L0001", "--run", "２０２６1105-101500-a3f2"],
    ["run", "--resume", "20261105-１01500-a3f2"],
    ["fetch", RUN, "--recheck", "S００4"],
]


@pytest.mark.parametrize("argv", NON_ASCII_DIGIT_IDS)
def test_cli2_non_ascii_digits_are_bad_id(repo: Path, capsys, argv) -> None:
    """CLI-2（1-4）：全角などの数字を含む ID は書式に合わない。E_BAD_ID（終了コード2）で、台帳を引かない。"""
    code, payload = run_cli(argv, capsys)
    assert code == 2
    assert payload["error"]["code"] == "E_BAD_ID"


def test_cli2_proposal_ids_with_non_ascii_digits_are_schema_errors(
    repo: Path, synthetic_run: str
) -> None:
    """CLI-2（1-4）：提案の中の ID も同じ書式で照らす。全角の数字の候補ID は書式の誤り（E_SCHEMA）で、
    存在しない参照（E_REF_NOT_FOUND）にしない。"""
    from kizashi import record

    arg = put(
        repo,
        "h.json",
        {"kind": "hypothesis", "lead_id": "L０００１", "text": "仮説", "withdraw_if": "条件"},
    )
    with pytest.raises(KizashiError) as exc:
        record.record(repo, synthetic_run, arg)
    assert exc.value.code == "E_SCHEMA"


# ---------------------------------------------------------------------------
# 1-17（CLI-1）：成功の出力を JSON にできなければ、1つの E_INTERNAL の JSON と終了コード5
# ---------------------------------------------------------------------------


def test_cli1_unserializable_result_is_one_internal_json(
    repo: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-1（1-17）：操作の戻り値に JSON にできない値があっても、標準出力は1つの JSON（E_INTERNAL）で、
    終了コードは5（3.2）。"""
    monkeypatch.setitem(cli.HANDLERS, "inspect", lambda args: {"value": object()})
    code, payload = run_cli(["inspect", RUN], capsys)
    assert code == 5
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_INTERNAL"


def test_cli1_unserializable_warning_is_one_internal_json(
    repo: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-1（1-17）：警告の側に JSON にできない値があっても同じ。"""
    monkeypatch.setitem(
        cli.HANDLERS, "inspect", lambda args: {"value": 1, "warnings": [{"code": object()}]}
    )
    code, payload = run_cli(["inspect", RUN], capsys)
    assert code == 5
    assert payload["error"]["code"] == "E_INTERNAL"


# ---------------------------------------------------------------------------
# 1-15（IN-2）：accepted/ の連番は桁を限らずに読み、既存のファイルを上書きしない
# ---------------------------------------------------------------------------


def _accepted(repo: Path, run_id: str) -> Path:
    path = repo / "data" / "runs" / run_id / "accepted"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_in2_serial_beyond_four_digits(repo: Path) -> None:
    """IN-2（1-15）：5桁の連番（10000-…）の次は 10001。4桁だけを読むと次も 10000 になり上書きする。"""
    directory = repo / "data" / "runs" / RUN / "accepted"
    directory.mkdir(parents=True)
    (directory / "9999-claim.json").write_text("{}", encoding="utf-8")
    (directory / "10000-claim.json").write_text("{}", encoding="utf-8")
    assert inbox.next_serial(directory) == 10001


def test_in2_copy_to_accepted_never_overwrites(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """IN-2（1-15）：同じ名前のファイルが accepted/ にあれば、上書きせずに止める。元の提案と既存のファイルは
    そのまま残る。"""
    directory = _accepted(repo, RUN)
    existing = directory / "0007-claim.json"
    existing.write_text('{"old": true}', encoding="utf-8")
    proposal = repo / "inbox" / "c.json"
    proposal.write_text('{"kind": "claim"}', encoding="utf-8")
    monkeypatch.setattr(inbox, "next_serial", lambda d: 7)
    with pytest.raises(KizashiError) as exc:
        inbox.copy_to_accepted(repo, RUN, proposal, "claim")
    assert exc.value.code == "E_INTERNAL"
    assert existing.read_text(encoding="utf-8") == '{"old": true}'
    assert proposal.read_text(encoding="utf-8") == '{"kind": "claim"}'


def test_in2_record_keeps_proposal_when_accepted_name_taken(
    repo: Path, synthetic_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IN-2（1-15）：record で accepted/ の名前がぶつかれば登録しない（台帳を変えず、提案は inbox/ に残る）。"""
    from kizashi import record

    directory = _accepted(repo, synthetic_run)
    (directory / "0042-calc.json").write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(inbox, "next_serial", lambda d: 42)
    arg = put(repo, "k.json", calc_sum("翻訳業務", 2024))
    conn = ledger_of(repo, synthetic_run)
    before = conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0]
    conn.close()
    with pytest.raises(KizashiError):
        record.record(repo, synthetic_run, arg)
    conn = ledger_of(repo, synthetic_run)
    assert conn.execute("SELECT COUNT(*) FROM computations").fetchone()[0] == before
    conn.close()
    assert (repo / arg).is_file()
    assert (directory / "0042-calc.json").read_text(encoding="utf-8") == '{"old": true}'


# ---------------------------------------------------------------------------
# D-091①（1-6。HS-2）：dataset.yaml がなければ、check・record・render は E_RULE_VERSION（終了コード3）
# ---------------------------------------------------------------------------


def _remove_dataset_yaml(repo: Path) -> None:
    (repo / "datasets" / "cao-procurement" / "dataset.yaml").unlink()


@pytest.mark.parametrize(
    "argv",
    [
        ["check", "L0001", "--run", "{run}"],
        ["render", "L0001", "--run", "{run}"],
        ["record", "{run}", "inbox/k.json"],
    ],
)
def test_hs2_missing_dataset_yaml_is_rule_version(
    repo: Path, synthetic_run: str, capsys, argv
) -> None:
    """HS-2（D-091①）：規則の版を照らせないので E_RULE_VERSION。ファイルがないことと、元に戻すことを書く。
    想定外のエラー（E_INTERNAL・終了コード5）にしない。提案は inbox/ に残る。"""
    put(repo, "k.json", calc_sum("翻訳業務", 2024))
    capsys.readouterr()
    _remove_dataset_yaml(repo)
    code, payload = run_cli([a.replace("{run}", synthetic_run) for a in argv], capsys)
    assert code == 3
    assert payload["error"]["code"] == "E_RULE_VERSION"
    assert "dataset.yaml" in payload["error"]["message"]
    assert "ない" in payload["error"]["message"]
    assert (repo / "inbox" / "k.json").is_file()


# ---------------------------------------------------------------------------
# D-090⑨（IN-2・HS-2）：record は提案の形より先に規則の版を照らす
# ---------------------------------------------------------------------------


def _drift_rules_version(repo: Path, run_id: str) -> None:
    conn = sqlite3.connect(repo / "data" / "runs" / run_id / "ledger.sqlite")
    (recorded,) = conn.execute("SELECT rule_versions_json FROM runs").fetchone()
    versions = json.loads(recorded)
    versions["rules_version"] = "0"
    conn.execute("UPDATE runs SET rule_versions_json = ?", (json.dumps(versions),))
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "no_such_kind"},  # E_UNKNOWN_KIND の形
        {"kind": "calc", "op": "sum", "state": "checked"},  # E_SELF_DECLARED の形
        {"kind": "claim", "unknown_field": 1},  # E_SCHEMA の形
    ],
)
def test_in2_rule_version_before_proposal_shape(repo: Path, synthetic_run: str, data: dict) -> None:
    """IN-2・HS-2（D-090⑨）：版の違う台帳への record は、提案の形の誤りより先に E_RULE_VERSION。台帳と
    accepted/ を変えず、提案は inbox/ に残る。"""
    from kizashi import record

    _drift_rules_version(repo, synthetic_run)
    arg = put(repo, "x.json", data)
    conn = ledger_of(repo, synthetic_run)
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    accepted = repo / "data" / "runs" / synthetic_run / "accepted"
    before = sorted(p.name for p in accepted.iterdir())
    with pytest.raises(KizashiError) as exc:
        record.record(repo, synthetic_run, arg)
    assert exc.value.code == "E_RULE_VERSION"
    assert exc.value.exit_code == 3
    conn = ledger_of(repo, synthetic_run)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events
    conn.close()
    assert (repo / arg).is_file()
    assert sorted(p.name for p in accepted.iterdir()) == before


def test_in2_rule_version_before_broken_json(repo: Path, synthetic_run: str) -> None:
    """IN-2・HS-2（D-090⑨）：JSON として読めない提案でも、版が違えば E_RULE_VERSION を先に返す。"""
    from kizashi import record

    _drift_rules_version(repo, synthetic_run)
    (repo / "inbox" / "b.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(KizashiError) as exc:
        record.record(repo, synthetic_run, "inbox/b.json")
    assert exc.value.code == "E_RULE_VERSION"
