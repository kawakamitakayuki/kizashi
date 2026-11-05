"""CLI の共通仕様のテスト（test_plan.md の CLI-1・CLI-2・CLI-3）。

期待値は implementation_spec.md の 3.1（作業フォルダーと SQLite の版）、3.2（出力と終了コード）、
3.7（エラーコードの一覧）、9章（検証のコード）、4.1〜4.7（各操作の引数の形）から書き写したもの。
実装の出力を期待値にしていない。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from kizashi import cli, errors

ERROR_KEYS = {"code", "stage", "message", "affected_ids", "retryable", "next_action"}


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """リポジトリの直下と見なされる最小のフォルダー（pyproject.toml と datasets/ がある）。"""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fake"\n', encoding="utf-8")
    (tmp_path / "datasets").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def new_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI-1 の検査が、実行環境の SQLite の版に左右されないようにする。"""
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.45.1")


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict, str]:
    """main を呼び、終了コード・標準出力のJSON・標準エラー出力を返す。

    標準出力が1つのJSONでなければ json.loads が失敗する（余分な出力があれば Extra data）。
    """
    code = cli.main(argv)
    out, err = capsys.readouterr()
    assert out.endswith("\n")
    assert out.count("\n") == 1, f"標準出力が1行のJSONでない: {out!r}"
    return code, json.loads(out), err


def assert_error(payload: dict, code: str) -> None:
    assert payload["ok"] is False
    assert set(payload["error"]) == ERROR_KEYS
    assert payload["error"]["code"] == code
    assert isinstance(payload["error"]["affected_ids"], list)
    assert isinstance(payload["error"]["retryable"], bool)
    assert payload["error"]["message"]


# ---------------------------------------------------------------------------
# CLI-1：リポジトリ直下以外で E_CWD。出力は1つのJSON。終了コードが仕様どおり
# ---------------------------------------------------------------------------


def test_cli1_outside_repo_is_e_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, new_sqlite
) -> None:
    """CLI-1：pyproject.toml も datasets/ もないフォルダーでは E_CWD（終了コード3）。"""
    monkeypatch.chdir(tmp_path)
    code, payload, _ = run_cli(["inspect", "20261105-000000-test"], capsys)
    assert code == 3
    assert_error(payload, "E_CWD")


def test_cli1_subfolder_of_repo_is_e_cwd(repo_root: Path, monkeypatch, capsys, new_sqlite) -> None:
    """CLI-1：リポジトリの中でも、直下でなければ E_CWD（例：src/ や inbox/ の中）。"""
    sub = repo_root / "inbox"
    sub.mkdir()
    monkeypatch.chdir(sub)
    code, payload, _ = run_cli(["inspect", "20261105-000000-test"], capsys)
    assert code == 3
    assert_error(payload, "E_CWD")


@pytest.mark.parametrize("missing", ["pyproject.toml", "datasets"])
def test_cli1_both_markers_required(
    tmp_path: Path, monkeypatch, capsys, new_sqlite, missing: str
) -> None:
    """CLI-1：pyproject.toml と datasets/ の片方だけでは直下と見なさない。"""
    if missing != "pyproject.toml":
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    if missing != "datasets":
        (tmp_path / "datasets").mkdir()
    monkeypatch.chdir(tmp_path)
    code, payload, _ = run_cli(["inspect", "20261105-000000-test"], capsys)
    assert code == 3
    assert_error(payload, "E_CWD")


def test_cli1_e_cwd_before_argument_errors(tmp_path: Path, monkeypatch, capsys, new_sqlite) -> None:
    """CLI-1：起動時の検査が先。リポジトリの外では、引数が誤っていても E_CWD を返す。"""
    monkeypatch.chdir(tmp_path)
    code, payload, _ = run_cli(["no-such-operation"], capsys)
    assert code == 3
    assert_error(payload, "E_CWD")


