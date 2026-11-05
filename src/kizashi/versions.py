"""版の記録（implementation_spec.md 3.5）。

- 照合するもの：dataset_yaml_sha256、sql_sha256、rules_version、parser_version。
- 記録だけのもの：code_version、git_commit（と、Claude Code の版）。

RULES_VERSION は、正規化・照合・検証の結果を変えるコードの変更をしたときに、人が手で上げる。
テストの期待値を変えたときは上げる（共通ルール11）。
"""

from __future__ import annotations

import os
import subprocess
from importlib import metadata
from pathlib import Path

import yaml

from kizashi import paths
from kizashi.hashing import sha256_bytes, sha256_file

# 2：P0-3 で照合（reconcile_import・reconcile_source）を足し、Q13 を別経路との突合にした（結果が変わる）。
# 3：D-033〜D-035（数値の途中で切れた引用、文書値の符号と ▲・△、追加資料のリダイレクト先、
#    V7 の氏名の検査の欄）。引用・文書値・取得・V7 の検査の結果が変わる。
# 4：D-036（引用の数値の範囲に符号・▲・△・先頭の「.」を含める、氏名の検査を提案のすべての文字列に、
#    比較の条件に強い語の警告）。引用と V7 の検査の結果が変わる。
# 5：D-037（探索SQL と実行設定にも氏名の検査）。query と run の結果が変わる。
RULES_VERSION = "5"


def code_version() -> str:
    try:
        return metadata.version("kizashi")
    except metadata.PackageNotFoundError:
        return "unknown"


def git_commit(root: Path) -> str:
    """いまのコミット（短い形）。git がない、リポジトリでないなどのときは unknown。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = proc.stdout.strip()
    return commit if proc.returncode == 0 and commit else "unknown"


def sql_sha256(root: Path, domain: str) -> str:
    """screens/*.sql と populations/*.sql を、パスの順に連結したハッシュ（3.5）。"""
    base = paths.dataset_dir(root, domain)
    files = sorted(
        [p for sub in ("screens", "populations") for p in (base / sub).glob("*.sql")],
        key=lambda p: p.relative_to(base).as_posix(),
    )
    return sha256_bytes(b"".join(p.read_bytes() for p in files))


def rule_versions(root: Path, domain: str) -> dict:
    """runs.rule_versions_json に記録する版。"""
    yaml_path = paths.dataset_dir(root, domain) / "dataset.yaml"
    yaml_bytes = yaml_path.read_bytes()
    config = yaml.safe_load(yaml_bytes)
    versions = {
        "dataset_yaml_sha256": sha256_bytes(yaml_bytes),
        "sql_sha256": sql_sha256(root, domain),
        "rules_version": RULES_VERSION,
        "parser_version": str(config["parser_version"]),
        "code_version": code_version(),
        "git_commit": git_commit(root),
    }
    claude_version = os.environ.get("KIZASHI_CLAUDE_VERSION")
    if claude_version:
        # scripts/investigate が渡す Claude Code の版（3.5）。記録だけで、照合しない。
        versions["claude_code_version"] = claude_version
    return versions


def knowledge_hashes(root: Path) -> dict[str, str]:
    """references/ の知識ファイルの SHA-256（runs.knowledge_hashes_json）。"""
    ref = root / "references"
    if not ref.is_dir():
        return {}
    return {p.name: sha256_file(p) for p in sorted(ref.glob("*.md"))}
