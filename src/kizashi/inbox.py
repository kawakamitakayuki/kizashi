"""提案ファイル（inbox）の検査と、成功時の accepted/ への移動（implementation_spec.md 2章の inbox.py、3.4、4.5、6.1）。

- 場所・拡張子・大きさ・シンボリックリンクは paths.resolve_inbox_file で検査する（E_INBOX_PATH）。
- 中身は UTF-8 の JSON のオブジェクトで、最上位に kind（文字列）があること。キーの重複と NaN などは
  受け付けない（E_SCHEMA）。
- 成功したときだけ、data/runs/<run_id>/accepted/<連番>-<kind>.json に写し、inbox/ から消す。
  探索SQL（.sql）は名前を変えずに accepted/<名前> に写す（候補の query_file が指す。6.2）。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from kizashi import paths
from kizashi.errors import KizashiError

_SERIAL_RE = re.compile(r"^(\d{4})-")


@dataclass(frozen=True)
class Proposal:
    path: Path
    kind: str
    data: dict


def _schema(arg: str, why: str) -> KizashiError:
    return KizashiError(
        "E_SCHEMA",
        "record",
        f"提案ファイルの形が誤っている（{why}）：{arg}",
        next_action="1ファイルに1件、UTF-8 の JSON のオブジェクトで書き、最上位に kind を置く"
        "（.claude/skills/investigate/record-examples.md）。",
    )


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"同じキーが2回ある：{'・'.join(dup)}")
    return dict(pairs)


def _no_constant(name: str) -> object:
    raise ValueError(f"JSON にない値：{name}")


def load_proposal(root: Path, arg: str) -> Proposal:
    """inbox/<名前>.json を検査して読む。"""
    path = paths.resolve_inbox_file(root, arg, (".json",))
    try:
        text = path.read_bytes().decode("utf-8")
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except UnicodeDecodeError:
        raise _schema(arg, "UTF-8 で読めない") from None
    except ValueError as exc:  # JSONDecodeError を含む
        raise _schema(arg, f"JSON として読めない：{exc}") from None
    if not isinstance(data, dict):
        raise _schema(arg, "最上位がオブジェクトでない")
    kind = data.get("kind")
    if not isinstance(kind, str) or not kind:
        raise _schema(arg, "最上位に kind（文字列）がない")
    return Proposal(path=path, kind=kind, data=data)


def accepted_dir(root: Path, run_id: str) -> Path:
    target = paths.run_dir(root, run_id) / "accepted"
    target.mkdir(exist_ok=True)
    paths.ensure_in_data(root, target)
    return target


def next_serial(directory: Path) -> int:
    numbers = [int(m.group(1)) for p in directory.iterdir() if (m := _SERIAL_RE.match(p.name))]
    return max(numbers, default=0) + 1


def copy_to_accepted(root: Path, run_id: str, path: Path, label: str) -> Path:
    """提案を accepted/<連番>-<label>.json に写す（inbox/ の元のファイルは残す）。"""
    directory = accepted_dir(root, run_id)
    target = directory / f"{next_serial(directory):04d}-{label}.json"
    shutil.copy2(path, target)
    return target


def accept(root: Path, run_id: str, path: Path, label: str) -> Path:
    """提案を accepted/<連番>-<label>.json に写し、inbox/ から消す。"""
    target = copy_to_accepted(root, run_id, path, label)
    path.unlink()
    return target
