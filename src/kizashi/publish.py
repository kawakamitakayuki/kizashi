"""公開の検査（implementation_spec.md 2章の publish.py、12.2。設計書9.12）。

kizashi check --publish examples/publish.manifest --run <run_id> [--run <run_id> ...]

P0 の4項目を検査し、どれかに当たれば終了コード1で返す（P1 の項目は検査しない）。
1. 一覧（manifest）のファイルと、`git ls-files examples/`（ステージ）と、`git ls-tree -r --name-only HEAD examples/`
   （最新のコミット）が一致する。
2. 補助資料の中身の SHA-256 が、検査の対象のどの中身とも一致しない。ファイル名が、対象のパスと履歴のパス
   （`git log --all --name-only --format=`）にない。補助資料の名前とハッシュは環境変数 KIZASHI_DENYLIST の
   ファイル（リポジトリの外）から読む。ないか読めなければ E_DENYLIST_MISSING（終了コード3）。
3. 検査の対象のテキストに、氏名の一覧と取り込まない列の値（担当官の所在地と相手方の住所。D-032）がない。
4. 検査の対象のテキストに、disclosure.allow にない受注者名・法人番号がない。

検査の対象は、ファイル名の一覧ではなく中身である：作業フォルダーの一覧のファイル、ステージの blob
（`git ls-files -s`）、すべての参照からたどれる履歴の blob（`git rev-list --objects --all`）。中身の SHA-256 は
すべてで照らし、氏名・受注者名の検査は UTF-8 として読めて 5MB 以下のものだけに当てる。氏名の一覧などは
`--run` の実行の原本と台帳から検査のたびに作り、保存しない。見つけた語そのものは返さない（場所だけ）。
"""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from kizashi import check, ledger
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError
from kizashi.hashing import sha256_bytes
from kizashi.normalize import nfkc
from kizashi.render import disclosure_allow

STAGE = "check"
EXAMPLES = "examples"
DENYLIST_ENV = "KIZASHI_DENYLIST"
MAX_TEXT_BYTES = 5 * 1024 * 1024
AUX_DOCUMENTS = 4  # 12.2 の「補助資料4点」。一覧はその4点を1行ずつ持つ（D-056③）
_DENY_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")

# 検査ごとの不合格のコード（12.2 の表の P0 の4行。仕様はコードの名前を決めていない。D-053）
MANIFEST_MISMATCH = "P_MANIFEST_MISMATCH"
DENYLIST_HASH = "P_DENYLIST_HASH"
DENYLIST_NAME = "P_DENYLIST_NAME"
PERSONAL_NAME = "P_PERSONAL_NAME"
EXCLUDED_VALUE = "P_EXCLUDED_VALUE"
COUNTERPARTY = "P_COUNTERPARTY"
CORP_NUMBER = "P_CORP_NUMBER"
WHAT = {
    PERSONAL_NAME: "氏名の一覧の氏名",
    EXCLUDED_VALUE: "取り込まない列の値（担当官の所在地・相手方の住所）",
    COUNTERPARTY: "disclosure.allow にない受注者名",
    CORP_NUMBER: "disclosure.allow にない法人番号",
}


def _err(code: str, message: str, **kw) -> KizashiError:
    return KizashiError(code, STAGE, message, **kw)


# ---------------------------------------------------------------------------
# 補助資料の一覧（publish_denylist.txt）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Denylist:
    hashes: frozenset[str]
    names: frozenset[str]


