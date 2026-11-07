"""伏せた写しと公開の検査（test_plan.md の RD-4・PB-1。implementation_spec.md 12章。D-022・D-032）。

合成データ（tests/fixtures/make_cao_fixtures.py）の値：
- 受注者名：翻訳業務の行（S001:000012・S005:000007）は「株式会社テスト翻訳」。令和6年度の競争入札の1行目
  （S001:000001）は「株式会社テスト清掃」。令和6年度の庁舎警備業務は「株式会社テスト?警備」（CSV の `?`）。
- 法人番号（13桁）：令和6年度の競争入札の1行目は 0000000000001。
- 担当官の氏名：「架空　太郎」など。担当官の所在地は「テスト県架空市官庁町１－１」、相手方の住所は
  「テスト県模擬市本町２－２」（どれも架空）。
テストの中で、原本の CSV の1行の相手方の住所を `?` を含む架空の住所に書き換える（取り込まない列なので、
正規化した値は変わらない）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from conftest import ledger_of, rec
from kizashi import check, cli, publish, render
from kizashi.errors import KizashiError
from test_check import prepare

ADDR = "テスト県模擬市本町２－２"
WILD_ADDR = "テスト県?野市緑町３－３"  # 架空。`?` は PDF の文字が置き換わったもの（D-022）
OFFICER_ADDR = "テスト県架空市官庁町１－１"


def rewrite_raw_csv(repo: Path, run_id: str, source_id: str, old: str, new: str) -> None:
    """原本の CSV の1か所を書き換え、台帳の SHA-256 を合わせる（取り込まない列の値を変えるため）。"""
    conn = ledger_of(repo, run_id)
    (sha,) = conn.execute("SELECT sha256 FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    data = (repo / "data" / "raw" / f"{sha}.csv").read_bytes()
    assert old.encode("cp932") in data
    changed = data.replace(old.encode("cp932"), new.encode("cp932"), 1)
    new_sha = hashlib.sha256(changed).hexdigest()
    (repo / "data" / "raw" / f"{new_sha}.csv").write_bytes(changed)
    conn.execute("UPDATE sources SET sha256 = ? WHERE source_id = ?", (new_sha, source_id))
    conn.commit()
    conn.close()


def set_counterparty(repo: Path, run_id: str, record_id: str, name: str) -> None:
    conn = ledger_of(repo, run_id)
    conn.execute(
        "UPDATE procurement_contracts SET counterparty_raw = ? WHERE record_id = ?",
        (name, record_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# RD-4：伏せた写し（render --public）
# ---------------------------------------------------------------------------


def test_rd4_public_copy_masks_counterparties(repo: Path, synthetic_run: str) -> None:
    """RD-4：--public の写しでは受注者名が「受注者A」などに替わり、元のメモは変わらない。CSV の受注者名の `?`
    （架空の（公財）テスト?研究所）は、メモの（公財）テスト?研究所と（公財）テスト−研究所に同じ記号が付く。"""
    set_counterparty(repo, synthetic_run, "S001:000001", "（公財）テスト?研究所")
    lead = prepare(repo, synthetic_run)
    rec(
        repo,
        synthetic_run,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "observation",
            "text": "同じ部局は（公財）テスト−研究所とも契約し、公表データには（公財）テスト?研究所と書かれている。",
            "calc_ids": [],
            "evidence": [],
        },
    )
    result = render.render_lead(repo, synthetic_run, lead["lead"], public=True)
    memo = (repo / result["memo"]).read_text(encoding="utf-8")
    copy = (repo / result["public"]).read_text(encoding="utf-8")
    assert result["public"] == f"data/runs/{synthetic_run}/public/{lead['lead']}.v1.md"
    assert "株式会社テスト翻訳" in memo and "（公財）テスト−研究所" in memo
    assert copy.startswith(render.PUBLIC_NOTICE + "\n\n")
    assert "受注者名は伏せている" in copy.splitlines()[0]
    assert "テスト翻訳" not in copy and "テスト−研究所" not in copy and "テスト?研究所" not in copy
    # 記号は実行の受注者名の最初に現れた行の順。令和6年度の競争入札の12行は受注者名がすべて違い、1行目
    # （S001:000001。書き換えた（公財）テスト?研究所）が A、12行目の株式会社テスト翻訳（S001:000012）が L。
    # 同じ名前の2つの書き方に同じ記号が付く
    assert "同じ部局は受注者Aとも契約し、公表データには受注者Aと書かれている。" in copy
    assert "受注者L" in copy
    conn = ledger_of(repo, synthetic_run)
    (after,) = conn.execute(
        "SELECT after_json FROM events WHERE kind = 'render' ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert json.loads(after)["public"] == f"public/{lead['lead']}.v1.md"


def test_rd4_disclosure_allow_names_are_not_masked(repo: Path, synthetic_run: str) -> None:
    """RD-4：examples/disclosure.allow にある名前は伏せない（記者が書く）。"""
    lead = prepare(repo, synthetic_run)
    (repo / "examples").mkdir()
    (repo / "examples" / "disclosure.allow").write_text("株式会社テスト翻訳\n", encoding="utf-8")
    result = render.render_lead(repo, synthetic_run, lead["lead"], public=True)
    copy = (repo / result["public"]).read_text(encoding="utf-8")
    assert "株式会社テスト翻訳" in copy


def test_rd4_wildcard_rule_and_corp_numbers() -> None:
    """RD-4（D-022）：`?` は空白以外の任意の1文字。`?` 以外が3文字未満の名前は元の表記どおりだけ。法人番号は
    全角の数字でも伏せる。重なる出現は長いものを採る。"""
    matcher = check.TermMatcher(
        [
            ("（公財）テスト?研究所", "name:a"),
            ("テ?", "name:b"),
            ("株式会社テスト", "name:c"),
            ("株式会社テスト配送", "name:d"),
            ("0000000000001", "corp"),
        ]
    )
    labels = {"name:a": "受注者A", "name:b": "受注者B", "name:c": "受注者C", "name:d": "受注者D"}
    labels["corp"] = render.CORP_NUMBER_MASK
    text = (
        "（公財）テスト一研究所、（公財）テスト 研究所、テス、テ?、株式会社テスト配送、"
        "法人番号 0000000000001・００００００００００００１"
    )
    assert render.redact(text, matcher, labels) == (
        "受注者A、（公財）テスト 研究所、テス、受注者B、受注者D、"
        "法人番号 法人番号（伏せ字）・法人番号（伏せ字）"
    )
    assert check.wildcard_pattern("テ?") is None
    assert check.wildcard_pattern("株式会社テスト") is None


def test_rd4_run_summary_public_copy(repo: Path, synthetic_run: str, capsys) -> None:
    """RD-4：render <run_id> --public は、実行の概要の伏せた写しを public/ に書く。"""
    code = cli.main(["render", synthetic_run, "--public"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["data"]["public"] == f"data/runs/{synthetic_run}/public/run_summary.v1.md"
    assert (
        (repo / payload["data"]["public"])
        .read_text(encoding="utf-8")
        .startswith(render.PUBLIC_NOTICE)
    )


# ---------------------------------------------------------------------------
# PB-1：check --publish（一時の Git リポジトリ）
def test_rd4_labels_do_not_shift_when_names_are_allowed(repo: Path, synthetic_run: str) -> None:
    """RD-4（D-056⑤）：記号は disclosure.allow の名前も含めて最初に現れた行の順に割り当て、公開を認めた名前は
    伏せない。先に現れる名前（S001:000001 の株式会社テスト清掃）を認めても、ほかの受注者の記号は変わらない
    （12行目の株式会社テスト翻訳は L のまま）。"""
    from kizashi.adapters import load_adapter

    conn = ledger_of(repo, synthetic_run)
    adapter = load_adapter(repo, "cao-procurement")
    terms, labels = render.redaction_terms(conn, adapter, set())
    allowed_terms, allowed_labels = render.redaction_terms(conn, adapter, {"株式会社テスト清掃"})
    conn.close()
    assert labels["name:株式会社テスト清掃"] == "受注者A"
    assert (
        labels["name:株式会社テスト翻訳"] == allowed_labels["name:株式会社テスト翻訳"] == "受注者L"
    )
    assert ("株式会社テスト清掃", "name:株式会社テスト清掃") in terms
    assert all(term != "株式会社テスト清掃" for term, _ in allowed_terms)
    shared = set(labels) & set(allowed_labels)
    assert all(labels[k] == allowed_labels[k] for k in shared)


# ---------------------------------------------------------------------------

AUX_NAME = "aux_reference_notes.md"  # 補助資料の架空のファイル名
AUX_CONTENT = "架空の補助資料の本文。公開しない。\n".encode()
# 補助資料4点（12.2）の一覧。1点目が上の AUX_NAME。ほかの3点も架空の名前と中身
AUX_DOCS = [
    (AUX_NAME, AUX_CONTENT),
    ("aux_second.md", "架空の補助資料2。\n".encode()),
    ("aux_third.xlsx", b"synthetic auxiliary 3"),
    ("aux_fourth.pdf", b"synthetic auxiliary 4"),
]


def denylist_text(docs: list[tuple[str, bytes]]) -> str:
    return "".join(f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in docs)


CLEAN = "# 公開する例\n\n受注者Aの契約金額の合計が増えた（合成データの例）。\n"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return done.stdout.decode()


@pytest.fixture
def git_repo(
    repo: Path, synthetic_run: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """合成データの実行を持つ作業フォルダーを Git のリポジトリにし、公開する例を1つコミットする。
    補助資料の一覧（KIZASHI_DENYLIST）はリポジトリの外に置く。"""
    for key, value in {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp_path / "home"),
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "home").mkdir()
    deny = tmp_path / "publish_denylist.txt"
    deny.write_text(denylist_text(AUX_DOCS), encoding="utf-8")
    monkeypatch.setenv("KIZASHI_DENYLIST", str(deny))
    git(repo, "init", "-q")
    (repo / ".gitignore").write_text("data/\ninbox/\n", encoding="utf-8")
    (repo / "examples").mkdir(exist_ok=True)
    write(repo, "examples/publish.manifest", "examples/example.md\n")
    write(repo, "examples/example.md", CLEAN)
    commit(repo, "公開する例")
    return repo


def write(repo: Path, rel: str, text: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def publish_failures(repo: Path, run_id: str) -> list[dict]:
    with pytest.raises(KizashiError) as info:
        publish.check_publish(repo, "examples/publish.manifest", [run_id])
    assert info.value.exit_code == 1 and info.value.stage == "check"
    assert info.value.code == info.value.failures[0]["code"]
    return info.value.failures


def found(failures: list[dict]) -> set[tuple[str, str, str]]:
    return {
        (f["code"], f["location"]["where"], f["location"]["path"])
        for f in failures
        if "location" in f
    }


def test_pb1_clean_repository_passes(git_repo: Path, synthetic_run: str, capsys) -> None:
    """PB-1：一覧とステージと最新のコミットが一致し、補助資料・氏名・受注者名がなければ合格する。"""
    result = publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    assert result["passed"] is True and result["files"] == 2
    code = cli.main(["check", "--publish", "examples/publish.manifest", "--run", synthetic_run])
    assert code == 0 and json.loads(capsys.readouterr().out)["data"]["passed"] is True


def test_pb1_pass_message_states_unchecked_contents(git_repo: Path, synthetic_run: str) -> None:
    """PB-1（D-070）：合格のメッセージは、氏名などを検査しなかった中身（UTF-8 として読めないか 5MB を超える）の
    数を示す。ひな形の状態ではそういう中身はない（0件）。CP932 の架空のファイル（担当官の氏名「架空　太郎」を含む）を
    公開の例に足すと、仕様どおり氏名は検査されずに合格し（12.2 の限界）、その1件をメッセージに書く。"""
    clean = publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    assert clean["targets"]["hash_only"] == 0
    assert "氏名などを検査できなかった中身はない" in clean["message"]
    write(git_repo, "examples/legacy.csv", "担当官,架空　太郎\n".encode("cp932"))
    write(git_repo, "examples/publish.manifest", "examples/example.md\nexamples/legacy.csv\n")
    commit(git_repo, "CP932 の例")
    result = publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    assert result["passed"] is True and result["targets"]["hash_only"] == 1
    assert "検査しておらず" in result["message"] and "1件" in result["message"]
    assert "検査を通したコミットそのもの" in result["message"]


def test_pb1_check_the_local_commit_to_publish(git_repo: Path, synthetic_run: str) -> None:
    """PB-1（D-069）：公開の手順は「公開用のコミットを手元で作り（push しない）、そのコミットで検査し、通ったその
    コミットを公開する」。新しい例を一覧に足してステージしただけでは、最新のコミットの一覧と合わず
    P_MANIFEST_MISMATCH。手元でコミットすると合格する。"""
    write(git_repo, "examples/second.md", CLEAN)
    write(git_repo, "examples/publish.manifest", "examples/example.md\nexamples/second.md\n")
    git(git_repo, "add", "-A")
    failures = publish_failures(git_repo, synthetic_run)
    assert {f["code"] for f in failures} == {publish.MANIFEST_MISMATCH}
    git(git_repo, "commit", "-q", "-m", "公開用のコミット")
    result = publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    assert result["passed"] is True and result["files"] == 3


def test_pb1_denylist_is_required(git_repo: Path, synthetic_run: str, monkeypatch) -> None:
    """PB-1：KIZASHI_DENYLIST がないか読めなければ E_DENYLIST_MISSING（終了コード3）で、合格にしない。"""
    monkeypatch.delenv("KIZASHI_DENYLIST")
    with pytest.raises(KizashiError) as info:
        publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    assert info.value.code == "E_DENYLIST_MISSING" and info.value.exit_code == 3
    monkeypatch.setenv("KIZASHI_DENYLIST", str(git_repo / "no-such-file.txt"))
    with pytest.raises(KizashiError) as info:
        publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    assert info.value.code == "E_DENYLIST_MISSING"


@pytest.mark.parametrize(
    "docs",
    [
        AUX_DOCS[:1],  # 1点だけ
        AUX_DOCS[:3],  # 3点
        [*AUX_DOCS, ("aux_fifth.md", b"synthetic auxiliary 5")],  # 5点
        [AUX_DOCS[0]] * 4,  # 同じ行を4回
        [*AUX_DOCS[:3], ("aux_other_name.md", AUX_DOCS[0][1])],  # 名前は4つ、中身は3つ
        [*AUX_DOCS[:3], (AUX_NAME.upper(), b"synthetic other")],  # 中身は4つ、名前は3つ
    ],
    ids=["one", "three", "five", "same-line", "same-hash", "same-name"],
)
def test_pb1_denylist_must_list_four_distinct_documents(
    git_repo: Path, synthetic_run: str, tmp_path: Path, docs: list
) -> None:
    """PB-1（D-056③）：補助資料の一覧は、4点（12.2）のそれぞれのハッシュと名前を1行ずつ持つ。点数が4でない、
    ハッシュか名前（NFC にし大文字と小文字を区別しない）が重なる一覧は E_DENYLIST_MISSING（終了コード3）で、
    合格にしない。不合格の文に補助資料の名前を書かない。"""
    (tmp_path / "publish_denylist.txt").write_text(denylist_text(docs), encoding="utf-8")
    with pytest.raises(KizashiError) as info:
        publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    assert info.value.code == "E_DENYLIST_MISSING" and info.value.exit_code == 3
    assert "4点" in info.value.message
    assert all(name.casefold() not in info.value.message.casefold() for name, _ in docs)


def test_pb1_unlisted_file_and_aux_name(git_repo: Path, synthetic_run: str) -> None:
    """PB-1：一覧にないファイルと、補助資料のファイル名（中身が違っても名前で）を見つける。"""
    write(git_repo, "examples/extra.md", CLEAN)
    write(git_repo, f"docs/{AUX_NAME}", "名前だけ同じ別の中身\n")
    commit(git_repo, "足した")
    failures = publish_failures(git_repo, synthetic_run)
    messages = [f["message"] for f in failures if f["code"] == publish.MANIFEST_MISMATCH]
    assert any("一覧にない：examples/extra.md" in m for m in messages)
    # D-090⑧：補助資料の名前を含むパスの全文は返さない。名前を伏せたパスと、場所の区分・blob・コミットで示す
    hits = [f for f in failures if f["code"] == publish.DENYLIST_NAME]
    assert hits
    for f in hits:
        assert AUX_NAME.casefold() not in json.dumps(f, ensure_ascii=False).casefold()
        assert f["path"] == "docs/（補助資料の名前）" == f["location"]["path"]
        assert f["location"]["where"] in ("staged", "history")
    staged = next(f for f in hits if f["location"]["where"] == "staged")
    assert staged["location"]["blob"]
    history = next(f for f in hits if f["location"]["where"] == "history")
    assert history["location"]["blob"] and history["location"]["commits"]


def test_pb1_aux_name_only_in_history_is_located_without_the_name(
    git_repo: Path, synthetic_run: str
) -> None:
    """PB-1（D-090⑧）：別名でコミットしてから消した補助資料の名前も、名前を書かずに、履歴の blob とそれを含む
    コミットで示す。不合格の文（最上位の message を含む）に名前を書かない。"""
    write(git_repo, f"docs/{AUX_NAME}", "名前だけ同じ別の中身\n")
    commit(git_repo, "足した")
    added = git(git_repo, "rev-parse", "HEAD").strip()
    (git_repo / "docs" / AUX_NAME).unlink()
    commit(git_repo, "消した")
    with pytest.raises(KizashiError) as info:
        publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    assert AUX_NAME.casefold() not in info.value.message.casefold()
    hits = [f for f in info.value.failures if f["code"] == publish.DENYLIST_NAME]
    assert [f["location"]["where"] for f in hits] == ["history"]
    assert added in hits[0]["location"]["commits"]
    assert AUX_NAME.casefold() not in json.dumps(hits, ensure_ascii=False).casefold()


def test_pb1_aux_name_is_masked_in_every_failure(git_repo: Path, synthetic_run: str) -> None:
    """PB-1（D-090⑧）：補助資料の名前のファイルを examples/ に置くと、一覧の食い違い・名前・中身（ハッシュ）の
    どの不合格でも、文と構造化した欄に名前を書かない（伏せたパスで示す）。"""
    write(git_repo, f"examples/{AUX_NAME}", AUX_CONTENT)
    commit(git_repo, "補助資料を置いた")
    with pytest.raises(KizashiError) as info:
        publish.check_publish(git_repo, "examples/publish.manifest", [synthetic_run])
    codes = {f["code"] for f in info.value.failures}
    assert {publish.MANIFEST_MISMATCH, publish.DENYLIST_NAME, publish.DENYLIST_HASH} <= codes
    assert AUX_NAME.casefold() not in info.value.message.casefold()
    dumped = json.dumps(info.value.failures, ensure_ascii=False).casefold()
    assert AUX_NAME.casefold() not in dumped
    assert "examples/（補助資料の名前）" in dumped


def test_pb1_names_and_counterparties_in_contents(git_repo: Path, synthetic_run: str) -> None:
    """PB-1：担当官の氏名、伏せていない受注者名・法人番号、取り込まない列の値（担当官の所在地）を見つける。
    見つけた語そのものは返さない。"""
    write(
        git_repo,
        "examples/example.md",
        CLEAN + "担当は架空 太郎。\n株式会社テスト翻訳と契約した。\n法人番号 0000000000001。\n"
        f"所在地は{OFFICER_ADDR}。\n",
    )
    commit(git_repo, "実名を入れた")
    failures = publish_failures(git_repo, synthetic_run)
    hits = found(failures)
    for code in (
        publish.PERSONAL_NAME,
        publish.COUNTERPARTY,
        publish.CORP_NUMBER,
        publish.EXCLUDED_VALUE,
    ):
        assert (code, "worktree", "examples/example.md") in hits
        assert (code, "staged", "examples/example.md") in hits
        assert (code, "history", "examples/example.md") in hits
    text = json.dumps(failures, ensure_ascii=False)
    assert "架空" not in text and "テスト翻訳" not in text and "官庁町" not in text


def test_pb1_staged_and_worktree_differ(git_repo: Path, synthetic_run: str) -> None:
    """PB-1：作業フォルダーは伏せたがステージに実名が残る場合と、ステージは伏せたが作業フォルダーに実名が残る
    場合を、それぞれ見つける（コミットの前なので履歴にはない）。"""
    write(git_repo, "examples/example.md", CLEAN + "株式会社テスト翻訳\n")
    git(git_repo, "add", "examples/example.md")
    write(git_repo, "examples/example.md", CLEAN)
    hits = found(publish_failures(git_repo, synthetic_run))
    assert (publish.COUNTERPARTY, "staged", "examples/example.md") in hits
    assert (publish.COUNTERPARTY, "worktree", "examples/example.md") not in hits
    assert not any(h[1] == "history" for h in hits)

    git(git_repo, "add", "examples/example.md")
    write(git_repo, "examples/example.md", CLEAN + "株式会社テスト翻訳\n")
    hits = found(publish_failures(git_repo, synthetic_run))
    assert (publish.COUNTERPARTY, "worktree", "examples/example.md") in hits
    assert (publish.COUNTERPARTY, "staged", "examples/example.md") not in hits


def test_pb1_name_only_in_past_commit(git_repo: Path, synthetic_run: str) -> None:
    """PB-1：過去のコミットにだけ実名のメモがある場合を、blob とそれを含むコミットで返す。"""
    write(git_repo, "examples/example.md", CLEAN + "株式会社テスト翻訳\n")
    commit(git_repo, "実名")
    bad = git(git_repo, "rev-parse", "HEAD").strip()
    write(git_repo, "examples/example.md", CLEAN)
    commit(git_repo, "伏せた")
    failures = publish_failures(git_repo, synthetic_run)
    history = [f for f in failures if f["location"]["where"] == "history"]
    assert [f["code"] for f in history] == [publish.COUNTERPARTY]
    assert bad in history[0]["location"]["commits"]
    assert not any(f["location"]["where"] in ("worktree", "staged") for f in failures)


def test_pb1_aux_content_committed_under_other_name_then_deleted(
    git_repo: Path, synthetic_run: str
) -> None:
    """PB-1：補助資料を別名でコミットしてから消しても、履歴の中身のハッシュで見つける。"""
    write(git_repo, "notes/renamed.bin", AUX_CONTENT)
    commit(git_repo, "別名で入れた")
    (git_repo / "notes" / "renamed.bin").unlink()
    commit(git_repo, "消した")
    failures = publish_failures(git_repo, synthetic_run)
    assert [
        (f["code"], f["location"]["where"], f["location"]["path"])
        for f in failures
        if f["code"] == publish.DENYLIST_HASH
    ] == [(publish.DENYLIST_HASH, "history", "notes/renamed.bin")]


def test_pb1_wildcard_counterparty_and_address(git_repo: Path, synthetic_run: str) -> None:
    """PB-1（D-022・D-032）：CSV の `?` を含む受注者名（株式会社テスト?警備）と取り込まない列の値（相手方の
    住所。合成データの1行を `?` を含む住所に書き換える）が、`?` の位置を別の文字で書いた形でも見つかる。"""
    rewrite_raw_csv(git_repo, synthetic_run, "S001", ADDR, WILD_ADDR)
    write(git_repo, "examples/example.md", CLEAN + "株式会社テスト−警備\n")
    commit(git_repo, "受注者名")
    assert (publish.COUNTERPARTY, "worktree", "examples/example.md") in found(
        publish_failures(git_repo, synthetic_run)
    )
    write(git_repo, "examples/example.md", CLEAN + "住所はテスト県大野市緑町３－３。\n")
    commit(git_repo, "住所")
    hits = found(publish_failures(git_repo, synthetic_run))
    assert (publish.EXCLUDED_VALUE, "worktree", "examples/example.md") in hits
    assert (publish.COUNTERPARTY, "worktree", "examples/example.md") not in hits