# 4.1〜4.7 の書式どおりの呼び出し。仮の実装は {"ok": true, "data": {"stub": "<操作名>"}} を返す。
# run <分野> --config は P0-2 で本実装にした（test_cli2_run_is_implemented と test_run.py）。
# fetch（--recheck を除く）・inspect・query・record は P0-4、run --resume は P0-5 で本実装にした
# （IMPLEMENTED_FORMS）。
VALID_FORMS = [
    ("fetch", ["fetch", "20261105-101500-a3f2", "--recheck", "S004"]),
    ("check", ["check", "L0001", "--run", "20261105-101500-a3f2"]),
    (
        "check",
        ["check", "--publish", "examples/publish.manifest", "--run", "20261105-101500-a3f2"],
    ),
    (
        "check",
        [
            "check",
            "--publish",
            "examples/publish.manifest",
            "--run",
            "20261105-101500-a3f2",
            "--run",
            "20261106-090000-b7c1",
        ],
    ),
    ("render", ["render", "L0001", "--run", "20261105-101500-a3f2"]),
    ("render", ["render", "L0001", "--run", "20261105-101500-a3f2", "--public"]),
    ("render", ["render", "20261105-101500-a3f2"]),
    ("render", ["render", "20261105-101500-a3f2", "--public"]),
]


@pytest.mark.parametrize(("operation", "argv"), VALID_FORMS)
def test_cli1_stub_accepts_documented_forms(
    repo_root: Path, capsys, new_sqlite, operation: str, argv: list[str]
) -> None:
    """CLI-1：リポジトリ直下では、仕様の書式の呼び出しが1つのJSONと終了コード0を返す（仮の実装）。"""
    code, payload, _ = run_cli(argv, capsys)
    assert code == 0
    assert payload == {"ok": True, "data": {"stub": operation}}


# P0-4・P0-5 で本実装にした操作の書式。最小のフォルダー（台帳も inbox/ もない）では、引数の誤り（E_USAGE・
# E_BAD_ID）でなく、操作の中の型付きのエラー（台帳がない、提案ファイルがない）を返す。
IMPLEMENTED_FORMS = [
    ["run", "--resume", "20261105-101500-a3f2"],
    ["fetch", "20261105-101500-a3f2", "https://www.cao.go.jp/x.pdf"],
    ["fetch", "20261105-101500-a3f2", "https://www.cao.go.jp/", "--index"],
    ["fetch", "20261105-101500-a3f2", "https://www.cao.go.jp/y.html", "--definition"],
    ["inspect", "20261105-000000-test"],
    ["inspect", "20261105-000000-test", "--page", "2"],
    ["inspect", "L0001", "--run", "20261105-000000-test"],
    ["inspect", "S004:000042", "--run", "20261105-000000-test", "--page", "3"],
    ["query", "20261105-101500-a3f2", "inbox/q-001.sql"],
    ["record", "20261105-101500-a3f2", "inbox/calc-001.json"],
]


@pytest.mark.parametrize("argv", IMPLEMENTED_FORMS)
def test_cli1_implemented_forms_reach_the_operation(
    repo_root: Path, capsys, new_sqlite, argv: list[str]
) -> None:
    """CLI-1：本実装の操作も、書式どおりなら引数の検査を通り、操作の型付きのエラーを1つのJSONで返す。"""
    code, payload, _ = run_cli(argv, capsys)
    assert code == 1
    assert payload["error"]["code"] in ("E_REF_NOT_FOUND", "E_INBOX_PATH")


