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
# 6：P0-5 で run に検出（detect）を足した（候補・自動の基礎集計・系列・比べた組の数）。run の結果が変わる。
# 7：D-038（系列を分野の系列のSQLで数え、合計できる行のない年度を0円にしない。件数の意味。探索の候補と結論
#    の母集団に絞り込みを残す。再開で原本の SHA-256 を照らす）と、資料で符号でない「-」で始まる引用の拒否。
#    run・record の結果が変わる。
# 8：D-040（実行設定の全文を氏名の検査の後に保存し、検査の前に止まった実行は inbox/ の設定で再開する。探索SQL
#    の結果・列名・SQLite のエラーの文にも氏名の検査）。run・query の結果が変わる。
# 9：P0-6 で check（V1〜V4・V8、T6a・T6b の機械の判定）と render（候補のメモ、検証記録のハッシュ）を足した。
#    check・render の結果が変わる。
RULES_VERSION = "9"


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


SQL_DIRS = ("screens", "populations", "series")
# 照合する版（3.5）。run の再開と、check・render が開始時の記録と照らす。
COMPARED_VERSIONS = ("dataset_yaml_sha256", "sql_sha256", "rules_version", "parser_version")


def sql_sha256(root: Path, domain: str) -> str:
    """screens/*.sql、populations/*.sql、series/*.sql を、パスの順に連結したハッシュ（3.5）。"""
    base = paths.dataset_dir(root, domain)
    files = sorted(
        [p for sub in SQL_DIRS for p in (base / sub).glob("*.sql")],
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


def drift(recorded: dict, root: Path, domain: str) -> list[str]:
    """開始時に記録した版と、いまの版で違う項目（照合するものだけ）。"""
    current = rule_versions(root, domain)
    return [k for k in COMPARED_VERSIONS if recorded.get(k) != current.get(k)]
