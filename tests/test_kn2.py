"""Skill の提案の例（test_plan.md の KN-2。P0-4 の完了条件。D-045 で補強）。

.claude/skills/investigate/record-examples.md の例（run_config を除く）を、合成データの台帳に合わせて inbox/ に置き、
CLI の record ですべて登録できることを確かめる。

- 例の ID のつながりを保つ：例が作る ID（record-examples.md の冒頭の「例の ID」）を、その例を登録したときに CLI
  が返した ID に1対1で置き換える。どの例でも作られていない計算・候補・主張・対応の ID を参照する例があれば失敗
  にする（候補の calc_to と残る差の比較対象が別の ID だった不整合を、同じ ID に置き換えて隠さないため。D-045）。
- 金額の候補（探索の候補 L0003）と金額以外の候補（検出の候補 L0005）のそれぞれで、例の判定（説明を反映した値と
  残る差）を record → check → render まで通す。例にない必須の中身（ほかの型の判定、係数の提案を記者が不要と判断
  した後の登録など）はこのテストで足す。
- lead の例で探索の候補ができる。explanation に T6a・T6b を書くと拒否される。
- coefficient の例の手がかりの引用が references/ の原本（知識ファイル）にある。
- run_config の2つの例が CF-1 の検査を通る。
- 登録した後に inspect（実行ID、候補ID、資料ID、row_ref、計算ID、主張ID）を呼び、候補IDに同種業務の対応と
  係数の提案と判定の残る差（D-045③）が出ること、row_ref に take のキーと見出しが出ることを確かめる。

置き換え：例の値は合成の例で、合成データ（tests/fixtures/make_cao_fixtures.py）にはない。金額の候補は翻訳業務
（令和6年度 S001:000012 の 650,000円、令和7年度 S005:000007 の 750,000円）、金額以外の候補は S3 の検出の候補
「調査研究委託」（令和6年度 S003:000001 → 令和7年度 S007:000001。令和7年度の行の理由の分類は企画競争）に合わせる。
資料は、合成の仕様書（tests/fixtures/evidence/spec_sample.txt。令和6年度の数量 1,000件が2ページ、令和7年度の数量
1,200件が4ページ）を S010、合成の公表元の告知（HTML）を S011 として取得する。実行には検出（detect）の候補 L0001〜
L0016 と計算 K0009〜K0080 があるので、探索の候補は L0017、record の計算は K0081 から。

期待値は手で計算した：増減率 (750,000−650,000)÷650,000、差額 100,000、数量の比 1,200÷1,000＝1.2、数量の比で
補正した令和6年度の合計 650,000×1.2＝780,000、残る差 750,000−780,000＝−30,000。金額以外の候補の残る行は、令和7年度の
候補の行（S007:000001。企画競争）のうち企画競争によらない行なので0件。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

from conftest import REPO, Site, evidence_pdf_bytes, ledger_of, rec
from kizashi import cli, fetcher, query
from kizashi.adapters import load_adapter
from kizashi.runner import validate_run_config

EXAMPLES = REPO / ".claude" / "skills" / "investigate" / "record-examples.md"
SPEC_URL = "https://www.cao.go.jp/test/spec.pdf"
NOTICE_URL = "https://www.cao.go.jp/test/notice.html"
NOTICE_TEXT = "令和7年度から、調査研究の委託は企画競争により相手方を選ぶこととした。"

# 例が作る ID（record-examples.md の冒頭の「例の ID」）。ID を返す種類の例（calc・lead・derive・doc_value・claim）を
# ファイルの順に並べたもの。K0020 は calc の例の fiscal_year を 2024 にした計算で、最初に登録する。
CREATED_BY_EXAMPLES = [
    "K0021",  # calc：令和7年度の契約金額の合計（候補 L0003 の calc_to）
    "L0003",  # lead：探索の候補
    "K0022",  # derive：増減率
    "K0023",  # derive：差額
    "K0024",  # doc_value：令和7年度の数量
    "K0025",  # doc_value：令和6年度の数量
    "C0006",  # claim：observation
    "C0007",  # claim：record
    "K0026",  # derive：数量の比
    "K0027",  # derive：数量の比で補正した令和6年度の合計（説明を反映した値）
    "K0028",  # derive：残る差（K0021 との diff）
    "K0040",  # calc：金額以外の候補の、説明の後に残る行の件数
]
CREATING_KINDS = ("calc", "lead", "derive", "doc_value", "claim")
# 台帳の ID の形（実装仕様3.3：row_ref、資料 S＋3桁、計算 K・候補 L・主張 C＋4桁、対応 M＋3桁）。前後が英数字で
# ないもの。知識ファイルの番号（ノウハウ集の C024・M10 など）は桁が違うので当たらない
ID_RE = re.compile(r"(?<![A-Za-z0-9])(S\d{3}:\d{6}|S\d{3}|[KLC]\d{4}|M\d{3})(?![A-Za-z0-9])")
# 例の値のうち、合成データに合わせて替える ID 以外の文字列
TEXT_MAPPING = [
    ("荷物等の配送業務", "翻訳業務"),
    ("本業務は、…の配送を行うものである。", "本業務は、荷物等の配送を行うものである。"),
    ("7,793,940", "650,000"),
]


def examples() -> list[tuple[str, dict]]:
    """(見出し, JSON) の一覧。見出しは直前の「## 」の行。"""
    text = EXAMPLES.read_text(encoding="utf-8")
    found: list[tuple[str, dict]] = []
    heading = ""
    for block in re.split(r"(?m)^(## .*)$", text):
        if block.startswith("## "):
            heading = block[3:].strip()
            continue
        for body in re.findall(r"```json\n(.*?)\n```", block, flags=re.DOTALL):
            found.append((heading, json.loads(body)))
    return found