INVALID_FORMS = [
    [],  # 操作がない
    ["no-such-operation"],
    ["inspect"],  # ID がない
    ["inspect", "L0001", "--page", "0"],  # ページは1から
    ["inspect", "L0001", "--page", "x"],
    ["inspect", "--help"],  # 説明を出力すると1つのJSONでなくなるため、引数の誤りとして返す
    ["--help"],
    ["run", "cao-procurement"],  # --config がない
    ["run", "--config", "inbox/run-config.json"],  # 分野がない
    ["run", "cao-procurement", "--config", "inbox/a.json", "--resume", "20261105-101500-a3f2"],
    ["run", "--res", "20261105-101500-a3f2"],  # 省略形を受け付けない
    ["fetch", "20261105-101500-a3f2"],  # URL も --recheck もない
    ["fetch", "20261105-101500-a3f2", "https://www.cao.go.jp/", "--recheck", "S004"],
    ["fetch", "20261105-101500-a3f2", "--recheck", "S004", "--index"],
    ["fetch", "20261105-101500-a3f2", "https://www.cao.go.jp/", "--index", "--definition"],
    ["query", "20261105-101500-a3f2"],  # SQLファイルがない
    ["record", "20261105-101500-a3f2"],  # JSONファイルがない
    ["record", "20261105-101500-a3f2", "inbox/a.json", "inbox/b.json"],
    ["check", "--run", "20261105-101500-a3f2"],  # 候補も --publish もない
    ["check", "L0001", "--publish", "examples/publish.manifest", "--run", "20261105-101500-a3f2"],
    ["check", "L0001", "--run", "20261105-101500-a3f2", "--run", "20261106-090000-b7c1"],
    ["render"],
    ["render", "L0001", "--run", "20261105-101500-a3f2", "extra"],
]


@pytest.mark.parametrize("argv", INVALID_FORMS)
def test_cli1_bad_arguments_are_e_usage(repo_root: Path, capsys, new_sqlite, argv) -> None:
    """CLI-1：引数の誤りは E_USAGE（終了コード2）で、出力は1つのJSON。"""
    code, payload, _ = run_cli(argv, capsys)
    assert code == 2
    assert_error(payload, "E_USAGE")


def test_cli1_unexpected_error_is_e_internal(repo_root: Path, monkeypatch, capsys, new_sqlite):
    """CLI-1：想定外の例外は E_INTERNAL（終了コード5）。スタックトレースは標準エラー出力だけに出す。"""

    def boom(args):
        raise RuntimeError("secret-detail-in-trace")

    monkeypatch.setitem(cli.HANDLERS, "inspect", boom)
    code, payload, err = run_cli(["inspect", "20261105-000000-test"], capsys)
    assert code == 5
    assert_error(payload, "E_INTERNAL")
    assert "Traceback" in err
    assert "Traceback" not in json.dumps(payload, ensure_ascii=False)


def test_cli1_typed_error_from_operation(repo_root: Path, monkeypatch, capsys, new_sqlite):
    """CLI-1：操作が投げた型付きのエラーは、そのコードと終了コードで返る（例：E_FETCH_FAILED は4）。"""

    def fail(args):
        raise errors.KizashiError(
            "E_FETCH_FAILED",
            "fetch",
            "通信に失敗した",
            affected_ids=["S004"],
            retryable=True,
            next_action="時間をおいて再試行する",
        )

    monkeypatch.setitem(cli.HANDLERS, "fetch", fail)
    code, payload, _ = run_cli(["fetch", "20261105-101500-a3f2", "--recheck", "S004"], capsys)
    assert code == 4
    assert payload == {
        "ok": False,
        "error": {
            "code": "E_FETCH_FAILED",
            "stage": "fetch",
            "message": "通信に失敗した",
            "affected_ids": ["S004"],
            "retryable": True,
            "next_action": "時間をおいて再試行する",
        },
    }


