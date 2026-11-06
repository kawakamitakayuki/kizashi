"""候補のメモ（implementation_spec.md 2章の render.py、4.7、10.1。設計書6章）。

kizashi render <lead_id> --run <run_id> は、1つのトランザクションの中で、
①検証（check と同じ V1〜V4・V8。巡回数に数えない）を行い、②合格したら主張を checked、leads.state を結論の
案の値にし、③その後の台帳から検証記録のハッシュを計算し（11章）、④メモを memos/<lead_id>.v<n>.md に書き
（n は1から。上書きしない）、⑤leads.memo_no と leads.memo_hash を更新してから確定する。確定に失敗したら書いた
メモを消す。不合格ならエラーを返し、何も書かない。

メモの数値は計算記録から「値・意味・計算ID」の形で埋め込む（{calc:ID} の展開）。Claude の文章は、主張・仮説・
判定・質問の本文として登録されたものだけを使う。表示の丸めは dataset.yaml の display（四捨五入）。
実行の概要（render <run_id>）と --public は P0-8。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path, PurePosixPath

import httpx

from kizashi import check, hashing, ledger, paths, versions
from kizashi.adapters import Adapter, load_adapter
from kizashi.errors import KizashiError
from kizashi.hashing import dict_rows

STAGE = "render"
HEADER_LINES = 4  # 表計算ソフトで開いたときの、論理レコードの前の行の数（10.1。目安）

OUTCOME_NAMES = {"change": "比較成立", "artifact": "見かけの変化", "hold": "保留"}
EXPLANATION_STATUS_NAMES = {"none": "なし", "part": "一部", "full": "全部", "unknown": "範囲不明"}
HOLD_REASON_NAMES = {
    "comparison": "比較の条件（同種業務の対応、期間・数量・対象）を公開資料で確定できない",
    "source_check": "原本照合ができない（照合不能）",
    "limit": "探索上限に達した",
}
JUDGMENT_NAMES = {"support": "支持", "refute": "否定", "unknown": "未確認", "na": "対象外"}
SCOPE_NAMES = {"full": "全部", "part": "一部", "unknown": "範囲不明"}
CLAIM_TYPE_NAMES = {
    "record": "原資料の記載",
    "observation": "観測",
    "publisher_explanation": "公表元の説明",
    "hypothesis": "仮説",
}
CLAIM_STATE_NAMES = {
    "draft": "下書き",
    "checked": "検査済み",
    "held": "保留",
    "stale": "失効",
    "withdrawn": "取り下げ",
}
COMPARISON_STATE_NAMES = {
    "comparable": "そろっている",
    "adjusted": "補正した",
    "unresolved": "確定できない",
    "not_applicable": "対象外",
}
COMPARISON_NAMES = {
    "population": "母集団",
    "row_meaning": "1行の意味",
    "period": "期間",
    "unit": "単位",
    "entity_mapping": "主体の対応",
    "formula": "算式",
}
HYPOTHESIS_RESULT_NAMES = {
    "kept": "維持（取り下げ条件は満たされなかった）",
    "withdrawn": "取り下げた",
}
COEF_STATUS_NAMES = {
    "proposed": "提案中",
    "accepted": "記者が使うと判断",
    "declined": "記者が不要と判断",
}
MATCH_STATUS_NAMES = {"confirmed": "確認済み", "pending": "確認待ち", "withdrawn": "取り下げ"}
RUN_STATE_NAMES = {
    "running": "実行中",
    "completed": "完了",
    "partial": "一部が欠けた",
    "failed": "失敗",
}
SOURCE_STATUS_NAMES = {
    "planned": "予定のまま",
    "fetched": "取得済み",
    "not_published": "非公表",
    "failed": "取得できなかった",
    "out_of_scope": "対象外",
}
ROLE_NAMES = {"support": "支える", "oppose": "反する", "background": "背景"}
FIELD_STATE_NAMES = {"match": "一致", "mismatch": "不一致", "unverifiable": "照合不能"}
PURPOSE_NAMES = {
    "verify_record": "元の記載の確認",
    "reason_breakdown": "理由・内訳の確認",
    "missing_documents": "不足資料の照会",
    "other": "そのほか",
}
FIXED_NOTICE = "調査用・記者未確認。調査の端緒であり、不正の指摘ではない。"
FIXED_CSV_PDF = "CSVとPDFの一致は元の記載の正しさを示さない。"
FIXED_SAME_CONTEXT = (
    "6つの型の判定と反証の点検は、調査と同じ会話の文脈で行った（独立した検証ではない）。"
    "独立した確認は、別経路の再集計（V3）と記者の原本の確認が担う。"
)


def _one(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    rows = dict_rows(conn, sql, params)
    return rows[0] if rows else None


def _year_ja(year: int | str) -> str:
    year = int(year)
    return f"令和{year - 2018}年度（{year}年度）" if year >= 2019 else f"{year}年度"


class _Memo:
    """1つの候補のメモを台帳から組み立てる。"""

    def __init__(
        self, conn: sqlite3.Connection, root: Path, run_id: str, adapter: Adapter, lead_id: str
    ) -> None:
        self.conn, self.root, self.run_id, self.adapter = conn, root, run_id, adapter
        self.lead = _one(conn, "SELECT * FROM leads WHERE lead_id = ?", (lead_id,)) or {}
        self.run = _one(conn, "SELECT * FROM runs", ()) or {}
        self.config = json.loads(self.run["config_json"])
        self.counts = json.loads(self.run["counts_json"])
        self.display = adapter.config.get("display") or {}
        self.claims = check.memo_claims(conn, lead_id)
        self.explanations = {
            r["type"]: r
            for r in dict_rows(conn, "SELECT * FROM explanations WHERE lead_id = ?", (lead_id,))
        }
        self.matches = dict_rows(
            conn,
            "SELECT * FROM procurement_matches WHERE lead_id = ? ORDER BY match_id",
            (lead_id,),
        )
        self.rows = check.candidate_rows(conn, lead_id)

    # -- 計算の表示 --------------------------------------------------------

    def calc(self, calc_id: str | None) -> dict | None:
        if not calc_id:
            return None
        return _one(self.conn, "SELECT * FROM computations WHERE calc_id = ?", (calc_id,))

    def number(self, value: str | None, unit: str | None) -> str:
        if value is None:
            return "（値なし）"
        spec = self.display.get(unit or "")
        if not spec:
            return f"{value}{unit or ''}"
        number = Decimal(value) * Decimal(str(spec.get("scale", 1)))
        places = int(spec.get("places", 0))
        number = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        text = f"{number:,}" if spec.get("thousands") else f"{number}"
        return text + str(spec.get("suffix", unit or ""))

    def calc_text(self, calc_id: str | None) -> str:
        """「7,793,940円（令和6年度の合計。計算K0012）」の形（10.1）。"""
        comp = self.calc(calc_id)
        if comp is None:
            return f"（計算 {calc_id} がない）"
        return f"{self.number(comp['value'], comp['unit'])}（{comp['meaning']}。計算{comp['calc_id']}）"

    def expand(self, text: str | None) -> str:
        return hashing.CALC_REF_RE.sub(lambda m: self.calc_text(m.group(1)), text or "")

    # -- 資料とレコード ----------------------------------------------------

    def source(self, source_id: str) -> dict:
        return _one(self.conn, "SELECT * FROM sources WHERE source_id = ?", (source_id,)) or {}

    def file_name(self, source: dict) -> str:
        url = source.get("final_url") or source.get("url") or ""
        try:
            return PurePosixPath(httpx.URL(url).path).name or url
        except httpx.InvalidURL:
            return url

    def record(self, record_id: str) -> dict:
        return _one(self.conn, "SELECT * FROM records WHERE record_id = ?", (record_id,)) or {}

    def pdf_page(self, record_id: str) -> tuple[str | None, int | None, dict]:
        for comp in dict_rows(
            self.conn,
            "SELECT calc_id, request_json, result_json FROM computations WHERE kind = 'reconcile'"
            " AND state = 'active' ORDER BY calc_id",
        ):
            request = json.loads(comp["request_json"])
            if request.get("type") != "source":
                continue
            entry = json.loads(comp["result_json"] or "{}").get("rows", {}).get(record_id)
            if entry is not None:
                return request.get("pdf_source"), entry.get("pdf_page"), entry
        return None, None, {}

    def spec_of(self, source: dict):
        key = source.get("logical_key") or ""
        if key.count("|") != 3:
            return None
        _, year, family, _ = key.split("|")
        return self.adapter.file_spec(int(year), family)

    # -- 節 ----------------------------------------------------------------

    def title(self) -> str:
        labels = getattr(self.adapter.module, "CATEGORY_LABELS", {}) or {}
        category = labels.get(self.lead["change_category"], self.lead["change_category"])
        return f"# {category}：{self.lead['lead_key']}"

    def section1(self) -> list[str]:
        lead = self.lead
        outcome = OUTCOME_NAMES.get(lead["state"], lead["state"])
        if lead["state"] == "change":
            outcome += f"（説明の状況：{EXPLANATION_STATUS_NAMES.get(lead['explanation_status'], '未記録')}）"
        elif lead["state"] == "hold":
            outcome += f"（理由：{HOLD_REASON_NAMES.get(lead['hold_reason'], '未記録')}）"
        reopen = (
            [f"- 再開の条件：{self.expand(lead['reopen_condition'])}"]
            if lead.get("reopen_condition")
            else []
        )
        years = [int(lead["period_from"]), int(lead["period_to"])]
        lines = [
            "## 1 状態と範囲",
            "",
            f"- 結論：{outcome}",
            *reopen,
            f"- 問い：{self.config.get('question', '（未記録）')}",
            (
                f"- 比べた年度：{_year_ja(years[0])}と{_year_ja(years[1])}"
                f"（系列として読んだ年度：{'・'.join(_year_ja(y) for y in self.config.get('series_years', []))}）"
            ),
            "- 収録範囲：公表元が公表した表に載る契約の範囲で、支出のすべてではない。母集団は2節の比較の条件のとおり。",
        ]
        scope = self.config.get("scope")
        if scope:
            conditions = "、".join(
                f"{f['field']} {f['op']} 「{f['value']}」" for f in scope.get("filters", [])
            )
            lines.append(
                f"- 母集団の絞り込み：記者の指示「{scope['instruction']}」。条件：{conditions}"
            )
        else:
            lines.append("- 母集団の絞り込み：なし")
        lines.append("- 資料：")
        for source in dict_rows(
            self.conn,
            "SELECT * FROM sources WHERE kind = 'dataset' ORDER BY source_id",
        ):
            key = source.get("logical_key") or ""
            if key.count("|") == 3 and int(key.split("|")[1]) in years:
                fetched = source.get("fetched_at") or "未取得"
                lines.append(
                    f"  - {source['source_id']}：{self.file_name(source)}（{key}。"
                    f"{SOURCE_STATUS_NAMES.get(source['status'], source['status'])}。"
                    f"取得 {fetched}）"
                )
        stop = self.run.get("stop_reason")
        lines += [
            f"- 実行ID：{self.run_id}（{RUN_STATE_NAMES.get(self.run['state'], self.run['state'])}"
            + (f"。止まった・欠けたもの：{stop}" if stop else "。途中で止まっていない")
            + "）",
            f"- 調査用の設定で実行：{'はい' if self.run.get('profile') == 'investigate' else 'いいえ'}",
            "",
        ]
        return lines

    def section2(self) -> list[str]:
        lead = self.lead
        lines = ["## 2 検出した変化", ""]
        if lead["screen_id"] == "explore":
            lines.append(f"- 見つけ方：探索SQL（{lead['query_file']}）")
            compared = (self.counts.get("explore") or {}).get("compared")
        else:
            lines.append(f"- 見つけ方：検出SQL {lead['screen_id']}")
            compared = (self.counts.get(lead["screen_id"]) or {}).get("compared")
        lines += [
            f"- 前の年度：{self.calc_text(lead['calc_from'])}",
            f"- 後の年度：{self.calc_text(lead['calc_to'])}",
        ]
        for comp in dict_rows(
            self.conn,
            "SELECT * FROM computations WHERE lead_id = ? AND kind = 'derived' AND state = 'active'"
            " ORDER BY calc_id",
            (lead["lead_id"],),
        ):
            request = json.loads(comp["request_json"])
            label = {"diff": "差", "rate": "増減率", "ratio": "比率"}.get(
                request.get("op"), "派生計算"
            )
            lines.append(f"- {label}：{self.calc_text(comp['calc_id'])}（式 {comp['formula']}）")
            if request.get("op") == "rate":
                lines.append(
                    f"  - 分子：後の年度と前の年度の差。分母：{self.calc_text(request['args'][1])}"
                )
        series = json.loads(lead["calc_ids_json"])
        if series:
            lines.append("- 年度の系列：")
            lines += [f"  - {self.calc_text(c)}" for c in series]
        if lead.get("note"):
            lines.append(f"- 注記：{self.expand(lead['note'])}")
        comparison = json.loads(lead["comparison_json"])
        state = COMPARISON_STATE_NAMES.get(lead["comparison_state"], "未記録")
        lines.append(f"- 比較の条件（{state}）：")
        lines += [
            f"  - {COMPARISON_NAMES[k]}：{self.expand(comparison.get(k, ''))}"
            for k in COMPARISON_NAMES
        ]
        reasons = json.loads(lead["incomparable_reasons_json"])
        lines.append(f"- 比較できない理由：{'・'.join(reasons) if reasons else 'なし'}")
        lines.append(
            f"- 探索した比較の数：{compared if compared is not None else '（記録なし）'}組"
            f"（{'探索SQL' if lead['screen_id'] == 'explore' else '検出SQL ' + lead['screen_id']}）"
        )
        peers = lead.get("peer_count")
        lines.append(
            "- 同じ区分の変化のほかの件数："
            + (
                f"{peers}件（同じ実行の検出の候補）"
                if peers is not None
                else "数えていない（探索の候補）"
            )
        )
        lines.append("")
        return lines

    def section3(self) -> list[str]:
        lines = ["## 3 照合済みの記載と計算", ""]
        checked = [c for c in self.claims if c["state"] == "checked"]
        if not checked:
            lines.append("- 検査済みの主張はない。")
        for claim in checked:
            year = f"。{claim['fiscal_year']}年度" if claim["fiscal_year"] else ""
            lines.append(f"### {claim['claim_id']}（{CLAIM_TYPE_NAMES[claim['type']]}{year}）")
            lines += ["", self.expand(claim["text"]), ""]
            for e in json.loads(claim["evidence_json"]):
                lines.append(f"- 根拠：{self.evidence_text(e)}")
            calc_ids = json.loads(claim["calc_ids_json"])
            if calc_ids:
                lines.append("- 計算：" + "、".join(calc_ids))
            lines.append("")
        lines += ["### 計算に加わったレコードと式", ""]
        for calc_id in hashing.memo_calc_ids(self.conn, self.lead["lead_id"]):
            comp = self.calc(calc_id)
            if comp is None or comp["kind"] == "reconcile":
                continue
            lines.append(f"- {self.calc_text(calc_id)}")
            request = json.loads(comp["request_json"])
            if comp["kind"] == "derived":
                lines.append(f"  - 式：{comp['formula']}")
            elif comp["kind"] == "doc_value":
                lines.append(f"  - 文書値：{self.evidence_text({**request, 'role': 'support'})}")
            else:
                filters = "、".join(
                    f"{f['col']} {f['op']} {f.get('value')}" for f in request.get("filter", [])
                )
                op = (
                    f"{request.get('op')}（{request.get('column')}）"
                    if request.get("column")
                    else request.get("op")
                )
                lines.append(f"  - 式：{op}。条件：{filters}。行の数 {comp['row_count']}")
                if request.get("excluded"):
                    dropped = "、".join(f"{e['ref']}（{e['reason']}）" for e in request["excluded"])
                    lines.append(f"  - 合計から除いた行：{dropped}")
                for ref in json.loads(comp["row_refs_json"] or "[]"):
                    lines.append(f"  - {self.row_line(ref)}")
        lines += ["", "### 資料の一覧", ""]
        lines.append(
            "引用・行・照合の根拠の資料IDは、この一覧で引く（検証が依存する資料のすべて）。"
        )
        lines.append("")
        for source_id in hashing.dependency_sources(self.conn, self.lead["lead_id"], self.adapter):
            lines.append(self.source_line(source_id))
        lines.append("")
        return lines

    def source_line(self, source_id: str) -> str:
        source = self.source(source_id)
        if not source:
            return f"- {source_id}：（台帳にない）"
        path = check.raw_path(self.root, source)
        where = path.relative_to(self.root).as_posix() if path is not None else "（原本がない）"
        kind = {
            "dataset": "公表データ",
            "evidence": "追加資料",
            "definition": "定義の資料",
            "index": "索引",
        }
        return (
            f"- {source_id}：{kind.get(source['kind'], source['kind'])}。{self.file_name(source)}。"
            f"URL {source.get('final_url') or source.get('url')}。取得 {source.get('fetched_at')}。"
            f"保存場所 {where}。SHA-256 {source.get('sha256')}"
        )

    def evidence_text(self, e: dict) -> str:
        if "calc_id" in e:
            return f"照合の計算 {e['calc_id']}"
        position = e.get("position") or {}
        if "page" in position:
            where = f"{position['page']}ページ"
        else:
            where = f"{position.get('row_ref')} の {position.get('column')}"
        role = ROLE_NAMES.get(e.get("role"), e.get("role"))
        return f"{e.get('source_id')} の {where}「{e.get('quote')}」（根拠の役割：{role}）"

    def composite_key(self, record_id: str) -> str:
        cells = json.loads(self.record(record_id).get("cells_json") or "{}")
        keys = ("title_raw", "contract_date_raw", "amount_raw", "counterparty_raw")
        return "／".join(str(cells.get(k, "")) for k in keys)

    def row_line(self, record_id: str) -> str:
        source = self.source(record_id.split(":", 1)[0])
        _, page, _ = self.pdf_page(record_id)
        return (
            f"{record_id}：{self.file_name(source)}（{source.get('url')}。取得 {source.get('fetched_at')}）"
            f"。複合キー {self.composite_key(record_id)}。PDF {page if page is not None else '（対応なし）'}ページ"
        )

    def section4(self) -> list[str]:
        lead = self.lead
        lines = ["## 4 説明の検討", ""]
        coefs = json.loads(lead["coefficients_json"])
        if coefs:
            lines.append("### 補正の係数の提案（推論・未適用）")
            lines.append("")
            for c in coefs:
                lines.append(
                    f"- {c['key']}：{c['value']}（{self.expand(c['meaning'])}。"
                    f"状態 {COEF_STATUS_NAMES.get(c['status'], c['status'])}）。"
                    f"必要と考えた理由：{self.expand(c['why_needed'])}。使い方：{self.expand(c['apply_to'])}"
                    + (
                        f"。記者の判断：{self.expand(c['reporter_note'])}"
                        if c.get("reporter_note")
                        else ""
                    )
                )
                for b in c.get("basis", []):
                    where = (
                        f"{b.get('file')} {b.get('locator')}"
                        if "file" in b
                        else self.evidence_text(b)
                    )
                    lines.append(f"  - 手がかり：{where}「{b.get('quote', '')}」")
            lines.append("")
        lines.append("### 6つの型の判定")
        lines.append("")
        for t in check.TYPES:
            exp = self.explanations.get(t)
            name = f"{t} {check.TYPE_NAMES[t]}"
            if exp is None:
                lines.append(f"- {name}：判定なし")
                continue
            who = "（kizashi が照合の記録から判定）" if exp["context"] == "cli" else ""
            lines.append(f"- {name}：{JUDGMENT_NAMES[exp['judgment']]}{who}")
            for e in json.loads(exp["evidence_json"]):
                lines.append(f"  - 根拠：{self.evidence_text(e)}")
            if exp.get("searched"):
                lines.append(f"  - 探した範囲・記録：{self.expand(exp['searched'])}")
            if exp.get("scope"):
                lines.append(f"  - 説明が及ぶ範囲：{SCOPE_NAMES[exp['scope']]}")
            if exp.get("recalc_calc_id"):
                lines.append(f"  - 説明を反映した再計算：{self.calc_text(exp['recalc_calc_id'])}")
            if exp.get("residual_calc_id"):
                lines.append(f"  - 残る差：{self.calc_text(exp['residual_calc_id'])}")
                lines += self.residual_detail(exp)
            if exp.get("no_quant_reason"):
                lines.append(f"  - 定量化できない理由：{self.expand(exp['no_quant_reason'])}")
        if lead.get("adjustment_json"):
            adjustment = json.loads(lead["adjustment_json"])
            lines += [
                "",
                f"- 補正の方法：{self.expand(adjustment.get('method'))}",
                f"  - 補正前：{self.calc_text(adjustment.get('before'))}",
                f"  - 補正後：{self.calc_text(adjustment.get('after'))}",
            ]
        lines += [
            "",
            "### 仮説と取り下げ条件",
            "",
            f"- 仮説：{self.expand(lead['hypothesis']) or '（未記録）'}",
            f"- 取り下げ条件：{self.expand(lead['withdraw_if']) or '（未記録）'}",
            f"- 結果：{HYPOTHESIS_RESULT_NAMES.get(lead['hypothesis_result'], '未記録')}",
            "",
            FIXED_SAME_CONTEXT,
            "",
        ]
        return lines

    def residual_detail(self, exp: dict) -> list[str]:
        """残る差の比較対象と引き算の向き（金額の候補）、または対象の年度と行（金額以外の候補）。D-042③。"""
        residual = self.calc(exp["residual_calc_id"]) or {}
        if residual.get("kind") == "derived":
            args = json.loads(residual["request_json"]).get("args") or []
            other = check.residual_comparison(self.conn, self.lead, exp)
            if len(args) == 2:
                return [
                    (
                        f"    - 引き算の向き：{args[0]} から {args[1]} を引いた値（{residual['formula']}）。"
                        f"比較対象：{other or '（規則に合う比較対象がない）'}。単位：{residual['unit']}"
                    )
                ]
            return []
        if residual.get("kind") == "base":
            rows = "・".join(json.loads(residual["row_refs_json"] or "[]")) or "なし"
            return [f"    - 条件をそろえた後に残る行：{rows}（単位：{residual['unit']}）"]
        return []

    def section5(self) -> list[str]:
        lead = self.lead
        reasons = json.loads(lead["incomparable_reasons_json"])
        lines = [
            "## 5 言えないこと",
            "",
            f"- 比較できない点：{'・'.join(reasons) if reasons else '検出の記録にはない'}",
            "- 母集団の限界：公表された表に載る契約だけを比べた。表に載らない支出は比べていない。",
            "- 原因・違反・故意は立証していない。説明は誰の説明かを示した範囲にとどまる。",
        ]
        unchecked = [c for c in self.claims if c["state"] != "checked"]
        for claim in unchecked:
            lines.append(
                f"- 検査済みでない主張 {claim['claim_id']}（{CLAIM_STATE_NAMES[claim['state']]}）："
                f"{self.expand(claim['text'])}"
            )
        if lead.get("hypothesis"):
            lines.append(f"- 仮説（立証していない）：{self.expand(lead['hypothesis'])}")
        if lead.get("hold_reason") == "limit":
            lines.append("- 探索上限に達して中断した。調べていない範囲に変化がないとは言えない。")
        lines += [f"- {FIXED_CSV_PDF}", ""]
        return lines

    def section6(self) -> list[str]:
        lines = ["## 6 次の取材質問", ""]
        questions = json.loads(self.lead["questions_json"])
        if not questions:
            lines.append("- 取材質問はない。")
        for i, q in enumerate(questions, 1):
            lines += [
                f"{i}. 相手：{self.expand(q['to'])}",
                f"   - 問い：{self.expand(q['question'])}",
                f"   - 目的：{'・'.join(PURPOSE_NAMES.get(x, x) for x in q.get('purposes') or []) or '未記録'}",
                f"   - 必要な資料：{self.expand(q['documents'])}",
                f"   - 何が分かれば判断が変わるか：{self.expand(q['decision_impact'])}",
            ]
            if q.get("foia"):
                lines.append(f"   - 情報公開請求の候補：{self.expand(q['foia'])}")
        lines += ["", "連絡は記者が行う。", ""]
        return lines

    def section7(self) -> list[str]:
        lead = self.lead
        lines = ["## 7 記者の確認リスト", ""]
        coefs = [c for c in json.loads(lead["coefficients_json"]) if c.get("status") != "declined"]
        if coefs:
            lines.append("### 記者が判断すること（補正の係数の提案。推論・未適用）")
            lines += [f"- {c['key']}：{c['value']}（{c['meaning']}）を使うか" for c in coefs]
            lines.append("")
        fields = list(
            ((self.adapter.config.get("reconcile") or {}).get("fields_by_category") or {}).get(
                lead["change_category"]
            )
            or []
        )
        pdf_fields = (self.adapter.config.get("reconcile") or {}).get("pdf_fields") or {}
        unverified, verified = [], []
        for ref in self.rows:
            _, _, entry = self.pdf_page(ref)
            states = entry.get("fields", {})
            bad = entry.get("row_status") != "matched" or any(
                states.get(f) not in (None, "match") for f in fields
            )
            (unverified if bad else verified).append(ref)
        if unverified:
            lines.append("### 原本照合ができなかった行と項目（先に確かめる）")
            lines += [self.check_line(ref, fields, pdf_fields) for ref in unverified]
            lines.append("")
        lines.append("### 原本で確かめる行")
        lines += [self.check_line(ref, fields, pdf_fields) for ref in verified] or ["- なし"]
        lines.append("")
        if self.matches:
            lines.append("### 同種業務の対応")
            for m in self.matches:
                lines.append(
                    f"- {m['match_id']}（{MATCH_STATUS_NAMES.get(m['status'], m['status'])}。{m['relation']}）："
                    f"{'・'.join(json.loads(m['left_json']))} と {'・'.join(json.loads(m['right_json']))}。"
                    f"理由：{self.expand(m['reason'])}"
                )
            lines.append("")
        return lines

    def check_line(self, ref: str, fields: list[str], pdf_fields: dict) -> str:
        record = self.record(ref)
        source = self.source(ref.split(":", 1)[0])
        spec = self.spec_of(source)
        cells = json.loads(record.get("cells_json") or "{}")
        pdf_source, page, entry = self.pdf_page(ref)
        items = []
        for f in fields:
            key = pdf_fields.get(f, f)
            if spec is None or key not in spec.take:
                continue
            header = spec.take[key]["header"]
            state = (entry.get("fields") or {}).get(f)
            mark = (
                ""
                if state in (None, "match")
                else f"（照合：{FIELD_STATE_NAMES.get(state, state)}）"
            )
            items.append(f"「{header}」={cells.get(key, '')}{mark}")
        logical = record.get("logical_no")
        row_no = f"{HEADER_LINES + logical}行目（目安）" if logical is not None else "不明"
        return (
            f"- {ref}：{self.file_name(source)}（原本 data/raw/{source.get('sha256')}.csv）。"
            f"NO {cells.get('no', '')}。表計算ソフトの行 {row_no}。"
            f"PDF {pdf_source or '（なし）'} の{page if page is not None else '（対応なし）'}ページ。"
            f"確かめる項目：{'、'.join(items) if items else 'なし'}"
        )

    def section8(self, memo_no: int, digest: str) -> list[str]:
        recorded = json.loads(self.run["rule_versions_json"])
        lines = [
            "## 8 再現情報",
            "",
            f"- 実行ID：{self.run_id}",
            f"- メモ：{self.lead['lead_id']} の v{memo_no}",
            "- 計算：",
        ]
        for calc_id in hashing.memo_calc_ids(self.conn, self.lead["lead_id"]):
            comp = self.calc(calc_id)
            if comp is None:
                continue
            lines.append(
                f"  - {calc_id}（{comp['kind']}）：依頼 {comp['request_json']}。"
                f"row_refs のハッシュ {comp['row_refs_sha256'] or 'なし'}"
            )
        lines.append("- 原本のハッシュ：3節の資料の一覧のとおり（検証が依存する資料のすべて）")
        lines += self.rule_files_lines(recorded)
        lines += [
            (
                f"- 版：コード {recorded.get('code_version')}（コミット {recorded.get('git_commit')}。Skill と"
                f"ルールのファイルを含む）、規則 {recorded.get('rules_version')}、分野の規則 {recorded.get('parser_version')}、"
                f"dataset.yaml {recorded.get('dataset_yaml_sha256')}、分野のSQL {recorded.get('sql_sha256')}"
            ),
            f"- モデル：{self.config.get('model', '（未記録）')}。Claude Code：{recorded.get('claude_code_version', '（未記録）')}",
            f"- 検証記録のハッシュ：{digest}",
            "",
        ]
        return lines

    def rule_files_lines(self, recorded: dict) -> list[str]:
        """Skill とルールのファイル：run の開始時の記録と render の時点の値を分けて出す（D-042④）。開始時の記録の
        ない実行は「未記録」とし、render の時点の値で補わない。"""
        start = recorded.get("rule_files")
        now = versions.rule_file_hashes(self.root)
        lines = []
        if not isinstance(start, dict) or not isinstance(start.get("files"), dict):
            lines.append("- Skill とルールのファイル（run の開始時の記録）：未記録")
        else:
            match = start.get("matches_commit")
            if match is None:
                state = "コミットと同じかは確かめられなかった"
            elif match:
                state = f"コミット {start.get('commit')} の内容と同じ"
            else:
                parts = [
                    f"{name} {'・'.join(start.get(key) or [])}"
                    for key, name in (("added", "追加"), ("deleted", "削除"), ("changed", "変更"))
                    if start.get(key)
                ]
                state = f"コミット {start.get('commit')} の内容と違う（{'、'.join(parts)}）"
            lines.append(f"- Skill とルールのファイル（run の開始時の記録）：{state}")
            lines += [f"  - {rel}：{sha}" for rel, sha in sorted(start["files"].items())]
        lines.append("- Skill とルールのファイル（render の時点）：")
        lines += [f"  - {rel}：{sha}" for rel, sha in sorted(now.items())] or ["  - なし"]
        if isinstance(start, dict) and isinstance(start.get("files"), dict):
            before = start["files"]
            diff = [
                (name, sorted(paths))
                for name, paths in (
                    ("追加", set(now) - set(before)),
                    ("削除", set(before) - set(now)),
                    ("変更", {k for k in set(now) & set(before) if now[k] != before[k]}),
                )
                if paths
            ]
            lines.append(
                "  - 開始時から変わった："
                + "、".join(f"{name} {'・'.join(paths)}" for name, paths in diff)
                if diff
                else "  - 開始時の記録と同じ"
            )
        return lines

    def text(self, memo_no: int, digest: str) -> str:
        profile = "はい" if self.run.get("profile") == "investigate" else "いいえ"
        lines = [
            self.title(),
            "",
            f"> {FIXED_NOTICE}",
            f"> 調査用の設定で実行：{profile}　実行ID：{self.run_id}　メモ：v{memo_no}　検証記録のハッシュ：{digest}",
            "",
            *self.section1(),
            *self.section2(),
            *self.section3(),
            *self.section4(),
            *self.section5(),
            *self.section6(),
            *self.section7(),
            *self.section8(memo_no, digest),
        ]
        return "\n".join(lines).rstrip("\n") + "\n"


def render_lead(root: Path, run_id: str, lead_id: str) -> dict:
    """kizashi render <lead_id> --run <run_id>（4.7）。"""
    conn = ledger.open_rw(root, run_id)
    written: Path | None = None
    try:
        check.check_rule_versions(conn, root, run_id, STAGE)
        (domain,) = conn.execute("SELECT domain FROM runs").fetchone()
        adapter = load_adapter(root, domain)
        memos = paths.run_dir(root, run_id) / "memos"
        with ledger.transaction(conn):
            v = check.verify(conn, root, run_id, adapter, lead_id)  # 巡回数に数えない
            if v.failures:
                raise check.failure_error(v, STAGE)
            lead = _one(
                conn,
                "SELECT state, proposed_outcome, memo_no FROM leads WHERE lead_id = ?",
                (lead_id,),
            )
            assert lead is not None
            check.mark_checked(conn, v.memo_claims)
            if lead["state"] != lead["proposed_outcome"]:
                conn.execute(
                    "UPDATE leads SET state = ? WHERE lead_id = ?",
                    (lead["proposed_outcome"], lead_id),
                )
                ledger.add_event(
                    conn,
                    lead_id,
                    "state_change",
                    before={"state": lead["state"]},
                    after={"state": lead["proposed_outcome"]},
                    reason="render",
                )
            digest = hashing.check_hash(conn, lead_id, adapter)
            memo_no = int(lead["memo_no"]) + 1
            memos.mkdir(parents=True, exist_ok=True)
            paths.ensure_in_data(root, memos)
            path = memos / f"{lead_id}.v{memo_no}.md"
            text = _Memo(conn, root, run_id, adapter, lead_id).text(memo_no, digest)
            try:
                with path.open("x", encoding="utf-8") as fh:  # 上書きしない
                    written = path
                    fh.write(text)
            except FileExistsError:
                raise KizashiError(
                    "E_INTERNAL",
                    STAGE,
                    f"メモ {path.name} がすでにある（番号と台帳が合わない）。上書きしない。",
                    affected_ids=[lead_id],
                ) from None
            conn.execute(
                "UPDATE leads SET memo_no = ?, memo_hash = ? WHERE lead_id = ?",
                (memo_no, digest, lead_id),
            )
            ledger.add_event(
                conn,
                lead_id,
                "render",
                after={
                    "memo": path.relative_to(paths.run_dir(root, run_id)).as_posix(),
                    "memo_no": memo_no,
                },
                check_hash=digest,
            )
    except BaseException:
        if written is not None:
            written.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    return {
        "run_id": run_id,
        "lead_id": lead_id,
        "state": lead["proposed_outcome"],
        "memo": path.relative_to(root).as_posix(),
        "memo_no": memo_no,
        "check_hash": digest,
        "t6": v.t6,
        "message": "メモを書いた。記者の確認リスト（7節）の行を、記者が原本で確かめる。",
    }