def cli_record(repo: Path, run_id: str, data: dict, capsys, name: str) -> tuple[int, dict]:
    (repo / "inbox" / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    code = cli.main(["record", run_id, f"inbox/{name}"])
    out, _ = capsys.readouterr()
    return code, json.loads(out)


def cli_ok(argv: list[str], capsys) -> dict:
    code = cli.main(argv)
    out, _ = capsys.readouterr()
    assert code == 0, out
    return json.loads(out)["data"]


def cli_inspect(target: str, run_id: str, capsys) -> dict:
    argv = ["inspect", target] if target == run_id else ["inspect", target, "--run", run_id]
    return cli_ok(argv, capsys)


def substitute(data: dict, ids: dict[str, str], where: str) -> dict:
    """例の文字列と ID を合成データのものに替える。ID は1回の置き換えで、表にない ID は失敗にする。"""
    text = json.dumps(data, ensure_ascii=False)
    for old, new in TEXT_MAPPING:
        text = text.replace(old, new)

    def swap(match: re.Match) -> str:
        found = match.group(1)
        assert found in ids, f"{where} の {found} を作る例がない（例の ID のつながりが切れている）"
        return ids[found]

    return json.loads(ID_RE.sub(swap, text))


def value_of(repo: Path, run_id: str, calc_id: str) -> str:
    conn = ledger_of(repo, run_id)
    (value,) = conn.execute(
        "SELECT value FROM computations WHERE calc_id = ?", (calc_id,)
    ).fetchone()
    conn.close()
    return value


def test_kn2_run_config_examples_pass_cf1(repo: Path) -> None:
    """KN-2：run_config の2つの例が CF-1 の検査（validate_run_config）を通る。"""
    adapter = load_adapter(repo, "cao-procurement")
    configs = [d for _, d in examples() if d["kind"] == "run_config"]
    assert len(configs) == 2
    for config in configs:
        validate_run_config(config, adapter)


def _fetch_evidence(repo: Path, run_id: str, site: Site, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        fetcher, "make_client", lambda transport=None: httpx.Client(transport=site.transport)
    )
    site.routes[SPEC_URL] = httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=evidence_pdf_bytes("spec_sample")
    )
    site.routes[NOTICE_URL] = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=f"<html><body><p>{NOTICE_TEXT}</p></body></html>".encode(),
    )
    assert cli_ok(["fetch", run_id, SPEC_URL], capsys)["source_id"] == "S010"
    assert cli_ok(["fetch", run_id, NOTICE_URL], capsys)["source_id"] == "S011"