# implementation_spec.md 3.7 の表を、そのまま書き写した期待値。
SPEC_3_7_EXIT_CODES = {
    "E_CWD": 3,
    "E_BAD_ID": 2,
    "E_USAGE": 2,
    "E_INBOX_PATH": 1,
    "E_SCHEMA": 1,
    "E_UNKNOWN_KIND": 1,
    "E_SELF_DECLARED": 1,
    "E_REF_NOT_FOUND": 1,
    "E_QUOTE_NOT_FOUND": 1,
    "E_QUOTE_TRUNCATED": 1,
    "E_QUOTE_YEAR": 1,
    "E_BAD_EVIDENCE_SOURCE": 1,
    "E_DOC_VALUE_MISMATCH": 1,
    "E_BAD_EXPR": 1,
    "E_DIV_ZERO": 1,
    "E_NOT_ALLOWED_COLUMN": 1,
    "E_V7": 1,
    "E_LIMIT_LEADS": 1,
    "E_LIMIT_EVIDENCE": 1,
    "E_LIMIT_HTTP": 1,
    "E_LIMIT_BYTES": 1,
    "E_ROUNDS_EXCEEDED": 1,
    "E_HOST_NOT_ALLOWED": 1,
    "E_REDIRECT_NOT_ALLOWED": 1,
    "E_FETCH_FAILED": 4,
    "E_FETCH_INVALID": 1,
    "E_FORMAT_CHANGED": 3,
    "E_QUERY_REJECTED": 1,
    "E_QUERY_TIMEOUT": 1,
    "E_NOT_RESUMABLE": 3,
    "E_INPUT_CHANGED": 3,
    "E_RULE_VERSION": 3,
    "E_DENYLIST_MISSING": 3,
    "E_INTERNAL": 5,
}

# implementation_spec.md 9章の検証のコード。検査の不合格は終了コード1（3.2）。
SPEC_9_CHECK_CODES = [
    "V1_INPUT_EMPTY",
    "V1_QUALITY_NOT_RUN",
    "V1_RUN_FAILED",
    "V2_REF_MISSING",
    "V2_HASH_MISMATCH",
    "V2_STALE_OR_WITHDRAWN",
    "V2_BAD_EVIDENCE_SOURCE",
    "V3A_RECALC_MISMATCH",
    "V3B_REAGG_MISMATCH",
    "V3B_HEADER_UNRESOLVED",
    "V3C_DERIVE_MISMATCH",
    "V3_UNCITED_NUMBER",
    "V3_NO_MEANING",
    "V4_QUOTE_NOT_FOUND",
    "V4_QUOTE_TRUNCATED",
    "V4_QUOTE_YEAR",
    "V5_CONDITION_MISSING",
    "V5_UNRESOLVED_COMPARISON",
    "V5_ADJUSTED_NO_VALUES",
    "V5_BAD_RATE",
    "V5_PARTIAL_YEAR",
    "V6_TYPE_MISSING",
    "V6_NO_EVIDENCE",
    "V6_SELF_EVIDENCE",
    "V6_NO_SEARCH_SCOPE",
    "V7_RAW_NUMBER",
    "V7_PERSONAL_NAME",
    "V8_REQUIRED_MISSING",
    "V8_MATCH_UNCONFIRMED",
    "V8_COEFFICIENT_PENDING",
    "V8_CHANGE_NOT_NEGATED",
    "V8_ARTIFACT_NO_FACTOR",
    "V8_EXPLANATION_STATUS",
    "V8_NO_WITHDRAW_RESULT",
    "V8_HOLD_REASON",
    "V8_T6C_QUESTION",
]


def test_cli1_exit_codes_match_spec() -> None:
    """CLI-1：エラーコードと終了コードの対応が 3.7 の表と9章のとおり。表にないコードはない。"""
    expected = dict(SPEC_3_7_EXIT_CODES)
    expected.update({c: 1 for c in SPEC_9_CHECK_CODES})
    assert errors.EXIT_CODES == expected


def test_cli1_unknown_error_code_is_rejected() -> None:
    """CLI-1：表にないコード（書き損じ）でエラーを作れない。警告のコードもエラーにならない。"""
    with pytest.raises(ValueError):
        errors.KizashiError("E_TYPO", "cli", "x")
    with pytest.raises(ValueError):
        errors.KizashiError("W7_STRONG_WORD", "check", "x")


