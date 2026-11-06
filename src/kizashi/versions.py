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
# 10：D-041（照合の PDF を原本の検査と検証記録のハッシュに入れる、依存する資料を条件と分野の規則から決める、
#     メモが展開する欄のすべての {calc:ID} を検査する、内容の同じ再検査でハッシュを変えない、質問の目的と残る差、
#     T6b の当たる項目）。check・render・record の結果が変わる。
# 11：D-042（該当する行のない依存する CSV も原本の検査とハッシュに入れる、年度は整数だけ、残る差の規則、Skill と
#     ルールのファイルを開始時に記録する、版の違う台帳への record を拒否する）。run・check・render・record の
#     結果が変わる。
# 12：D-043（金額以外の候補の残る差を説明を反映した値と同じ件数に限り、説明によって足した条件を記録して照らす、
#     条件の単純な同値の表現をそろえる、git のパスを NUL 区切りで読む）。check・render・record・run の結果が変わる。
# 13：D-044（候補の行に宣言した条件だけを当てた行と残る件数の行の一致を求める、method_family を説明の条件に
#     しない）。check・render の結果が変わる。
# 14：D-048（残りの件数を出す判定ごとに、残りを選ぶ条件の列が空の候補の行を =・!=・in・not_null で外していれば
#     不合格にする。別の説明の合格で免除しない）。check・render の結果が変わる。
# 15：D-049（空の値で外れた行の検査を免除する別の条件を、その判定の reflected_conditions に宣言した説明用の条件に
#     限る）。check・render の結果が変わる。
# 16：P0-8（check の V5・V6 と check の時点の V7、V7 の取り込まない列の値（D-032。record と check）、巡回数の
#     上限（E_ROUNDS_EXCEEDED。record の結論の案と check））。check・render・record の結果が変わる。
RULES_VERSION = "16"


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
    # Skill とルールのファイル（記録だけ。照らすのはメモでの表示。D-042④）
    versions["rule_files"] = rule_files_record(root, versions["git_commit"])
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


def rule_files(root: Path) -> list[Path]:
    """Skill とルールのファイル（CLAUDE.md と .claude/skills/ の Markdown）。"""
    files = [root / "CLAUDE.md"] if (root / "CLAUDE.md").is_file() else []
    skills = root / ".claude" / "skills"
    if skills.is_dir():
        files += sorted(skills.rglob("*.md"))
    return files


def _is_rule_file(rel: str) -> bool:
    return rel == "CLAUDE.md" or (rel.startswith(".claude/skills/") and rel.endswith(".md"))


def rule_file_hashes(root: Path) -> dict[str, str]:
    """いまの Skill とルールのファイル（パス → SHA-256）。"""
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in rule_files(root)}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def rule_files_record(root: Path, commit: str | None) -> dict:
    """Skill とルールのファイルの一覧と SHA-256 と、記録したコミットとの照合（3.5 の rule_files。D-042④）。

    コミットとの照合は、内容だけでなくファイルの一覧（追加・削除）も照らす。確かめられなければ matches_commit は
    None。"""
    files = rule_file_hashes(root)
    record: dict = {"files": files, "commit": commit, "matches_commit": None}
    if not commit or commit == "unknown":
        return record
    # -z：NUL 区切りで、日本語などのパスを引用・エスケープしない形で読む（D-043③）
    listed = _git(
        root, "ls-tree", "-r", "-z", "--name-only", commit, "--", "CLAUDE.md", ".claude/skills"
    )
    if listed is None or listed.returncode != 0:
        return record
    names = listed.stdout.split(b"\0")
    try:
        in_commit = sorted(r for r in (n.decode("utf-8") for n in names if n) if _is_rule_file(r))
    except UnicodeDecodeError:
        return record
    changed = []
    for rel in sorted(set(in_commit) & set(files)):
        shown = _git(root, "show", f"{commit}:{rel}")
        if shown is None or shown.returncode != 0:
            return record
        if sha256_bytes(shown.stdout) != files[rel]:
            changed.append(rel)
    record.update(
        added=sorted(set(files) - set(in_commit)),
        deleted=sorted(set(in_commit) - set(files)),
        changed=changed,
    )
    record["matches_commit"] = not (record["added"] or record["deleted"] or changed)
    return record