def _norm_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def load_denylist() -> Denylist:
    """KIZASHI_DENYLIST のファイル。1行に `<SHA-256> <ファイル名>`（sha256sum の出力の形）。空行と # で始まる行は
    読まない。補助資料4点（12.2）のそれぞれを1行ずつ書く。形の誤った行がある、行が4つでない、ハッシュか名前
    （パスの最後の名前を NFC にし、大文字と小文字を区別しない）が重なる一覧は、4点がそろったと確かめられない
    ので合格にしない（E_DENYLIST_MISSING。D-056③）。不合格の文に補助資料の名前を書かない。"""
    where = os.environ.get(DENYLIST_ENV)
    hint = (
        f"補助資料{AUX_DOCUMENTS}点のファイル名とハッシュの一覧（publish_denylist.txt。リポジトリの外に置く）の"
        f"パスを、環境変数 {DENYLIST_ENV} に入れる。1行に「SHA-256 ファイル名」（sha256sum の出力の形）で、"
        f"{AUX_DOCUMENTS}点を1行ずつ書く。"
    )
    if not where:
        raise _err("E_DENYLIST_MISSING", f"環境変数 {DENYLIST_ENV} がない。", next_action=hint)
    try:
        text = Path(where).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _err(
            "E_DENYLIST_MISSING",
            f"{DENYLIST_ENV} のファイルを読めない（{type(exc).__name__}）。",
            next_action=hint,
        ) from None
    entries: list[tuple[str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _DENY_LINE_RE.match(line.strip())
        if match is None:
            raise _err(
                "E_DENYLIST_MISSING",
                f"{DENYLIST_ENV} のファイルの {number} 行目の形が誤っている。",
                next_action=hint,
            )
        name = _norm_name(PurePosixPath(match.group(2).replace("\\", "/")).name)
        entries.append((match.group(1).lower(), name))
    hashes = {h for h, _ in entries}
    names = {n for _, n in entries}
    if len(entries) != AUX_DOCUMENTS or len(hashes) != AUX_DOCUMENTS or len(names) != AUX_DOCUMENTS:
        raise _err(
            "E_DENYLIST_MISSING",
            f"{DENYLIST_ENV} のファイルで、補助資料{AUX_DOCUMENTS}点がそろったと確かめられない"
            f"（行 {len(entries)}、異なるハッシュ {len(hashes)}、異なる名前 {len(names)}。"
            f"どれも {AUX_DOCUMENTS} であること）。",
            next_action=hint,
        )
    return Denylist(frozenset(hashes), frozenset(names))


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=off", *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise _err("E_USAGE", f"git を実行できない（{type(exc).__name__}）。") from None
    if done.returncode != 0:
        raise _err(
            "E_USAGE",
            f"git {args[0]} が失敗した：{done.stderr.decode('utf-8', 'replace').strip()[:300]}",
            next_action="Git のリポジトリの直下で実行する。",
        )
    return done.stdout


def _split_z(data: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in data.split(b"\0") if p]


def _has_head(root: Path) -> bool:
    done = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def _read_blobs(root: Path, oids: Iterable[str]) -> dict[str, bytes]:
    """blob の中身（git cat-file --batch）。"""
    wanted = list(dict.fromkeys(oids))
    if not wanted:
        return {}
    out = _git(root, "cat-file", "--batch", stdin=("\n".join(wanted) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        header = out[pos:end].decode().split()
        pos = end + 1
        if len(header) < 3 or header[1] == "missing":
            continue
        size = int(header[2])
        blobs[header[0]] = out[pos : pos + size]
        pos += size + 1
    return blobs


@dataclass
class Target:
    """検査の対象の1つの中身（作業フォルダーのファイル、ステージの blob、履歴の blob）。"""

    where: str  # worktree / staged / history
    path: str
    content: bytes
    blob: str | None = None
    commits: list[str] = field(default_factory=list)

    def location(self) -> dict:
        place = {"where": self.where, "path": self.path}
        if self.blob:
            place["blob"] = self.blob
        if self.commits:
            place["commits"] = self.commits
        return place


def _history(root: Path) -> list[tuple[str, str]]:
    """すべての参照からたどれる blob の (oid, パス)。"""
    lines = _git(root, "rev-list", "--objects", "--all").decode("utf-8", "surrogateescape")
    pairs = [(line.split(" ", 1) + [""])[:2] for line in lines.splitlines() if line.strip()]
    if not pairs:
        return []
    checked = _git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        stdin=("\n".join(oid for oid, _ in pairs) + "\n").encode(),
    ).decode()
    blobs = {line.split()[0] for line in checked.splitlines() if line.endswith(" blob")}
    return [(oid, path) for oid, path in pairs if oid in blobs]


def _commits_with(root: Path, oid: str) -> list[str]:
    out = _git(root, "log", "--all", "--format=%H", f"--find-object={oid}")
    return [line for line in out.decode().splitlines() if line]


# ---------------------------------------------------------------------------
# 検査
# ---------------------------------------------------------------------------


def _manifest_entries(root: Path, manifest_arg: str) -> tuple[str, list[str]]:
    rel = PurePosixPath(manifest_arg.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != EXAMPLES:
        raise _err(
            "E_USAGE",
            f"一覧のファイルは examples/ の中のパスで指定する：{manifest_arg}",
            next_action="kizashi check --publish examples/publish.manifest --run <run_id> の形で呼ぶ。",
        )
    path = root / Path(*rel.parts)
    if not path.is_file() or path.is_symlink():
        raise _err(
            "E_REF_NOT_FOUND",
            f"一覧のファイルがない：{rel.as_posix()}",
            next_action="記者が examples/publish.manifest に公開するファイルを1行に1つ書く。",
        )
    entries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(PurePosixPath(line.replace("\\", "/")).as_posix())
    return rel.as_posix(), entries


WHERE_NAMES = {"worktree": "作業フォルダー", "staged": "ステージ", "history": "履歴"}
WHERE_ORDER = {"worktree": 0, "staged": 1, "history": 2}
MASKED_NAME = "（補助資料の名前）"


def _masked_path(path: str) -> str:
    """補助資料の名前を伏せたパス（最後の名前だけを替える。D-090⑧）。"""
    parent = PurePosixPath(path).parent.as_posix()
    return MASKED_NAME if parent in ("", ".") else f"{parent}/{MASKED_NAME}"


def _fail(failures: list[dict], code: str, message: str, ids: Iterable[str] = (), **extra) -> None:
    failures.append({"code": code, "message": message, "affected_ids": list(ids), **extra})


def _check_manifest(
    root: Path,
    manifest: str,
    entries: list[str],
    failures: list[dict],
    shown: Callable[[str], str] = lambda p: p,
) -> None:
    """一覧・ステージ・最新のコミットの一致。不合格の文のパスは shown で表す（補助資料の名前を伏せる。D-090⑧）。"""
    listed = set(entries) | {manifest}
    staged = set(_split_z(_git(root, "ls-files", "-z", "--", f"{EXAMPLES}/")))
    head = (
        set(
            _split_z(_git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", f"{EXAMPLES}/"))
        )
        if _has_head(root)
        else set()
    )
    outside = sorted(
        e
        for e in entries
        if PurePosixPath(e).parts[:1] != (EXAMPLES,) or ".." in PurePosixPath(e).parts
    )
    if outside:
        _fail(
            failures,
            MANIFEST_MISMATCH,
            "一覧に examples/ の外のパスがある：" + "、".join(map(shown, outside)),
        )
    for label, names in (
        ("ステージ（git ls-files）", staged),
        ("最新のコミット（git ls-tree HEAD）", head),
    ):
        missing, extra = sorted(listed - names), sorted(names - listed)
        if missing:
            _fail(
                failures,
                MANIFEST_MISMATCH,
                f"一覧にあるが{label}にない：" + "、".join(map(shown, missing)),
            )
        if extra:
            _fail(
                failures,
                MANIFEST_MISMATCH,
                f"{label}にあるが一覧にない：" + "、".join(map(shown, extra)),
            )
    absent = sorted(
        e
        for e in listed
        if PurePosixPath(e).parts[:1] == (EXAMPLES,)
        and ".." not in PurePosixPath(e).parts
        and not (root / e).is_file()
    )
    if absent:
        _fail(
            failures,
            MANIFEST_MISMATCH,
            "一覧にあるが作業フォルダーにない：" + "、".join(map(shown, absent)),
        )


def _targets(root: Path, manifest: str, entries: list[str]) -> list[Target]:
    targets: list[Target] = []
    for rel in dict.fromkeys([manifest, *entries]):
        parts = PurePosixPath(rel).parts
        if parts[:1] != (EXAMPLES,) or ".." in parts:
            continue  # examples/ の外は読まない（一覧の不一致として返している）
        path = root / rel
        if path.is_file() and not path.is_symlink():
            targets.append(Target("worktree", rel, path.read_bytes()))
    staged: list[tuple[str, str]] = []
    for item in _split_z(_git(root, "ls-files", "-s", "-z")):
        meta, _, path = item.partition("\t")
        parts = meta.split()
        if len(parts) >= 2:
            staged.append((parts[1], path))
    history = _history(root)
    blobs = _read_blobs(root, [o for o, _ in staged] + [o for o, _ in history])
    targets += [Target("staged", p, blobs[o], blob=o) for o, p in staged if o in blobs]
    targets += [Target("history", p, blobs[o], blob=o) for o, p in history if o in blobs]
    return targets


@dataclass
class _Terms:
    names: check.TermMatcher
    excluded: check.TermMatcher
    counterparties: check.TermMatcher


def _terms(root: Path, run_ids: list[str]) -> _Terms:
    allow = disclosure_allow(root)
    names: set[str] = set()
    excluded: set[str] = set()
    parties: set[str] = set()
    numbers: set[str] = set()
    for run_id in run_ids:
        conn = ledger.open_ro(root, run_id)
        try:
            (domain,) = conn.execute("SELECT domain FROM runs").fetchone()
            adapter = load_adapter(root, domain)
            module = adapter.module
            names |= set(check.name_variants(module.name_list(conn, root, adapter)))
            excluded |= set(module.excluded_values(conn, root, adapter))
            parties |= {n for n in module.counterparty_names(conn) if nfkc(n) not in allow}
            numbers |= {n for n in module.corp_numbers(conn) if n not in allow}
        finally:
            conn.close()
    return _Terms(
        names=check.TermMatcher(((n, PERSONAL_NAME) for n in sorted(names)), wildcard=False),
        excluded=check.TermMatcher(((v, EXCLUDED_VALUE) for v in sorted(excluded)), wildcard=True),
        counterparties=check.TermMatcher(
            [(n, COUNTERPARTY) for n in sorted(parties)]
            + [(n, CORP_NUMBER) for n in sorted(numbers)],
            wildcard=True,
        ),
    )


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _text_hits(text: str, terms: _Terms) -> dict[str, list[int]]:
    """当たった検査のコード → 行番号（語そのものは返さない）。元の文字列と NFKC にした文字列の両方を見る。"""
    found: dict[str, set[int]] = {}
    variants = dict.fromkeys([text, nfkc(text)])
    for variant in variants:
        for matcher in (terms.names, terms.excluded, terms.counterparties):
            for start, _, code in matcher.find(variant):
                found.setdefault(code, set()).add(_line_of(variant, start))
    return {code: sorted(lines) for code, lines in found.items()}


def check_publish(root: Path, manifest_arg: str, run_ids: list[str]) -> dict:
    """kizashi check --publish（12.2）。"""
    denylist = load_denylist()
    if not run_ids:
        raise _err(
            "E_BAD_ID", "--run <run_id> が要る（氏名と受注者名の一覧を実行の原本と台帳から作る）。"
        )
    manifest, entries = _manifest_entries(root, manifest_arg)
    failures: list[dict] = []

    # 補助資料の名前を含むパスの全文は返さず、名前を伏せたパスで示す（どの不合格の文と構造化した欄でも。D-090⑧）
    def denied(path: str) -> bool:
        return bool(path) and _norm_name(PurePosixPath(path).name) in denylist.names

    def shown(path: str) -> str:
        return _masked_path(path) if denied(path) else path

    _check_manifest(root, manifest, entries, failures, shown)
    terms = _terms(root, run_ids)
    targets = _targets(root, manifest, entries)

    # ファイル名：対象のパスと、履歴のパス（git log --all --name-only）。場所の区分・blob・コミットで示す

    def name_hit(where: str, path: str, blob: str | None, commits: list[str]) -> None:
        masked = _masked_path(path)
        location: dict = {"where": where, "path": masked}
        if blob:
            location["blob"] = blob
        if commits:
            location["commits"] = commits
        _fail(
            failures,
            DENYLIST_NAME,
            f"{WHERE_NAMES[where]}の {masked} に補助資料のファイル名がある。",
            [blob] if blob else [],
            path=masked,
            location=location,
        )

    seen_paths: set[str] = set()
    for target in sorted(targets, key=lambda t: (WHERE_ORDER[t.where], t.path, t.blob or "")):
        seen_paths.add(target.path)
        if denied(target.path):
            commits = (
                _commits_with(root, target.blob)
                if target.where == "history" and target.blob
                else []
            )
            name_hit(target.where, target.path, target.blob, commits)
    logged = (
        _git(root, "log", "--all", "--name-only", "--format=")
        .decode("utf-8", "replace")
        .splitlines()
    )
    for path in sorted({p for p in logged if p} - seen_paths):
        if denied(path):
            out = _git(root, "log", "--all", "--format=%H", "--", path)
            name_hit("history", path, None, [c for c in out.decode().splitlines() if c])

    scanned: dict[str, dict[str, list[int]]] = {}
    text_checked = hash_only = 0
    history_hits: dict[str, Target] = {}
    for target in targets:
        digest = sha256_bytes(target.content)
        hits: dict[str, list[int]] = {}
        if digest in denylist.hashes:
            hits[DENYLIST_HASH] = []
        if digest not in scanned:
            text: str | None = None
            if len(target.content) <= MAX_TEXT_BYTES:
                try:
                    text = target.content.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
            scanned[digest] = {} if text is None else _text_hits(text, terms)
            if text is None:
                hash_only += 1
            else:
                text_checked += 1
        hits.update(scanned[digest])
        if not hits:
            continue
        if target.where == "history" and target.blob:
            target.commits = _commits_with(root, target.blob)
            history_hits[target.blob] = target
        for code, lines in hits.items():
            what = "補助資料と同じ中身" if code == DENYLIST_HASH else WHAT[code]
            where = WHERE_NAMES[target.where]
            at = f"（{'・'.join(map(str, lines[:20]))}行目）" if lines else ""
            _fail(
                failures,
                code,
                f"{where}の {shown(target.path)}{at} に{what}がある。",
                [x for x in (target.blob,) if x],
                location={**target.location(), "path": shown(target.path), "lines": lines},
            )
    if failures:
        next_action = (
            "公開する写しは kizashi render --public で作り、記者が examples/ に置き直す。氏名・取り込まない列の値は"
            "公開物に入れない。出してよいと記者が判断した受注者名だけを examples/disclosure.allow に書く。"
        )
        if history_hits:
            next_action += (
                "履歴にあるものは、作業フォルダーを直しても公開物から消えない。履歴の書き換えは自動では行わない。"
                "公開用に新しいリポジトリを作り直す。"
            )
        lines = "／".join(f"{f['code']}：{f['message']}" for f in failures[:20])
        raise _err(
            failures[0]["code"],
            f"公開の検査に不合格が{len(failures)}件ある：{lines}",
            affected_ids=list(run_ids),
            next_action=next_action,
            failures=failures,
        )
    return {
        "passed": True,
        "manifest": manifest,
        "files": len(set(entries) | {manifest}),
        "runs": run_ids,
        "targets": {
            "worktree": sum(1 for t in targets if t.where == "worktree"),
            "staged": sum(1 for t in targets if t.where == "staged"),
            "history_blobs": sum(1 for t in targets if t.where == "history"),
            "text_checked": text_checked,
            "hash_only": hash_only,
        },
        "message": "公開の検査（P0 の4項目）に合格した。"
        + (
            f"UTF-8 として読めないか 5MB を超える中身が {hash_only}件あり、それらは氏名・取り込まない列の値・"
            "受注者名・法人番号を検査しておらず、補助資料のハッシュだけを照らした（中身は記者が確かめる）。"
            if hash_only
            else "氏名などを検査できなかった中身はない（どれも UTF-8 で 5MB 以下）。"
        )
        + "P1 の項目（秘密の書式、個人の絶対パス、表計算の数式）は検査していない。まだ取り込んでいない遠隔の"
        "履歴と、--all に入らない参照（reflog だけに残るもの）も検査していない。公開するのは、この検査を通した"
        "コミットそのもの（検査の後に変えていないもの）だけにする。",
    }
