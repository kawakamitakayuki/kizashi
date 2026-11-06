"""取り込みの照合（T6a）と原本照合（T6b）を、computations（kind='reconcile'）に記録する。

implementation_spec.md 2章の reconcile.py、4.1 の工程 reconcile_import・reconcile_source、7.4 の記録の形。
アダプタ仕様6章（別経路の読み込み処理）と7章（PDF の原本照合。D-002 の方法(a)）。

- reconcile_import：資料（CSV）ごとに、別経路の読み込み処理で原本を読み直し、正規化した値と論理レコード番号
  ごとに比べる。照合の計算を資料ごとに1つ登録する。
- reconcile_source：同じ年度・方式の CSV と PDF の組ごとに、CSV の全行を PDF と照合する。照合の計算を組ごとに
  1つ登録する。PDF の文字列は記録に書かない（結果の区分とページだけ。担当官の氏名を書き出さないため）。
- 照合の結果で実行を止めない。T6a・T6b の判定は、候補の行について check が行う（実装仕様9章の末尾。P0-6）。

呼び出し側（runner）がトランザクションを持つ。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from kizashi import independent_reader, ledger, paths, pdftext
from kizashi.adapters import Adapter
from kizashi.hashing import canonical_json, sha256_text

MATCH, MISMATCH, UNVERIFIABLE = pdftext.MATCH, pdftext.MISMATCH, pdftext.UNVERIFIABLE
AMOUNT_YEN = "amount_yen"  # 整数にした金額（取り込みの照合の項目。アダプタ仕様6章）


def _fetched(conn: sqlite3.Connection, fmt: str) -> list[tuple[str, str, str]]:
    return conn.execute(
        "SELECT source_id, logical_key, sha256 FROM sources WHERE kind = 'dataset'"
        " AND status = 'fetched' AND logical_key LIKE ? ORDER BY source_id",
        (f"%|{fmt}",),
    ).fetchall()


def _register(
    conn: sqlite3.Connection,
    *,
    request: dict,
    meaning: str,
    row_refs: Sequence[str],
    result: dict,
) -> str:
    calc_id = ledger.next_id(conn, "K")
    refs = sorted(row_refs)
    refs_json = canonical_json(refs)
    conn.execute(
        "INSERT INTO computations (calc_id, kind, request_json, meaning, row_refs_json,"
        " row_refs_sha256, row_count, result_json, origin, created_at)"
        " VALUES (?, 'reconcile', ?, ?, ?, ?, ?, ?, 'run', ?)",
        (
            calc_id,
            canonical_json(request),
            meaning,
            refs_json,
            sha256_text(refs_json),
            len(refs),
            canonical_json(result),
            ledger.now_iso(),
        ),
    )
    return calc_id


def _spec_of(adapter: Adapter, logical_key: str):
    _, year, family, _ = logical_key.split("|")
    spec = adapter.file_spec(int(year), family)
    assert spec is not None, logical_key
    return spec


# ---------------------------------------------------------------------------
# 取り込みの照合（T6a）
# ---------------------------------------------------------------------------


def reconcile_import(conn: sqlite3.Connection, root: Path, adapter: Adapter) -> dict[str, dict]:
    """取得済みの CSV ごとに、別経路の読み込み処理の値と正規化した値を論理レコード番号ごとに比べて記録する。

    比べる項目：アダプタ仕様6章の元の値（分野アダプタの IMPORT_FIELDS のうち、その年度・方式の take にあるもの。
    records.cells_json の値と、別経路で読んだセルの前後の空白を除いた値の完全一致）と、整数にした金額
    （procurement_contracts.amount_yen と、別経路で4.4の規則で整数にした値）。
    行の結果は、すべて一致なら match、1つでも違えば mismatch。片方にしかない行は mismatch
    （row_reason は missing_in_ledger・missing_in_reader）。戻り値は資料ごとの計算IDと件数。
    """
    summary: dict[str, dict] = {}
    for source_id, logical_key, sha in _fetched(conn, "csv"):
        spec = _spec_of(adapter, logical_key)
        keys = [k for k in adapter.module.IMPORT_FIELDS if k in spec.take]
        expected = {k: str(spec.take[k]["header"]) for k in keys}
        table = independent_reader.read_by_headers(
            paths.raw_dir(root) / f"{sha}.csv", spec.encoding, expected, stage="reconcile_import"
        )
        reader = dict(table.records)
        stored = {
            row[1]: row
            for row in conn.execute(
                "SELECT r.record_id, r.logical_no, r.cells_json, r.status_reason, p.amount_yen,"
                " p.record_id IS NOT NULL FROM records r"
                " LEFT JOIN procurement_contracts p ON p.record_id = r.record_id"
                " WHERE r.source_id = ? ORDER BY r.logical_no",
                (source_id,),
            )
        }
        field_names = [*keys, AMOUNT_YEN]
        field_counts = {f: {MATCH: 0, MISMATCH: 0} for f in field_names}
        rows: dict[str, dict] = {}
        for number in sorted(set(reader) | set(stored)):
            rid = ledger.record_id(source_id, number)
            if number not in stored:
                rows[rid] = {
                    "row_status": MISMATCH,
                    "row_reason": "missing_in_ledger",
                    "fields": {},
                }
                continue
            if number not in reader:
                rows[rid] = {
                    "row_status": MISMATCH,
                    "row_reason": "missing_in_reader",
                    "fields": {},
                }
                continue
            _, _, cells_json, status_reason, amount_yen, has_contract = stored[number]
            values = reader[number]
            if status_reason == "empty_record":
                empty = not any(values.values())
                rows[rid] = {
                    "row_status": MATCH if empty else MISMATCH,
                    "row_reason": "empty_record",
                    "fields": {},
                }
                continue
            cells = json.loads(cells_json)
            fields = {k: MATCH if cells.get(k) == values[k] else MISMATCH for k in keys}
            if has_contract:
                reread = independent_reader.parse_amount_yen(values.get("amount_raw"))
                fields[AMOUNT_YEN] = MATCH if reread == amount_yen else MISMATCH
            for name, outcome in fields.items():
                field_counts[name][outcome] += 1
            ok = all(v == MATCH for v in fields.values())
            rows[rid] = {"row_status": MATCH if ok else MISMATCH, "fields": fields}
        n_match = sum(1 for r in rows.values() if r["row_status"] == MATCH)
        counts = {
            "rows": len(rows),
            MATCH: n_match,
            MISMATCH: len(rows) - n_match,
            "fields": field_counts,
        }
        calc_id = _register(
            conn,
            request={
                "type": "import",
                "source": source_id,
                "reader": "independent_reader.read_by_headers",
                "fields": field_names,
            },
            meaning=(
                f"取り込みの照合（T6a）：{source_id}（{logical_key}）の原本を、見出しの文字列で列を特定する"
                "別経路の読み込み処理で読み直し、正規化した値と論理レコード番号ごとに比べた"
            ),
            row_refs=list(rows),
            result={"type": "import", "source": source_id, "rows": rows, "counts": counts},
        )
        summary[source_id] = {
            "calc_id": calc_id,
            "rows": counts["rows"],
            MATCH: counts[MATCH],
            MISMATCH: counts[MISMATCH],
        }
    return summary


# ---------------------------------------------------------------------------
# 原本照合（T6b）
# ---------------------------------------------------------------------------


def reconcile_source(conn: sqlite3.Connection, root: Path, adapter: Adapter) -> dict[str, dict]:
    """取得済みの CSV ごとに、同じ年度・方式の PDF と照合して記録する（アダプタ仕様7章、実装仕様7.4）。

    - PDF を取得できていない CSV は照合しない（計算を作らず、戻り値に not_run と理由）。
    - PDF を読めない場合は、その組の全行を not_found（row_reason は pdf_unreadable）とし、組に error を書く。
    - 照合する行は分野の表にある行（正規化で採用・対象外にした行）。空のレコードと NO が数でない保留の行は
      照合せず、件数を counts の skipped に入れる。
    戻り値は CSV の資料IDごとの計算IDと件数。
    """
    cfg = adapter.config["reconcile"]
    if cfg.get("source_check") != "pdf":
        return {}
    pdf_fields = cfg["pdf_fields"]
    datasets = {
        row[1]: row
        for row in conn.execute(
            "SELECT source_id, logical_key, status, sha256 FROM sources WHERE kind = 'dataset'"
        )
    }
    summary: dict[str, dict] = {}
    for csv_id, csv_key, _ in _fetched(conn, "csv"):
        pdf_key = csv_key.rsplit("|", 1)[0] + "|pdf"
        pdf = datasets.get(pdf_key)
        if pdf is None or pdf[2] != "fetched":
            which = "予定にない" if pdf is None else f"{pdf[0]}（状態 {pdf[2]}）"
            summary[csv_id] = {
                "state": "not_run",
                "reason": f"同じ年度・方式の PDF を取得できていない：{which}",
            }
            continue
        pdf_id, _, _, pdf_sha = pdf
        spec = _spec_of(adapter, csv_key)
        tspec = pdftext.table_spec(spec, pdf_fields)
        pair: dict = {"csv_source": csv_id, "pdf_source": pdf_id}
        table: pdftext.PdfTable | None
        try:
            pages = pdftext.load_pages(paths.raw_dir(root) / f"{pdf_sha}.pdf")
        except Exception as exc:  # noqa: BLE001  PDF の読み込みの失敗は、照合不能として記録する
            table = None
            pair["error"] = f"PDF を読めない（{type(exc).__name__}: {exc}）"
        else:
            table = pdftext.extract_cells(pages, tspec)
            pair.update(
                pages=table.page_count,
                skipped_pages=table.skipped_pages,
                split_pages=table.split_pages,
            )
        result = _match_rows(conn, csv_id, tspec, table, pair)
        calc_id = _register(
            conn,
            request={
                "type": "source",
                "csv_source": csv_id,
                "pdf_source": pdf_id,
                "method": "cells_by_rules",  # D-002 の方法(a)
                "fields": list(tspec.field_columns),
            },
            meaning=(
                f"原本照合（T6b）：{csv_id}（{csv_key}）の全行を、同じ年度・方式の PDF（{pdf_id}）の表の"
                "セルと照合した（行の対応と項目の照合）"
            ),
            row_refs=list(result["rows"]),
            result=result,
        )
        counts = result["counts"]
        entry = {
            "calc_id": calc_id,
            "pdf_source": pdf_id,
            **{k: counts[k] for k in ("rows", "matched", "not_found", "ambiguous", "skipped")},
            "fields": counts["fields"],
            "corp_completion": counts["corp_completion"],
        }
        if "error" in pair:
            entry["error"] = pair["error"]
        summary[csv_id] = entry
    return summary


def _match_rows(
    conn: sqlite3.Connection,
    csv_id: str,
    tspec: pdftext.TableSpec,
    table: pdftext.PdfTable | None,
    pair: dict,
) -> dict:
    field_keys = list(tspec.field_columns)
    contracts = conn.execute(
        "SELECT r.record_id, r.cells_json, p.amount_yen, p.amount_note, p.corp_number_state,"
        " p.corp_numbers_json FROM records r JOIN procurement_contracts p"
        " ON p.record_id = r.record_id WHERE r.source_id = ? ORDER BY r.logical_no",
        (csv_id,),
    ).fetchall()
    (total,) = conn.execute(
        "SELECT COUNT(*) FROM records WHERE source_id = ?", (csv_id,)
    ).fetchone()
    counts: dict = {
        "rows": len(contracts),
        "matched": 0,
        "not_found": 0,
        "ambiguous": 0,
        "skipped": total - len(contracts),
        "fields": {f: {MATCH: 0, MISMATCH: 0, UNVERIFIABLE: 0} for f in field_keys},
        "corp_completion": {"valid_from_pdf": 0, "unconfirmed": 0},
    }
    corp_column = tspec.field_columns.get("corp_numbers")
    adopted: list[tuple[str, str]] = []
    rows: dict[str, dict] = {}
    for rid, cells_json, amount_yen, amount_note, corp_state, corp_json in contracts:
        cells = json.loads(cells_json)
        unverifiable = dict.fromkeys(field_keys, UNVERIFIABLE)
        if table is None:
            entry: dict = {"row_status": "not_found", "row_reason": "pdf_unreadable"}
            entry["fields"] = unverifiable
        else:
            found = pdftext.locate_row(table, cells.get("no"), cells.get("contract_date_raw"))
            if found.status == "matched":
                assert found.row is not None
                values = {
                    **cells,
                    "amount_yen": amount_yen,
                    "amount_note": amount_note,
                    "corp_number_state": corp_state,
                    "corp_numbers": json.loads(corp_json),
                }
                entry = {
                    "row_status": "matched",
                    "pdf_page": found.row.page,
                    "fields": {
                        f: pdftext.match_field(f, values, found.row.cells.get(col, ""))
                        for f, col in tspec.field_columns.items()
                    },
                }
            else:
                entry = {"row_status": found.status, "fields": unverifiable}
                if found.reason:
                    entry["row_reason"] = found.reason
                if found.pages:
                    entry["pdf_pages"] = found.pages
        counts[entry["row_status"]] += 1
        for f, outcome in entry["fields"].items():
            counts["fields"][f][outcome] += 1
        if corp_state == "exponent" and corp_column is not None:
            # 法人番号の補完（P1-b。設計書7.4）。行が対応しなければ未確定
            if entry["row_status"] == "matched" and table is not None:
                completion, number = pdftext.corp_completion(
                    cells.get("corp_number_raw"),
                    found.row.cells.get(corp_column, ""),
                    entry["fields"],
                )
            else:
                completion, number = {"state": "unconfirmed", "failed": ["row"]}, None
            entry["corp_completion"] = completion
            counts["corp_completion"][completion["state"]] += 1
            if number is not None:
                adopted.append((canonical_json([number]), rid))
        rows[rid] = entry
    conn.executemany(
        "UPDATE procurement_contracts SET corp_number_state = 'valid_from_pdf',"
        " corp_numbers_json = ? WHERE record_id = ?",
        adopted,
    )
    return {"type": "source", "pairs": [pair], "rows": rows, "counts": counts}


# ---------------------------------------------------------------------------
# 変化の区分ごとの照合項目と T6b の判定（アダプタ仕様7.4、実装仕様9章の末尾）
# ---------------------------------------------------------------------------


def source_fields(adapter: Adapter, category: str, *, counterparty: bool = False) -> list[str]:
    """変化の区分ごとの照合項目（dataset.yaml の reconcile.fields_by_category）。受注先を扱う主張があれば
    fields_counterparty を加える。"""
    cfg = adapter.config["reconcile"]
    by_category = cfg.get("fields_by_category") or {}
    if category not in by_category:
        raise ValueError(f"fields_by_category にない変化の区分：{category}")
    fields = list(by_category[category])
    if counterparty:
        fields += [f for f in cfg.get("fields_counterparty") or [] if f not in fields]
    return fields


def judge_t6b(rows: Mapping[str, dict], row_refs: Iterable[str], fields: Sequence[str]) -> str:
    """候補の行について、照合項目だけで T6b を判定する。

    どれかの行の照合項目に mismatch があれば support（支持）。すべての行が matched で、照合項目がすべて match
    なら refute（否定）。それ以外（unverifiable・not_found・ambiguous・照合の記録にない行）は unknown（未確認）。
    照合項目のうち、その行の方式にない項目（競争入札の行の reason_raw など）は見ない。
    """
    refs = list(row_refs)
    mismatch = False
    confirmed = bool(refs)
    for ref in refs:
        entry = rows.get(ref)
        if entry is None:
            confirmed = False
            continue
        outcomes = [entry["fields"][f] for f in fields if f in entry.get("fields", {})]
        if MISMATCH in outcomes:
            mismatch = True
        if (
            entry.get("row_status") != "matched"
            or not outcomes
            or any(o != MATCH for o in outcomes)
        ):
            confirmed = False
    if mismatch:
        return "support"
    return "refute" if confirmed else "unknown"