def _record_examples(repo: Path, run_id: str, capsys, ids: dict[str, str]) -> dict[str, dict]:
    """run_config を除く例をファイルの順に登録し、例が作る ID を ids に足す。種類ごとの最初の出力を返す。"""
    items = [(h, d) for h, d in examples() if d["kind"] != "run_config"]
    assert [d["kind"] for _, d in items] == [
        "calc", "lead", "derive", "derive", "doc_value", "doc_value", "hypothesis", "hypothesis",
        "match", "claim", "claim", "derive", "derive", "derive", "explanation", "calc",
        "explanation", "question", "question", "coefficient", "conclusion", "withdraw",
    ]  # fmt: skip

    # 候補 L0003 の calc_from（K0020）：calc の例の fiscal_year を 2024 にしたもの
    first_calc = items[0][1]
    data = substitute(first_calc, ids, "calc")
    data["filter"] = [
        f if f["col"] != "fiscal_year" else {**f, "value": 2024} for f in data["filter"]
    ]
    code, out = cli_record(repo, run_id, data, capsys, "pre-2024.json")
    assert code == 0, out
    ids["K0020"] = out["data"]["id"]

    expected = list(CREATED_BY_EXAMPLES)
    first: dict[str, dict] = {}
    for n, (heading, example) in enumerate(items):
        data = substitute(example, ids, f"例「{heading}」の {example['kind']}")
        code, out = cli_record(repo, run_id, data, capsys, f"ex-{n:02d}.json")
        assert code == 0, (heading, out)
        assert out["ok"] is True and "warnings" not in out, (heading, out)
        assert not (repo / "inbox" / f"ex-{n:02d}.json").exists()
        first.setdefault(example["kind"], out["data"])
        if example["kind"] in CREATING_KINDS:
            doc_id, actual = expected.pop(0), out["data"]["id"]
            assert doc_id not in ids and actual not in ids.values(), (doc_id, actual)
            ids[doc_id] = actual
    assert expected == [], f"例が作るはずの ID が作られていない：{expected}"
    return first


def _complete_amount_lead(repo: Path, run_id: str, ids: dict[str, str]) -> None:
    """金額の候補（L0003）に、例にない必須の中身を足す：T1・T2・T4・T5・T6c の判定と、係数の提案を記者が不要と
    判断した後の登録（例の coefficient の key で declined にする）。"""
    lid = ids["L0003"]
    for etype in ("T1", "T4", "T5", "T6c"):
        rec(
            repo,
            run_id,
            {
                "kind": "explanation",
                "lead_id": lid,
                "type": etype,
                "judgment": "unknown",
                "evidence": [],
                "searched": "公表ページと公表元の告知を探した。",
            },
        )
    rec(
        repo,
        run_id,
        {
            "kind": "explanation",
            "lead_id": lid,
            "type": "T2",
            "judgment": "refute",
            "evidence": [
                {
                    "source_id": "S001",
                    "position": {"row_ref": "S001:000012", "column": "contract_date_raw"},
                    "quote": "R7.3.31",
                    "role": "background",
                }
            ],
            "searched": "両年度の契約を締結した日を確かめた。",
        },
    )
    coefficient = next(d for _, d in examples() if d["kind"] == "coefficient")
    declined = substitute(coefficient, ids, "coefficient")
    declined.update(
        {
            "status": "declined",
            "reporter_note": "記者の判断：両年度とも税込みの記載と確かめたので不要",
        }
    )
    rec(repo, run_id, declined)


