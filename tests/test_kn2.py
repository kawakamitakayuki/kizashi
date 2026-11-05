"""Skill の提案の例（test_plan.md の KN-2。P0-4 の完了条件）。

.claude/skills/investigate/record-examples.md の例（run_config を除く）を、合成データの台帳に合わせて ID と値を
置き換えて inbox/ に置き、CLI の record ですべて登録できることを確かめる。

- lead の例で探索の候補ができる。explanation に T6a・T6b を書くと拒否される。
- coefficient の例の手がかりの引用が references/ の原本（知識ファイル）にある。
- run_config の2つの例が CF-1 の検査を通る。
- 登録した後に inspect（実行ID、候補ID、資料ID、row_ref、計算ID、主張ID）を呼び、候補IDに同種業務の対応と
  係数の提案が出ること、row_ref に take のキーと見出しが出ることを確かめる。

置き換え：例の値は合成の例で、合成データ（tests/fixtures/make_cao_fixtures.py）にはない。翻訳業務
（令和6年度 S001:000012 の 650,000円、令和7年度 S005:000007 の 750,000円）と、合成の仕様書
（tests/fixtures/evidence/spec_sample.txt を追加資料として取得した S010）に合わせる。実行には検出（detect）の
候補 L0001〜L0016 と計算 K0009〜K0080 があるので、探索の候補は L0017、record の計算は K0081 から。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

from conftest import REPO, Site, evidence_pdf_bytes, ledger_of
from kizashi import cli, fetcher, query
from kizashi.adapters import load_adapter
from kizashi.runner import validate_run_config

EXAMPLES = REPO / ".claude" / "skills" / "investigate" / "record-examples.md"
SPEC_URL = "https://www.cao.go.jp/test/spec.pdf"


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


def cli_inspect(target: str, run_id: str, capsys) -> dict:
    argv = ["inspect", target] if target == run_id else ["inspect", target, "--run", run_id]
    code = cli.main(argv)
    out, _ = capsys.readouterr()
    assert code == 0, out
    return json.loads(out)["data"]


def substitute(data: dict, mapping: list[tuple[str, str]]) -> dict:
    text = json.dumps(data, ensure_ascii=False)
    for old, new in mapping:
        text = text.replace(old, new)
    return json.loads(text)


def test_kn2_run_config_examples_pass_cf1(repo: Path) -> None:
    """KN-2：run_config の2つの例が CF-1 の検査（validate_run_config）を通る。"""
    adapter = load_adapter(repo, "cao-procurement")
    configs = [d for _, d in examples() if d["kind"] == "run_config"]
    assert len(configs) == 2
    for config in configs:
        validate_run_config(config, adapter)


def test_kn2_all_examples_can_be_recorded(
    repo: Path, synthetic_run: str, site: Site, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KN-2：run_config を除くすべての例を record で登録できる（値は合成データに合わせて直す）。"""
    run_id = synthetic_run
    shutil.copytree(REPO / "references", repo / "references")  # 知識ファイルの原本（読むだけ）
    monkeypatch.setattr(
        fetcher, "make_client", lambda transport=None: httpx.Client(transport=site.transport)
    )
    site.routes[SPEC_URL] = httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=evidence_pdf_bytes("spec_sample")
    )
    assert cli.main(["fetch", run_id, SPEC_URL]) == 0
    assert json.loads(capsys.readouterr()[0])["data"]["source_id"] == "S010"

    items = [(h, d) for h, d in examples() if d["kind"] != "run_config"]
    kinds = [d["kind"] for _, d in items]
    assert kinds == [
        "calc", "derive", "derive", "doc_value", "lead", "hypothesis", "match", "claim", "claim",
        "explanation", "question", "coefficient", "conclusion", "withdraw",
    ]  # fmt: skip
    by_heading = {h: d for h, d in items}

    # 探索SQL（例の query_file）を実行し、候補の両年度の計算を、calc の例の形で先に登録する
    (repo / "inbox" / "q-002.sql").write_text(
        "SELECT fiscal_year, amount_yen FROM procurement_contracts WHERE group_key = '翻訳業務'",
        encoding="utf-8",
    )
    query.run_query(repo, run_id, "inbox/q-002.sql")
    calc_example = by_heading["calc"]
    ids: dict[str, str] = {}
    for year, label in ((2024, "K_FROM"), (2025, "K_TO")):
        data = {k: v for k, v in calc_example.items() if k != "lead_id"}
        data = substitute(data, [("荷物等の配送業務", "翻訳業務")])
        data["filter"] = [
            f if f["col"] != "fiscal_year" else {**f, "value": year} for f in data["filter"]
        ]
        code, out = cli_record(repo, run_id, data, capsys, f"pre-{year}.json")
        assert code == 0, out
        ids[label] = out["data"]["id"]
    assert [ids["K_FROM"], ids["K_TO"]] == ["K0081", "K0082"]

    mapping = [
        ("荷物等の配送業務", "翻訳業務"),
        ("本業務は、…の配送を行うものである。", "本業務は、荷物等の配送を行うものである。"),
        ("L0003", "L0017"),
        ("K0030", ids["K_FROM"]),
        ("K0031", ids["K_TO"]),
        ("K0020", ids["K_FROM"]),
        ("K0021", ids["K_TO"]),
        ("S004:000001", "S001:000012"),
        ("S006:000003", "S005:000007"),
        ('"S004"', '"S001"'),
        ('"S006"', '"S005"'),
        ("S012", "S010"),
        ("7,793,940", "650,000"),
    ]
    order = [
        "lead",
        *[k for k in kinds if k != "lead"],
    ]  # 候補を先に作る（ほかの例が候補を参照する）
    pending = list(items)
    recorded: dict[str, dict] = {}
    for kind in order:
        heading, data = next((h, d) for h, d in pending if d["kind"] == kind)
        pending.remove((heading, data))
        data = substitute(data, mapping)
        if kind == "explanation":
            data["recalc_calc_id"] = recorded["doc_value"]["id"]  # 例の K0024（数量の文書値）
        if kind == "withdraw":
            data["target"] = recorded["claim"]["id"]  # 例の C0007（最初の主張）
        code, out = cli_record(repo, run_id, data, capsys, f"{kind}.json")
        assert code == 0, (heading, out)
        assert out["ok"] is True and "warnings" not in out
        assert not (repo / "inbox" / f"{kind}.json").exists()
        recorded.setdefault(kind, out["data"])
    assert not pending

    conn = ledger_of(repo, run_id)
    lead = conn.execute("SELECT * FROM leads WHERE lead_id = 'L0017'").fetchone()
    assert (lead["screen_id"], lead["query_file"], lead["proposed_outcome"]) == (
        "explore",
        "q-002.sql",
        "change",
    )
    assert recorded["doc_value"]["value"] == "1200"
    assert recorded["derive"]["value"] == "0.1538461538461538461538461538"  # rate(750000, 650000)
    claims = dict(conn.execute("SELECT claim_id, state FROM claims"))
    assert claims == {"C0001": "withdrawn", "C0002": "draft"}

    # explanation に T6a・T6b を書くと拒否される（提案は inbox/ に残る）
    for t in ("T6a", "T6b"):
        data = substitute(
            by_heading[next(h for h in by_heading if h.startswith("explanation"))], mapping
        )
        data.update({"type": t, "judgment": "refute"})
        for key in ("recalc_calc_id", "scope"):
            data.pop(key, None)
        code, out = cli_record(repo, run_id, data, capsys, f"t6-{t}.json")
        assert code == 1 and out["error"]["code"] == "E_SELF_DECLARED"
        assert (repo / "inbox" / f"t6-{t}.json").exists()

    # inspect（実行ID、候補ID、資料ID、row_ref、計算ID、主張ID）
    run = cli_inspect(run_id, run_id, capsys)
    assert run["leads"]["total"] == 17 and run["counts"]["explore"] == {"compared": 1}
    lead_view = cli_inspect("L0017", run_id, capsys)
    assert [(m["left"], m["right"], m["status"]) for m in lead_view["matches"]] == [
        (["S001:000012"], ["S005:000007"], "confirmed")
    ]
    assert [(c["key"], c["status"]) for c in lead_view["coefficients"]] == [
        ("tax_basis", "proposed")
    ]
    assert cli_inspect("S010", run_id, capsys)["page_count"] == 4
    row = cli_inspect("S005:000007", run_id, capsys)
    assert {
        "key": "method_raw",
        "header": "一般競争入札・指名競争入札の別(総合評価の実施)",
        "value": "一般競争",
    } in row["cells"]
    assert cli_inspect(ids["K_TO"], run_id, capsys)["value"] == "750000"
    assert cli_inspect("C0002", run_id, capsys)["type"] == "record"