def test_cli1_console_script(tmp_path: Path) -> None:
    """CLI-1：導入した kizashi コマンドの終了コードが main の値と同じ（E_CWD は3、成功は0）。"""
    exe = Path(sys.executable).with_name("kizashi")
    if not exe.exists():
        pytest.skip("kizashi コマンドが導入されていない（uv sync の後に実行する）")

    outside = tmp_path / "outside"
    outside.mkdir()
    proc = subprocess.run(
        [str(exe), "inspect", "20261105-000000-test"],
        cwd=outside,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["error"]["code"] == "E_CWD"

    if tuple(int(p) for p in sqlite3.sqlite_version.split(".")) < (3, 44):
        pytest.skip("実行環境の SQLite が3.44未満のため、成功の形は確かめない")
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "pyproject.toml").write_text("", encoding="utf-8")
    (fake_repo / "datasets").mkdir()
    proc = subprocess.run(
        [str(exe), "render", "L0001", "--run", "20261105-000000-test"],
        cwd=fake_repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"ok": True, "data": {"stub": "render"}}


# ---------------------------------------------------------------------------
# CLI-3：SQLite の版が3.44未満なら E_USAGE で止まる
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["3.43.2", "3.31.1", "3.9.0", "2.99.99"])
def test_cli3_old_sqlite_is_e_usage(repo_root: Path, monkeypatch, capsys, version: str) -> None:
    """CLI-3：3.44未満は E_USAGE（終了コード2）。3.9.0 のように、文字列の比較では見逃す版も止める。"""
    monkeypatch.setattr(sqlite3, "sqlite_version", version)
    code, payload, _ = run_cli(["inspect", "20261105-000000-test"], capsys)
    assert code == 2
    assert_error(payload, "E_USAGE")
    assert version in payload["error"]["message"]
    assert "3.44" in payload["error"]["message"]


@pytest.mark.parametrize("version", ["3.44.0", "3.45.1", "3.100.0", "4.0.0"])
def test_cli3_new_sqlite_passes(repo_root: Path, monkeypatch, capsys, version: str) -> None:
    """CLI-3：3.44以上は止めない。3.100.0 のように、文字列の比較では誤って止める版も通す。"""
    monkeypatch.setattr(sqlite3, "sqlite_version", version)
    code, payload, _ = run_cli(["render", "20261105-000000-test"], capsys)
    assert code == 0
    assert payload == {"ok": True, "data": {"stub": "render"}}


# ---------------------------------------------------------------------------
# CLI-2：書式に合わない ID、../ を含む引数を拒否する。実行ID以外の ID で --run がなければ E_BAD_ID
# ---------------------------------------------------------------------------

RUN = "20261105-101500-a3f2"

BAD_ID_FORMS = [
    ["inspect", "L0001"],  # 実行ID以外で --run がない
    ["inspect", "S004:000042"],
    ["inspect", "K0012", "--page", "2"],
    ["inspect", "../etc/passwd"],
    ["inspect", "L001"],  # 桁数
    ["inspect", "L00001"],
    ["inspect", "S004:42"],
    ["inspect", "l0001", "--run", RUN],  # 小文字
    ["inspect", "T6a", "--run", RUN],
    ["inspect", "L0001", "--run", "../20261105-101500-a3f2"],
    ["inspect", "L0001", "--run", "20261105-101500-A3F2"],
    ["inspect", "20261105-1015-a3f2"],
    ["fetch", "../x", "https://www.cao.go.jp/x.pdf"],
    ["fetch", RUN, "--recheck", "S04"],
    ["fetch", RUN, "--recheck", "../S004"],
    ["query", "L0001", "inbox/q.sql"],
    ["record", "run", "inbox/a.json"],
    ["check", "L0001"],  # --run がない
    ["check", "--publish", "examples/publish.manifest"],
    ["check", "S004", "--run", RUN],
    ["check", "L0001", "--run", "L0001"],
    ["check", "--publish", "examples/publish.manifest", "--run", RUN, "--run", "../x"],
    ["render", "L0001"],
    ["render", "S004", "--run", RUN],
    ["render", "C0001", "--run", RUN],
    ["run", "--resume", "../20261105-101500-a3f2"],
    ["run", "--resume", "L0001"],
]


@pytest.mark.parametrize("argv", BAD_ID_FORMS)
def test_cli2_bad_ids_are_e_bad_id(repo_root: Path, capsys, new_sqlite, argv) -> None:
    """CLI-2：ID の書式（3.3）に合わない引数と、--run の欠けは E_BAD_ID（終了コード2）。"""
    code, payload, _ = run_cli(argv, capsys)
    assert code == 2
    assert_error(payload, "E_BAD_ID")