def _complete_reason_lead(repo: Path, run_id: str, lead: dict) -> None:
    """金額以外の候補（L0005＝S3 の「調査研究委託」）に、例にない必須の中身を足す（説明の状況は「全部」）。"""
    lid = lead["lead_id"]
    hypothesis = {
        "kind": "hypothesis",
        "lead_id": lid,
        "text": "相手方の選び方の規則が変わった可能性がある。",
        "withdraw_if": "令和7年度も公募で選んでいたと分かった場合。",
    }
    rec(repo, run_id, hypothesis)
    rec(repo, run_id, {**hypothesis, "result": "kept"})
    rec(
        repo,
        run_id,
        {
            "kind": "match",
            "lead_id": lid,
            "left": ["S003:000001"],
            "right": ["S007:000001"],
            "relation": "1:1",
            "reason": "件名と業務の内容が同じ。",
            "evidence": [],
            "status": "confirmed",
        },
    )
    rec(
        repo,
        run_id,
        {
            "kind": "claim",
            "lead_id": lid,
            "type": "observation",
            "text": f"候補の行は令和6年度が{{calc:{lead['calc_from']}}}、令和7年度が{{calc:{lead['calc_to']}}}である。",
            "calc_ids": [lead["calc_from"], lead["calc_to"]],
            "evidence": [],
        },
    )
    for etype in ("T3", "T4", "T5", "T6c"):
        rec(
            repo,
            run_id,
            {
                "kind": "explanation",
                "lead_id": lid,
                "type": etype,
                "judgment": "unknown",
                "evidence": [],
                "searched": "公表ページと公表元の告知を探した。",
            },
        )
    rec(
        repo,
        run_id,
        {
            "kind": "explanation",
            "lead_id": lid,
            "type": "T2",
            "judgment": "refute",
            "evidence": [
                {
                    "source_id": "S003",
                    "position": {"row_ref": "S003:000001", "column": "title_raw"},
                    "quote": "調査研究委託",
                    "role": "background",
                }
            ],
            "searched": "両年度の件名を確かめた。",
        },
    )
    rec(
        repo,
        run_id,
        {
            "kind": "question",
            "lead_id": lid,
            "to": "所管部署",
            "question": "公表資料にある令和7年度のこの契約の随意契約の理由は、契約の決裁の記載と同じか。",
            "documents": "令和7年度の随意契約の理由書",
            "decision_impact": "記載が違えば、比較成立を取り消す。",
            "purposes": ["verify_record"],
        },
    )
    rec(
        repo,
        run_id,
        {
            "kind": "conclusion",
            "lead_id": lid,
            "outcome": "change",
            "comparison_state": "comparable",
            "explanation_status": "full",
        },
    )


