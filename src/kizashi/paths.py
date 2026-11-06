"""パス（implementation_spec.md 3.4）。

- data/raw/<sha256>.<拡張子>：原本。上書きしない。
- data/runs/<run_id>/：実行ごとの台帳と書き出し。
- inbox/：Claude が書く提案。CLI は inbox/ 直下の *.json と *.sql だけを読む。
- CLI の書き込み先は、実際のパスを解決したうえで data/ の中に限る。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from kizashi.errors import KizashiError

INBOX_MAX_BYTES = 1024 * 1024
INBOX_SUFFIXES = (".json", ".sql")


def repo_root() -> Path:
    """リポジトリの直下（cli.check_working_dir で確かめた現在のフォルダー）。"""
    return Path.cwd()


def data_dir(root: Path) -> Path:
    return root / "data"


def raw_dir(root: Path) -> Path:
    return data_dir(root) / "raw"


def runs_dir(root: Path) -> Path:
    return data_dir(root) / "runs"


def run_dir(root: Path, run_id: str) -> Path:
    return runs_dir(root) / run_id


def dataset_dir(root: Path, domain: str) -> Path:
    return root / "datasets" / domain


def ensure_in_data(root: Path, path: Path) -> Path:
    """書き込み先が、実際のパスを解決したうえで data/ の中にあることを確かめる（3.4）。"""
    data = data_dir(root).resolve()
    resolved = path.resolve()
    if resolved != data and not resolved.is_relative_to(data):
        raise KizashiError(
            "E_INTERNAL",
            "cli",
            f"書き込み先が data/ の外にある：{path}",
            next_action="開発側で原因を調べる（data/ やその下のシンボリックリンクを確かめる）。",
        )
    return resolved


# inbox/ のファイル名に使える文字（D-067）。氏名の一覧は日本語なので、英数字の名前には既知の氏名が入らない。
# ファイル名は accepted/・queries/・events に残るため、氏名がファイル名から保存・表示される経路をなくす
INBOX_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _inbox_error(arg: str, why: str, suffixes: Iterable[str]) -> KizashiError:
    allowed = "・".join(suffixes)
    shown = arg if arg.isascii() else "（英数字以外の文字を含む指定。表示しない）"
    return KizashiError(
        "E_INBOX_PATH",
        "cli",
        f"提案ファイルの指定が誤っている（{why}）：{shown}",
        next_action=f"inbox/ の直下の {allowed} のファイルを inbox/<名前>{allowed} の形で指定する。",
    )


def check_inbox_arg(arg: str, suffixes: Iterable[str] = INBOX_SUFFIXES) -> str:
    """引数の文字列が inbox/<名前>.<拡張子> の形かを確かめ、ファイル名を返す（ファイルは見ない）。

    絶対パス、..、inbox/ の外とサブフォルダー、許していない拡張子を E_INBOX_PATH にする。
    """
    suffixes = tuple(suffixes)
    if "\\" in arg or "\x00" in arg:
        raise _inbox_error(arg, "使えない文字がある", suffixes)
    pure = PurePosixPath(arg)
    if pure.is_absolute():
        raise _inbox_error(arg, "絶対パスは使えない", suffixes)
    if ".." in pure.parts:
        raise _inbox_error(arg, "「..」は使えない", suffixes)
    parts = [p for p in pure.parts if p != "."]
    if len(parts) != 2 or parts[0] != "inbox":
        raise _inbox_error(arg, "inbox/ の直下のファイルでない", suffixes)
    name = parts[1]
    if PurePosixPath(name).suffix not in suffixes or name.startswith("."):
        raise _inbox_error(arg, "拡張子が違う", suffixes)
    if not INBOX_NAME_RE.fullmatch(name):
        raise _inbox_error(
            arg, "ファイル名に使えるのは英数字と - _ . だけ（先頭は英数字）", suffixes
        )
    return name


def resolve_inbox_file(root: Path, arg: str, suffixes: Iterable[str] = INBOX_SUFFIXES) -> Path:
    """inbox/ 直下の提案ファイルを検査してパスを返す（3.4。test_plan.md の IN-1）。

    サブフォルダー、他の拡張子、1MBを超えるファイル、シンボリックリンク（ファイルと inbox/ の
    どちらでも）、通常のファイルでないものを E_INBOX_PATH で拒否する。
    """
    suffixes = tuple(suffixes)
    name = check_inbox_arg(arg, suffixes)
    inbox = root / "inbox"
    if inbox.is_symlink() or not inbox.is_dir():
        raise _inbox_error(arg, "inbox/ がフォルダーでないか、シンボリックリンクである", suffixes)
    path = inbox / name
    if path.is_symlink():
        raise _inbox_error(arg, "シンボリックリンクは読まない", suffixes)
    if not path.exists():
        raise _inbox_error(arg, "ファイルがない", suffixes)
    if not path.is_file():
        raise _inbox_error(arg, "通常のファイルでない", suffixes)
    if path.resolve().parent != inbox.resolve():
        raise _inbox_error(arg, "inbox/ の直下のファイルでない", suffixes)
    if path.stat().st_size > INBOX_MAX_BYTES:
        raise _inbox_error(arg, "1MBを超えている", suffixes)
    return path