BAD_INBOX_FORMS = [
    ["run", "cao-procurement", "--config", "inbox/../x.json"],
    ["run", "cao-procurement", "--config", "../inbox/x.json"],
    ["run", "cao-procurement", "--config", "inbox/sub/x.json"],
    ["run", "cao-procurement", "--config", "/tmp/x.json"],
    ["run", "cao-procurement", "--config", "inbox/x.sql"],
    ["query", RUN, "inbox/../q.sql"],
    ["query", RUN, "inbox/q.json"],
    ["record", RUN, "data/a.json"],
    ["record", RUN, "inbox/a.txt"],
]


@pytest.mark.parametrize("argv", BAD_INBOX_FORMS)
def test_cli2_bad_inbox_paths(repo_root: Path, capsys, new_sqlite, argv) -> None:
    """CLI-2・IN-1：../ や inbox/ の外、許していない拡張子の提案ファイルは E_INBOX_PATH（終了コード1）。"""
    code, payload, _ = run_cli(argv, capsys)
    assert code == 1
    assert_error(payload, "E_INBOX_PATH")


@pytest.mark.parametrize(
    "argv",
    [
        ["inspect", "S004", "--run", RUN],
        ["inspect", "S004:000042", "--run", RUN],
        ["inspect", "K0012", "--run", RUN],
        ["inspect", "C0031", "--run", RUN],
        ["inspect", "M002", "--run", RUN],
        ["inspect", RUN, "--page", "2"],
        ["query", RUN, "inbox/q-001.sql"],
        ["record", RUN, "./inbox/calc-001.json"],
    ],
)
def test_cli2_valid_ids_pass(repo_root: Path, capsys, new_sqlite, argv) -> None:
    """CLI-2：3.3 の書式どおりの ID は通る（E_BAD_ID にならず、操作の中で台帳・提案ファイルを探す）。"""
    code, payload, _ = run_cli(argv, capsys)
    assert code != 2
    assert payload["ok"] is True or payload["error"]["code"] in ("E_REF_NOT_FOUND", "E_INBOX_PATH")


def test_cli2_run_is_implemented(repo_root: Path, capsys, new_sqlite) -> None:
    """run <分野> --config は仮の実装でない。datasets/<分野>/dataset.yaml がなければ E_USAGE。"""
    code, payload, _ = run_cli(
        ["run", "cao-procurement", "--config", "inbox/run-config.json"], capsys
    )
    assert code == 2
    assert_error(payload, "E_USAGE")
    code, payload, _ = run_cli(
        ["run", "no-such-domain", "--config", "inbox/run-config.json"], capsys
    )
    assert code == 2
    assert_error(payload, "E_USAGE")


def test_cli_run_outputs_one_json(repo: Path, capsys, new_sqlite, monkeypatch) -> None:
    """run の成功は1つのJSON（終了コード0）。見出しの違うファイルで failed なら E_FORMAT_CHANGED（終了コード3）。"""
    import httpx

    from conftest import URL, Site, fixture_bytes, write_config
    from kizashi import fetcher

    site = Site()
    monkeypatch.setattr(
        fetcher, "make_client", lambda transport=None: httpx.Client(transport=site.transport)
    )
    config = write_config(repo)
    code, payload, _ = run_cli(["run", "cao-procurement", "--config", config], capsys)
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["state"] == "completed"

    site.routes[URL["k24_csv"]] = httpx.Response(200, content=fixture_bytes("kyoso_bad_header.csv"))
    config = write_config(repo)
    code, payload, _ = run_cli(["run", "cao-procurement", "--config", config], capsys)
    assert code == 3
    assert_error(payload, "E_FORMAT_CHANGED")
    assert payload["error"]["stage"] == "run"
    assert (
        payload["error"]["affected_ids"][0] != payload["error"]["affected_ids"][-1]
    )  # 実行IDと資料ID