def test_kn2_all_examples_can_be_recorded(
    repo: Path, synthetic_run: str, site: Site, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KN-2：run_config を除くすべての例を、例の ID のつながりを保ったまま record で登録でき、金額・金額以外の
    候補の説明の例が check に合格し、render がメモを書く（値は合成データに合わせて直す）。"""
    run_id = synthetic_run
    shutil.copytree(REPO / "references", repo / "references")  # 知識ファイルの原本（読むだけ）
    _fetch_evidence(repo, run_id, site, capsys, monkeypatch)

    # 探索SQL（lead の例の query_file）を実行しておく
    (repo / "inbox" / "q-002.sql").write_text(
        "SELECT fiscal_year, amount_yen FROM procurement_contracts WHERE group_key = '翻訳業務'",
        encoding="utf-8",
    )
    query.run_query(repo, run_id, "inbox/q-002.sql")
    conn = ledger_of(repo, run_id)
    reason_lead = dict(
        conn.execute(
            "SELECT * FROM leads WHERE screen_id = 's3_reason_change' AND lead_key = '調査研究委託'"
        ).fetchone()
    )
    conn.close()

    # 例の資料・行・検出の候補 → 合成データ（例が作らない ID）
    ids = {
        "S004": "S001",
        "S006": "S005",
        "S012": "S010",
        "S013": "S011",
        "S004:000001": "S001:000012",
        "S006:000003": "S005:000007",
        "L0005": reason_lead["lead_id"],
    }
    first = _record_examples(repo, run_id, capsys, ids)
    assert [ids[k] for k in ("K0020", "K0021", "L0003")] == ["K0081", "K0082", "L0017"]

    # 手で計算した値
    expected_values = {
        "K0020": "650000",
        "K0021": "750000",
        "K0022": "0.1538461538461538461538461538",
        "K0023": "100000",
        "K0024": "1200",
        "K0025": "1000",
        "K0026": "1.2",
        "K0027": "780000.0",
        "K0028": "-30000.0",
        "K0040": "0",
    }
    assert {k: value_of(repo, run_id, ids[k]) for k in expected_values} == expected_values

    conn = ledger_of(repo, run_id)
    lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (ids["L0003"],)).fetchone()
    assert (lead["screen_id"], lead["query_file"], lead["calc_from"], lead["calc_to"]) == (
        "explore",
        "q-002.sql",
        ids["K0020"],
        ids["K0021"],
    )
    assert (lead["proposed_outcome"], lead["hypothesis_result"]) == ("change", "kept")
    claims = dict(conn.execute("SELECT claim_id, state FROM claims"))
    assert claims == {ids["C0006"]: "draft", ids["C0007"]: "withdrawn"}
    conn.close()
    assert first["doc_value"]["value"] == "1200"

    # explanation に T6a・T6b を書くと拒否される（提案は inbox/ に残る）
    explanation = next(d for _, d in examples() if d["kind"] == "explanation")
    for t in ("T6a", "T6b"):
        data = substitute(explanation, ids, "explanation")
        data.update({"type": t, "judgment": "refute"})
        for key in ("recalc_calc_id", "residual_calc_id", "scope"):
            data.pop(key, None)
        code, out = cli_record(repo, run_id, data, capsys, f"t6-{t}.json")
        assert code == 1 and out["error"]["code"] == "E_SELF_DECLARED"
        assert (repo / "inbox" / f"t6-{t}.json").exists()
        (repo / "inbox" / f"t6-{t}.json").unlink()

    # 金額の候補：説明を反映した値（K0027）と、候補の calc_to（K0021）との diff の残る差（K0028）で合格する
    _complete_amount_lead(repo, run_id, ids)
    result = cli_ok(["check", ids["L0003"], "--run", run_id], capsys)
    assert result["passed"] is True and result["t6"] == {"T6a": "refute", "T6b": "refute"}
    memo = (repo / cli_ok(["render", ids["L0003"], "--run", run_id], capsys)["memo"]).read_text(
        encoding="utf-8"
    )
    assert (
        f"説明を反映した再計算：780,000円（配送数量の比で補正した令和6年度の契約金額の合計。計算{ids['K0027']}）"
        in memo
    )
    assert f"計算{ids['K0028']}）" in memo
    assert (
        f"引き算の向き：{ids['K0021']} から {ids['K0027']} を引いた値（diff({ids['K0021']}, {ids['K0027']})）。"
        f"比較対象：{ids['K0021']}。単位：円"
    ) in memo

    # 金額以外の候補：説明の後に残る行の件数（K0040）を説明を反映した値と残る差の両方にし、足した条件を書く
    _complete_reason_lead(repo, run_id, reason_lead)
    result = cli_ok(["check", ids["L0005"], "--run", run_id], capsys)
    assert result["passed"] is True
    memo = (repo / cli_ok(["render", ids["L0005"], "--run", run_id], capsys)["memo"]).read_text(
        encoding="utf-8"
    )
    assert "説明によって足した・変えた条件：reason_category != planning_competition" in memo
    assert "条件をそろえた後に残る行：なし（単位：件）" in memo

    # inspect（実行ID、候補ID、資料ID、row_ref、計算ID、主張ID）
    run = cli_inspect(run_id, run_id, capsys)
    assert run["leads"]["total"] == 17 and run["counts"]["explore"] == {"compared": 1}
    lead_view = cli_inspect(ids["L0003"], run_id, capsys)
    assert [(m["left"], m["right"], m["status"]) for m in lead_view["matches"]] == [
        (["S001:000012"], ["S005:000007"], "confirmed")
    ]
    assert [(c["key"], c["status"]) for c in lead_view["coefficients"]] == [
        ("tax_basis", "declined")
    ]
    t3 = next(e for e in lead_view["explanations"] if e["type"] == "T3")
    assert (t3["recalc_calc_id"], t3["residual_calc_id"]) == (ids["K0027"], ids["K0028"])
    t1 = next(
        e for e in cli_inspect(ids["L0005"], run_id, capsys)["explanations"] if e["type"] == "T1"
    )
    assert (t1["recalc_calc_id"], t1["residual_calc_id"], t1["reflected_conditions"]) == (
        ids["K0040"],
        ids["K0040"],
        [{"col": "reason_category", "op": "!=", "value": "planning_competition"}],
    )
    assert cli_inspect("S010", run_id, capsys)["page_count"] == 4
    row = cli_inspect("S005:000007", run_id, capsys)
    assert {
        "key": "method_raw",
        "header": "一般競争入札・指名競争入札の別(総合評価の実施)",
        "value": "一般競争",
    } in row["cells"]
    assert cli_inspect(ids["K0021"], run_id, capsys)["value"] == "750000"
    assert cli_inspect(ids["C0007"], run_id, capsys)["type"] == "record"
