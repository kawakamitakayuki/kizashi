"""候補のメモ（kizashi render <lead_id>。test_plan.md の RD-1・RD-2、CP-1（メモ）。implementation_spec.md 4.7・
10.1・11章）。

test_check.py の prepare で、合成データの「翻訳業務」の候補に比較成立の必須の中身をそろえる。期待値は合成の CSV
から手で決めた：令和6年度 650,000円・令和7年度 750,000円、差 100,000円、増減率 100,000÷650,000＝0.15384…
（四捨五入で 15.4%）。令和6年度の行 S001:000012 は NO 12（見出し4行の後で16行目の目安）・PDF の3ページ、
令和7年度の行 S005:000007 は NO 7（11行目の目安）・PDF の2ページ。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ledger_of, rec
from kizashi import cli, hashing, render
from kizashi.adapters import load_adapter
from kizashi.errors import KizashiError
from test_check import ROW_FROM, ROW_TO, prepare

SECTIONS = (
    "## 1 状態と範囲",
    "## 2 検出した変化",
    "## 3 照合済みの記載と計算",
    "## 4 説明の検討",
    "## 5 言えないこと",
    "## 6 次の取材質問",
    "## 7 記者の確認リスト",
    "## 8 再現情報",
)


def memo_files(repo: Path, run_id: str) -> list[str]:
    memos = repo / "data" / "runs" / run_id / "memos"
    return sorted(p.name for p in memos.glob("*.md")) if memos.is_dir() else []


def test_one_lead_from_records_to_memo(repo: Path, synthetic_run: str) -> None:
    """完了条件：record で仮説・対応（confirmed）・計算・主張・判定・質問・結論の案を登録した候補が check に
    合格し、render がメモを書く。候補の状態は結論の案になり、メモの番号と検証記録のハッシュが残る。"""
    lead = prepare(repo, synthetic_run)
    result = render.render_lead(repo, synthetic_run, lead["lead"])
    lid = lead["lead"]
    assert (result["state"], result["memo_no"]) == ("change", 1)
    assert result["memo"] == f"data/runs/{synthetic_run}/memos/{lid}.v1.md"
    conn = ledger_of(repo, synthetic_run)
    row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lid,)).fetchone()
    assert (row["state"], row["memo_no"], row["rounds"]) == (
        "change",
        1,
        0,
    )  # render は巡回数に数えない
    assert (
        row["memo_hash"]
        == hashing.check_hash(conn, lid, load_adapter(repo, "cao-procurement"))
        == result["check_hash"]
    )
    assert [r[0] for r in conn.execute("SELECT state FROM claims")] == ["checked", "checked"]
    event = conn.execute("SELECT * FROM events WHERE kind = 'render'").fetchone()
    assert (event["target_id"], event["check_hash"]) == (lid, row["memo_hash"])
    conn.close()
    text = (repo / result["memo"]).read_text(encoding="utf-8")
    assert result["check_hash"] in text.splitlines()[3]


def test_rd1_memo_sections_and_expansions(repo: Path, synthetic_run: str) -> None:
    """RD-1：8節がそろい、{calc:ID} が値・意味・計算IDに展開される（円は桁区切り、比率は四捨五入で小数1桁）。
    型と状態が日本語で出る。記者の確認リストの項目が原本の見出しで出る。冒頭に定型文、5節に CSV と PDF の定型文。"""
    lead = prepare(repo, synthetic_run)
    text = (repo / render.render_lead(repo, synthetic_run, lead["lead"])["memo"]).read_text(
        encoding="utf-8"
    )
    lines = text.splitlines()
    assert lines[0] == "# 契約金額の増加：翻訳業務"
    assert lines[2] == "> 調査用・記者未確認。調査の端緒であり、不正の指摘ではない。"
    # 合成の実行は KIZASHI_PROFILE なし（調査用の設定でない）
    assert lines[3].startswith(
        "> 調査用の設定で実行：いいえ　実行ID：" + synthetic_run + "　メモ：v1"
    )
    positions = [text.index(s) for s in SECTIONS]
    assert positions == sorted(positions)
    k_from, _k_to = lead["calc_from"], lead["calc_to"]
    meaning = "2024年度の契約金額の合計（s1_amount_change・翻訳業務。合計から除いた行を除く）"
    assert f"650,000円（{meaning}。計算{k_from}）" in text
    assert f"契約金額の合計は650,000円（{meaning}。計算{k_from}）から750,000円（" in text
    assert (
        "15.4%（契約金額の合計の増減率（2024年度を基準。s1_amount_change・翻訳業務）。計算" in text
    )
    assert (
        "100,000円（契約金額の合計の差（2025年度−2024年度。s1_amount_change・翻訳業務）。計算"
        in text
    )
    assert "{calc:" not in text
    assert "- 結論：比較成立（説明の状況：なし）" in text
    assert "- T6a 取り込みの誤り：否定（kizashi が照合の記録から判定）" in text
    assert "- T6b 公表形式間の食い違い：否定（kizashi が照合の記録から判定）" in text
    assert "- T2 集計範囲・期間の違い：否定" in text and "- T1 制度・定義の変更：未確認" in text
    assert "### C0001（観測）" in text and "### C0002（原資料の記載。2025年度）" in text
    assert "CSVとPDFの一致は元の記載の正しさを示さない。" in text.split("## 5 言えないこと")[1]
    assert "- 同じ変化区分に属する、ほかの候補の件数：9件（同じ実行の検出の候補）" in text
    assert "- 探索した比較の数：11組（検出SQL s1_amount_change）" in text
    checklist = text.split("## 7 記者の確認リスト")[1].split("## 8 再現情報")[0]
    assert (
        f"- {ROW_FROM}：20250711-2_kohyo_kyoso.csv（原本 data/raw/" in checklist
        and "NO 12。複合キー 翻訳業務／R7.3.31／650,000／" in checklist  # D-090⑦
        and "。表計算ソフトの行 16行目（目安）。PDF S002 の3ページ。" in checklist
    )
    assert (
        "「物品役務等の名称及び数量」=翻訳業務、「契約金額」=650,000" not in checklist
    )  # 項目の順は区分の照合項目
    assert (
        "「契約金額」=650,000、「契約を締結した日」=R7.3.31、「物品役務等の名称及び数量」=翻訳業務"
        in checklist
    )
    assert (
        f"- {ROW_TO}：" in checklist and "NO 7。複合キー 翻訳業務／R7.6.1／750,000／" in checklist
    )
    assert "。表計算ソフトの行 11行目（目安）。" in checklist  # 複合キーの後（D-090⑦）
    assert "### 同種業務の対応" in checklist and "（確認済み。1:1）" in checklist
    assert "原本照合ができなかった行" not in checklist
    body = text.split("## 8 再現情報")[0]
    for english in ("checked", "support", "refute", "unknown", "completed", "fetched", "confirmed"):
        assert english not in body, english
    assert "（根拠の役割：支える）" in body and "（完了。途中で止まっていない）" in body


def test_rd1_unverified_rows_first_and_coefficients(repo: Path, synthetic_run: str) -> None:
    """RD-1・CP-1：保留（source_check）のメモでは、照合できなかった行と項目を確認リストの先頭に出す。係数の提案は
    4節（推論・未適用）と7節の先頭（記者が判断すること）に出る。"""
    lead = prepare(repo, synthetic_run, conclusion=False)
    conn = ledger_of(repo, synthetic_run)
    (result,) = conn.execute(
        "SELECT result_json FROM computations WHERE calc_id = 'K0005'"
    ).fetchone()
    data = json.loads(result)
    data["rows"][ROW_FROM]["fields"]["contract_date"] = "unverifiable"
    conn.execute(
        "UPDATE computations SET result_json = ? WHERE calc_id = 'K0005'", (json.dumps(data),)
    )
    conn.commit()
    conn.close()
    rec(
        repo,
        synthetic_run,
        {
            "kind": "coefficient",
            "lead_id": lead["lead"],
            "key": "tax",
            "value": "1.1",
            "meaning": "税込と税抜の換算",
            "why_needed": "片方の年度が税抜かもしれない。",
            "apply_to": "両年度の合計",
            "basis": [],
            "status": "proposed",
        },
    )
    rec(
        repo,
        synthetic_run,
        {
            "kind": "conclusion",
            "lead_id": lead["lead"],
            "outcome": "hold",
            "comparison_state": "comparable",
            "hold_reason": "source_check",
        },
    )
    text = (repo / render.render_lead(repo, synthetic_run, lead["lead"])["memo"]).read_text(
        encoding="utf-8"
    )
    assert "- 結論：保留（理由：原本照合ができない（照合不能））" in text
    assert "- T6b 公表形式間の食い違い：未確認（kizashi が照合の記録から判定）" in text
    section4 = text.split("## 4 説明の検討")[1].split("## 5")[0]
    assert section4.lstrip().startswith("### 補正の係数の提案（推論・未適用）")
    checklist = text.split("## 7 記者の確認リスト")[1].split("## 8")[0]
    first, unverified = (
        checklist.index("### 記者が判断すること"),
        checklist.index("### 原本照合ができなかった"),
    )
    assert first < unverified < checklist.index("### 原本で確かめる行")
    block = checklist[unverified : checklist.index("### 原本で確かめる行")]
    assert ROW_FROM in block and "「契約を締結した日」=R7.3.31（照合：照合不能）" in block
    assert ROW_TO not in block


def test_rd2_failure_writes_nothing(
    repo: Path, synthetic_run: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """RD-2：不合格なら何も書かない（メモのファイル、番号、主張と候補の状態、T6 の判定、events を変えない）。
    CLI は不合格のすべてを failures に入れて終了コード1で返す。"""
    lead = prepare(repo, synthetic_run, match="none")  # 対応のない比較成立
    capsys.readouterr()
    assert cli.main(["render", lead["lead"], "--run", synthetic_run]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "V8_MATCH_UNCONFIRMED" and out["error"]["stage"] == "render"
    assert [f["code"] for f in out["error"]["failures"]] == ["V8_MATCH_UNCONFIRMED"]
    assert memo_files(repo, synthetic_run) == []
    conn = ledger_of(repo, synthetic_run)
    row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead["lead"],)).fetchone()
    assert (row["state"], row["memo_no"], row["memo_hash"], row["rounds"]) == (
        "investigating",
        0,
        None,
        0,
    )
    assert {r[0] for r in conn.execute("SELECT state FROM claims")} == {"draft"}
    assert (
        conn.execute("SELECT COUNT(*) FROM explanations WHERE context = 'cli'").fetchone()[0] == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'render'").fetchone()[0] == 0
    conn.close()


def test_rd2_numbers_advance_without_overwriting(repo: Path, synthetic_run: str) -> None:
    """RD-2：render のたびに番号を進め（v1、v2）、前のメモを上書きしない。番号のファイルがすでにあれば書かない。"""
    lead = prepare(repo, synthetic_run)
    first = render.render_lead(repo, synthetic_run, lead["lead"])
    v1 = (repo / first["memo"]).read_text(encoding="utf-8")
    second = render.render_lead(repo, synthetic_run, lead["lead"])
    assert second["memo_no"] == 2 and second["memo"].endswith(f"{lead['lead']}.v2.md")
    assert (repo / first["memo"]).read_text(encoding="utf-8") == v1
    assert memo_files(repo, synthetic_run) == [f"{lead['lead']}.v1.md", f"{lead['lead']}.v2.md"]
    stray = repo / "data" / "runs" / synthetic_run / "memos" / f"{lead['lead']}.v3.md"
    stray.write_text("前からあるファイル", encoding="utf-8")
    with pytest.raises(KizashiError) as info:
        render.render_lead(repo, synthetic_run, lead["lead"])
    assert info.value.code == "E_INTERNAL"
    assert stray.read_text(encoding="utf-8") == "前からあるファイル"
    conn = ledger_of(repo, synthetic_run)
    assert (
        conn.execute("SELECT memo_no FROM leads WHERE lead_id = ?", (lead["lead"],)).fetchone()[0]
        == 2
    )
    conn.close()


def test_hash_changes_with_the_ledger(repo: Path, synthetic_run: str) -> None:
    """11章：検証記録のハッシュは、主張の本文を変えると変わり、メモの番号・ハッシュ・巡回数だけを変えても
    変わらない。"""
    lead = prepare(repo, synthetic_run)
    render.render_lead(repo, synthetic_run, lead["lead"])
    conn = ledger_of(repo, synthetic_run)
    before = hashing.check_hash(conn, lead["lead"], load_adapter(repo, "cao-procurement"))
    conn.execute(
        "UPDATE leads SET memo_no = 9, memo_hash = 'x', rounds = 5 WHERE lead_id = ?",
        (lead["lead"],),
    )
    assert hashing.check_hash(conn, lead["lead"], load_adapter(repo, "cao-procurement")) == before
    conn.execute("UPDATE claims SET text = text || '。' WHERE claim_id = ?", (lead["observation"],))
    assert hashing.check_hash(conn, lead["lead"], load_adapter(repo, "cao-procurement")) != before
    conn.close()


def test_render_rows_and_files_in_section3(repo: Path, synthetic_run: str) -> None:
    """設計書6章の3節：計算に加わったレコードの一覧（ファイル名、URL、取得日、複合キー、PDF のページ）と式。"""
    lead = prepare(repo, synthetic_run)
    text = (repo / render.render_lead(repo, synthetic_run, lead["lead"])["memo"]).read_text(
        encoding="utf-8"
    )
    section3 = text.split("## 3 照合済みの記載と計算")[1].split("## 4")[0]
    assert (
        f"{ROW_FROM}：20250711-2_kohyo_kyoso.csv（https://www.cao.go.jp/chotatsu/kohyo/tekiseika/6nendo/"
        "20250711-2_kohyo_kyoso.csv。取得 " in section3
    )
    assert "複合キー 翻訳業務／R7.3.31／650,000／株式会社テスト翻訳。PDF 3ページ" in section3
    assert (
        "式：sum（amount_yen）。条件：fiscal_year = 2024、record_id in ['S001:000012']。行の数 1"
        in section3
    )
    assert f"式：diff({lead['calc_to']}, {lead['calc_from']})" in section3


def test_d041_source_list_traces_quotes(repo: Path, synthetic_run: str) -> None:
    """RD-1（D-041⑭）：3節の末尾の資料の一覧に、追加資料・照合の PDF を含む検証が依存する資料の、資料ID・URL・
    取得日時・保存場所・SHA-256 が出る。8節は Skill とルールのファイルがコミットと同じか確かめた結果を出す。"""
    import httpx

    from kizashi import fetcher

    lead = prepare(repo, synthetic_run)
    url = "https://www.cao.go.jp/test/translation-explanation.html"
    html = "<html><body><p>翻訳の分量を増やした。</p></body></html>".encode()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=html
        )
    )
    source = fetcher.fetch_evidence(repo, synthetic_run, url, "evidence", transport=transport)
    rec(
        repo,
        synthetic_run,
        {
            "kind": "claim",
            "lead_id": lead["lead"],
            "type": "publisher_explanation",
            "text": "公表元は、翻訳の分量を増やしたと説明している。",
            "calc_ids": [],
            "evidence": [
                {
                    "source_id": source["source_id"],
                    "position": {"page": 1},
                    "quote": "翻訳の分量を増やした。",
                    "role": "support",
                }
            ],
        },
    )
    text = (repo / render.render_lead(repo, synthetic_run, lead["lead"])["memo"]).read_text(
        encoding="utf-8"
    )
    listing = text.split("### 資料の一覧")[1].split("## 4")[0]
    conn = ledger_of(repo, synthetic_run)
    rows = {
        r["source_id"]: r
        for r in conn.execute(
            "SELECT * FROM sources WHERE source_id IN ('S001', 'S002', 'S005', 'S006', ?)",
            (source["source_id"],),
        )
    }
    conn.close()
    for sid, ext in (
        ("S001", "csv"),
        ("S002", "pdf"),
        ("S005", "csv"),
        ("S006", "pdf"),
        (source["source_id"], "html"),
    ):
        row = rows[sid]
        line = next(x for x in listing.splitlines() if x.startswith(f"- {sid}："))
        assert row["url"] in line and row["sha256"] in line and row["fetched_at"] in line, sid
        assert f"data/raw/{row['sha256']}.{ext}" in line, sid
    section8 = text.split("## 8 再現情報")[1]
    assert (
        "- Skill とルールのファイル（run の開始時の記録）：コミットと同じかは確かめられなかった"
        in section8
    )  # 合成のフォルダーは git でない


# ---------------------------------------------------------------------------
# D-042④：Skill とルールのファイルの版
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", *args],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


def test_d042_rule_files_state_detects_added_deleted_changed(tmp_path: Path) -> None:
    """3.5（D-042④）：コミットとの照合は、内容だけでなくファイルの一覧（追加・削除）も照らす。"""
    import shutil

    from kizashi import versions

    if shutil.which("git") is None:
        pytest.skip("git がない")
    root = tmp_path / "rules"
    skill = root / ".claude" / "skills" / "investigate" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    (root / "CLAUDE.md").write_text("テスト用のルール\n", encoding="utf-8")
    skill.write_text("テスト用の Skill\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "CLAUDE.md", ".claude")
    _git(root, "commit", "-qm", "合成の基準")
    commit = _git(root, "rev-parse", "HEAD")
    state = versions.rule_files_record(root, commit)
    assert state["matches_commit"] is True and (
        state["added"],
        state["deleted"],
        state["changed"],
    ) == ([], [], [])
    skill.unlink()
    extra = root / ".claude" / "skills" / "review-lead" / "SKILL.md"
    extra.parent.mkdir(parents=True)
    extra.write_text("足した Skill\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("書き換えたルール\n", encoding="utf-8")
    state = versions.rule_files_record(root, commit)
    assert state["matches_commit"] is False
    assert state["deleted"] == [".claude/skills/investigate/SKILL.md"]
    assert state["added"] == [".claude/skills/review-lead/SKILL.md"]
    assert state["changed"] == ["CLAUDE.md"]


def test_d043_japanese_rule_file_names(tmp_path: Path) -> None:
    """3.5（D-043③）：日本語名の Skill の補助ファイルも、変更なし・追加・削除・内容の変更をコミットと照らせる
    （git の引用・エスケープしたパスを読まない）。"""
    import shutil

    from kizashi import versions

    if shutil.which("git") is None:
        pytest.skip("git がない")
    root = tmp_path / "rules"
    helper = root / ".claude" / "skills" / "review-lead" / "判断基準.md"
    helper.parent.mkdir(parents=True)
    (root / "CLAUDE.md").write_text("テスト用のルール\n", encoding="utf-8")
    helper.write_text("テスト用の判断基準\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "CLAUDE.md", ".claude")
    _git(root, "commit", "-qm", "合成の基準")
    commit = _git(root, "rev-parse", "HEAD")
    rel = ".claude/skills/review-lead/判断基準.md"

    def state() -> tuple:
        r = versions.rule_files_record(root, commit)
        return r["matches_commit"], r["added"], r["deleted"], r["changed"]

    assert state() == (True, [], [], [])
    added = root / ".claude" / "skills" / "review-lead" / "新しい手順.md"
    added.write_text("足した手順\n", encoding="utf-8")
    assert state() == (False, [".claude/skills/review-lead/新しい手順.md"], [], [])
    added.unlink()
    helper.write_text("書き換えた判断基準\n", encoding="utf-8")
    assert state() == (False, [], [], [rel])
    helper.unlink()
    assert state() == (False, [], [rel], [])


def _start_with_rule_files(repo: Path) -> str:
    from conftest import Site, write_config
    from kizashi import runner

    skill = repo / ".claude" / "skills" / "investigate" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("開始時の Skill\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("開始時のルール\n", encoding="utf-8")
    return runner.start_run(
        repo, "cao-procurement", write_config(repo), transport=Site().transport
    )["run_id"]


def test_d042_rule_files_recorded_at_start_and_compared_at_render(repo: Path) -> None:
    """3.5・10.1（D-042④）：run の開始時に Skill とルールのファイルの一覧と SHA-256 を記録し、メモの8節に開始時の
    記録と render の時点の値を分けて出し、開始時からの追加・削除・変更を書く。開始時の記録は上書きしない。"""
    import hashlib

    run_id = _start_with_rule_files(repo)
    conn = ledger_of(repo, run_id)
    recorded = json.loads(conn.execute("SELECT rule_versions_json FROM runs").fetchone()[0])
    conn.close()
    start = recorded["rule_files"]
    assert start["files"] == {
        ".claude/skills/investigate/SKILL.md": hashlib.sha256(
            "開始時の Skill\n".encode()
        ).hexdigest(),
        "CLAUDE.md": hashlib.sha256("開始時のルール\n".encode()).hexdigest(),
    }
    assert start["matches_commit"] is None  # 合成のフォルダーは git でない
    (repo / ".claude" / "skills" / "investigate" / "SKILL.md").write_text(
        "書き換えた Skill\n", encoding="utf-8"
    )
    (repo / "CLAUDE.md").unlink()
    added = repo / ".claude" / "skills" / "review-lead" / "SKILL.md"
    added.parent.mkdir(parents=True)
    added.write_text("足した Skill\n", encoding="utf-8")
    lead = prepare(repo, run_id)
    text = (repo / render.render_lead(repo, run_id, lead["lead"])["memo"]).read_text(
        encoding="utf-8"
    )
    section8 = text.split("## 8 再現情報")[1]
    assert "- Skill とルールのファイル（run の開始時の記録）：" in section8
    assert "- Skill とルールのファイル（render の時点）：" in section8
    assert (
        "開始時から変わった：追加 .claude/skills/review-lead/SKILL.md、削除 CLAUDE.md、変更 .claude/skills/investigate/SKILL.md"
        in section8
    )
    assert start["files"]["CLAUDE.md"] in section8.split("（render の時点）")[0]
    conn = ledger_of(repo, run_id)
    again = json.loads(conn.execute("SELECT rule_versions_json FROM runs").fetchone()[0])
    conn.close()
    assert again["rule_files"] == start  # 上書きしない


def test_d042_run_without_start_record_is_unrecorded(repo: Path, synthetic_run: str) -> None:
    """10.1（D-042④）：開始時の記録のない実行は「未記録」とし、render の時点の値で補わない。"""
    conn = ledger_of(repo, synthetic_run)
    conn.execute(
        "UPDATE runs SET rule_versions_json = json_remove(rule_versions_json, '$.rule_files')"
    )
    conn.commit()
    conn.close()
    lead = prepare(repo, synthetic_run)
    text = (repo / render.render_lead(repo, synthetic_run, lead["lead"])["memo"]).read_text(
        encoding="utf-8"
    )
    assert "- Skill とルールのファイル（run の開始時の記録）：未記録" in text


def test_d042_source_list_includes_required_csv_without_rows(
    repo: Path, synthetic_run: str
) -> None:
    """10.1（D-042①）：資料の一覧に、該当する行のない依存する CSV（翻訳業務の随意契約 S003・S007）も出る。"""
    lead = prepare(repo, synthetic_run)
    text = (repo / render.render_lead(repo, synthetic_run, lead["lead"])["memo"]).read_text(
        encoding="utf-8"
    )
    listing = text.split("### 資料の一覧")[1].split("## 4")[0]
    assert "- S003：" in listing and "- S007：" in listing


def test_rd1_kept_hypothesis_is_shown_only_as_kept(repo: Path, synthetic_run: str) -> None:
    """RD-1（10.1。P0-7 の後のレビュー）：仮説の結果 kept は「維持」とだけ出す。取り下げ条件を調べて否定したこと
    （「取り下げ条件は満たされなかった」）までは書かない（仮説を維持する判断と、反証の条件を調べて否定したことは別）。"""
    lead = prepare(repo, synthetic_run)
    text = (repo / render.render_lead(repo, synthetic_run, lead["lead"])["memo"]).read_text(
        encoding="utf-8"
    )
    assert "- 結果：維持\n" in text
    assert "満たされなかった" not in text
