"""提案ファイルの読み込みと accepted/ への移動（test_plan.md の IN-1。implementation_spec.md 3.4・4.5・6.1）。

場所・拡張子・大きさ・シンボリックリンクの検査は paths.resolve_inbox_file（tests/test_paths.py）。
ここでは、inbox.load_proposal がその検査を通し、JSON の形を確かめることと、accept の連番を確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kizashi import inbox
from kizashi.errors import KizashiError

RUN = "20261105-101500-a3f2"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "inbox").mkdir()
    (tmp_path / "data" / "runs" / RUN).mkdir(parents=True)
    return tmp_path


def write(root: Path, name: str, content: str | bytes) -> str:
    path = root / "inbox" / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return f"inbox/{name}"


def test_in1_load_proposal_reads_json_object(root: Path) -> None:
    """IN-1：inbox/ 直下の JSON を読み、最上位の kind を返す。"""
    arg = write(root, "a.json", json.dumps({"kind": "question", "lead_id": "L0001"}))
    proposal = inbox.load_proposal(root, arg)
    assert proposal.kind == "question"
    assert proposal.data == {"kind": "question", "lead_id": "L0001"}
    assert proposal.path == root / "inbox" / "a.json"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("{not json", "E_SCHEMA"),
        ("[1, 2]", "E_SCHEMA"),
        ('{"lead_id": "L0001"}', "E_SCHEMA"),
        ('{"kind": 3}', "E_SCHEMA"),
        ('{"kind": "question", "kind": "claim"}', "E_SCHEMA"),
        ('{"kind": "question", "x": NaN}', "E_SCHEMA"),
        ("\ufeff" + '{"kind": "question"}', "E_SCHEMA"),
        ('{"kind": "question"}'.encode("cp932") + "　".encode("cp932"), "E_SCHEMA"),
    ],
)
def test_in1_bad_json_is_e_schema(root: Path, content: str | bytes, code: str) -> None:
    """IN-1：JSON として読めない、オブジェクトでない、kind がない・文字列でない、キーの重複、NaN、
    UTF-8 でない（BOM を含む）ものは E_SCHEMA。ファイルは inbox/ に残る。"""
    arg = write(root, "bad.json", content)
    with pytest.raises(KizashiError) as info:
        inbox.load_proposal(root, arg)
    assert info.value.code == code
    assert (root / "inbox" / "bad.json").exists()


def test_in1_load_proposal_checks_path(root: Path) -> None:
    """IN-1：場所・拡張子の検査を通す（ファイルがない、.sql を JSON として読まない）。"""
    with pytest.raises(KizashiError) as info:
        inbox.load_proposal(root, "inbox/none.json")
    assert info.value.code == "E_INBOX_PATH"
    write(root, "q.sql", "SELECT 1")
    with pytest.raises(KizashiError) as info:
        inbox.load_proposal(root, "inbox/q.sql")
    assert info.value.code == "E_INBOX_PATH"


def test_in2_accept_moves_with_serial_number(root: Path) -> None:
    """IN-2：成功した提案を accepted/<連番>-<kind>.json に写して inbox/ から消す。連番は続きから。"""
    accepted = root / "data" / "runs" / RUN / "accepted"
    accepted.mkdir()
    (accepted / "0001-run_config.json").write_text("{}", encoding="utf-8")
    (accepted / "q-001.sql").write_text("SELECT 1", encoding="utf-8")
    arg = write(root, "c.json", '{"kind": "claim"}')
    target = inbox.accept(root, RUN, root / "inbox" / "c.json", "claim")
    assert target == accepted / "0002-claim.json"
    assert target.read_text(encoding="utf-8") == '{"kind": "claim"}'
    assert not (root / arg).exists()
