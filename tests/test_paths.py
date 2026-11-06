"""パスの検査のテスト（test_plan.md の IN-1 の一部：パスの検査。implementation_spec.md 3.4）。

inbox/ の外、サブフォルダー、.json・.sql 以外、1MB超、シンボリックリンクを E_INBOX_PATH で拒否する。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kizashi import paths
from kizashi.errors import KizashiError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "ok.json").write_text("{}", encoding="utf-8")
    (tmp_path / "inbox" / "q.sql").write_text("SELECT 1", encoding="utf-8")
    return tmp_path


def test_in1_accepts_direct_children(root: Path) -> None:
    """IN-1：inbox/ 直下の .json と .sql は読める。"""
    assert paths.resolve_inbox_file(root, "inbox/ok.json") == root / "inbox" / "ok.json"
    assert paths.resolve_inbox_file(root, "./inbox/q.sql", (".sql",)) == root / "inbox" / "q.sql"


@pytest.mark.parametrize(
    "arg",
    [
        "ok.json",  # inbox/ の外
        "data/ok.json",
        "inbox/../inbox/ok.json",
        "../inbox/ok.json",
        "inbox/sub/ok.json",  # サブフォルダー
        "/tmp/ok.json",  # 絶対パス
        "inbox/ok.txt",  # 拡張子
        "inbox/ok.json.bak",
        "inbox/.json",
        "inbox\\ok.json",
        "inbox/missing.json",  # ない
    ],
)
def test_in1_rejects_bad_paths(root: Path, arg: str) -> None:
    """IN-1：inbox/ の外、..、サブフォルダー、絶対パス、拡張子の違い、ないファイルは E_INBOX_PATH。"""
    (root / "inbox" / "sub").mkdir(exist_ok=True)
    (root / "inbox" / "sub" / "ok.json").write_text("{}", encoding="utf-8")
    (root / "inbox" / "ok.txt").write_text("{}", encoding="utf-8")
    with pytest.raises(KizashiError) as info:
        paths.resolve_inbox_file(root, arg)
    assert info.value.code == "E_INBOX_PATH"


def test_in1_rejects_wrong_kind_for_operation(root: Path) -> None:
    """IN-1：操作ごとの拡張子（run の --config は .json だけ）。"""
    with pytest.raises(KizashiError) as info:
        paths.resolve_inbox_file(root, "inbox/q.sql", (".json",))
    assert info.value.code == "E_INBOX_PATH"


def test_in1_rejects_large_file(root: Path) -> None:
    """IN-1：1MBを超えるファイルは読まない（ちょうど1MBは読める）。"""
    (root / "inbox" / "edge.json").write_bytes(b" " * paths.INBOX_MAX_BYTES)
    paths.resolve_inbox_file(root, "inbox/edge.json")
    (root / "inbox" / "big.json").write_bytes(b" " * (paths.INBOX_MAX_BYTES + 1))
    with pytest.raises(KizashiError) as info:
        paths.resolve_inbox_file(root, "inbox/big.json")
    assert info.value.code == "E_INBOX_PATH"


def test_in1_rejects_symlinks(root: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    """IN-1：ファイルのシンボリックリンクと、inbox/ そのもののシンボリックリンクを拒否する。"""
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.json").write_text("{}", encoding="utf-8")
    os.symlink(outside / "secret.json", root / "inbox" / "link.json")
    with pytest.raises(KizashiError) as info:
        paths.resolve_inbox_file(root, "inbox/link.json")
    assert info.value.code == "E_INBOX_PATH"

    other = tmp_path_factory.mktemp("other")
    os.symlink(outside, other / "inbox")
    with pytest.raises(KizashiError) as info:
        paths.resolve_inbox_file(other, "inbox/secret.json")
    assert info.value.code == "E_INBOX_PATH"


def test_writes_are_limited_to_data(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """3.4：CLI の書き込み先は、実際のパスを解決したうえで data/ の中に限る。"""
    (tmp_path / "data" / "runs").mkdir(parents=True)
    assert paths.ensure_in_data(tmp_path, tmp_path / "data" / "runs")
    with pytest.raises(KizashiError):
        paths.ensure_in_data(tmp_path, tmp_path / "inbox")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    os.symlink(elsewhere, tmp_path / "data" / "escape")
    with pytest.raises(KizashiError):
        paths.ensure_in_data(tmp_path, tmp_path / "data" / "escape" / "x")


@pytest.mark.parametrize("name", ["架空太郎.sql", "設定.json", "ｑ1.sql", "q 1.sql", "q+1.json"])
def test_in1_inbox_names_are_ascii_letters_digits_and_marks(root: Path, name: str) -> None:
    """IN-1（D-067）：inbox/ のファイル名は英数字と `-`・`_`・`.` だけ（先頭は英数字）。ほかの文字（日本語・全角・
    空白など）は E_INBOX_PATH。英数字以外の文字を含む名前は不合格の文に書かない（氏名がファイル名に入って保存・表示される
    経路をなくす。氏名の一覧は日本語なので、英数字の名前には入らない）。"""
    suffix = Path(name).suffix
    with pytest.raises(KizashiError) as info:
        paths.check_inbox_arg(f"inbox/{name}", (suffix,))
    assert info.value.code == "E_INBOX_PATH"
    if (
        not name.isascii()
    ):  # 英数字以外の文字を含む名前は表示しない（英数字だけの名前には氏名が入らない）
        assert Path(name).stem not in info.value.message


@pytest.mark.parametrize("name", ["q-001.sql", "run-config.json", "t1.json", "a_b.c.json"])
def test_in1_inbox_ascii_names_pass(root: Path, name: str) -> None:
    """IN-1（D-067）：英数字と `-`・`_`・`.` の名前は通る。"""
    assert paths.check_inbox_arg(f"inbox/{name}", (Path(name).suffix,)) == name
