# 実装仕様書（詳細設計・プログラム設計）

- 対象：KIZASHI のP0（設計書の「P0」）。P1は13章で概要だけを示す。
- 正とする設計：`docs/harness_design_m_v06.md`（以下「設計書」）。節の番号は設計書のもの。
- 分野固有の内容（内閣府の列、検出SQLなど）は `docs/adapter_cao_procurement.md` に分けた。

---

## 1. 技術の選定

| 項目 | 決定 | 理由 |
| --- | --- | --- |
| 言語 | Python 3.12 以上 | 標準の csv・decimal・sqlite3・hashlib・unicodedata で大半を書ける |
| パッケージ管理 | uv（`pyproject.toml` と `uv.lock`） | 依存の固定。調査中は `uv run` を使わず `.venv/bin/kizashi` を呼ぶ（設計書9.5） |
| 依存（実行） | `pyyaml`（`dataset.yaml`）、`httpx`（取得。リダイレクトを1段ずつ検査するため自動追跡を切って使う）、`pdfplumber==0.11.10`（PDF） | PDFは初日に `pdfplumber` を第一候補として試し、だめなら `pypdfium2` を試す（U13）。→ pdfplumber 0.11.10 に決め、版を固定した。罫線の座標で表のセルを切り出す（D-002。アダプタ仕様7章）。pdfplumber は pdfminer.six、pypdfium2、Pillow などを依存として入れる |
| 依存（開発） | `pytest`、`ruff` | 設計書9.4 |
| SQLite | Pythonに組み込まれた `sqlite3` の SQLite 3.44 以上 | 検出SQLが集約の中の `ORDER BY`（`group_concat(x, '\|' ORDER BY x)`）を使う（アダプタ仕様9章）。uv が入れる Python では通常満たす。満たさなければ起動時に `E_USAGE` で止め、版を案内する |
| ビルド | `pyproject.toml` に `[build-system]`（`hatchling`）を書く | `[project.scripts]` の `kizashi` を `.venv/bin` に入れるため、パッケージとして導入する必要がある |
| 使わないもの | pandas、DuckDB、ORM、テンプレートエンジン、非同期処理 | 小規模で、標準ライブラリで足りる。依存を増やすと再現と審査が重くなる |

CLIのエントリーポイントは `pyproject.toml` の `[project.scripts]` に `kizashi = "kizashi.cli:main"` と書く。

## 2. リポジトリとモジュール

設計書3.3の構成に従う。`src/kizashi/` の中は次のとおりとする。1つのモジュールが1つの責務を持ち、他のモジュールの内部に手を入れない。

| モジュール | 責務 | 主な関数（例） |
| --- | --- | --- |
| `cli.py` | 引数の解釈、作業フォルダーの検査、出力のJSON化、例外からエラーJSONへの変換 | `main(argv)` |
| `errors.py` | 型付きのエラー | `KizashiError(code, stage, message, affected_ids=(), retryable=False, next_action="")` |
| `paths.py` | リポジトリ直下の判定、`data/`・`inbox/`・実行フォルダーのパス、パスの検査（`..` とシンボリックリンクの拒否） | `repo_root()`, `run_dir(run_id)`, `resolve_inbox_file(name)` |
| `limits.py` | 上限の定数（3.6） | — |
| `versions.py` | コードの版（パッケージの版＋gitのコミット）、規則の版（`dataset.yaml`・検出SQLのハッシュ）、知識ファイルのハッシュ | `rule_versions(domain)` |
| `ledger.py` | SQLiteの接続（書き込み用、読み取り専用）、スキーマの作成、ID の採番、トランザクション、`events` への追記 | `open_rw(run_id)`, `open_ro(run_id)`, `next_id(conn, prefix)` |
| `schema.sql` | 5章のDDL | — |
| `fetcher.py` | 許可リスト、リダイレクトの検査、サイズの上限、一時ファイル、SHA-256での保存、`sources` への登録、通信回数の計上 | `fetch(url, kind, run)`, `discover_links(page_url, patterns)` |
| `adapters/__init__.py` | 分野アダプタの読み込み（`datasets/<分野>/dataset.yaml`） | `load_adapter(domain)` |
| `adapters/cao_procurement.py` | 内閣府の正規化と分野の表への書き込み（アダプタ仕様の規則をコードにしたもの） | `normalize_row(...)` |
| `readers.py` | 正規化の経路のCSV読み込み（`dataset.yaml` の列番号で読む） | `read_rows(path, spec)` |
| `independent_reader.py` | 別経路の読み込み処理（見出しの文字列で列を特定する）。T6aの照合とV3(b)で共有する | `read_by_headers(path, expected)` |
| `normalize.py` | NFKC、空白の除去、和暦の日付、金額、法人番号、件名の正規化 | `wareki_to_iso("R6.4.1")` |
| `quality.py` | 品質検査（アダプタ仕様の Q 項目）と品質レポート | `run_quality(conn, run)` |
| `runner.py` | `run` の新しい実行：`run_config` の検査、実行IDの発行と台帳の作成、工程（plan〜detect）の順序とトランザクション、`runs.stages_json`・状態・`stop_reason` の記録、設定ファイルの `accepted/` への移動（D-029） | `start_run(root, domain, config_arg)`, `validate_run_config(config, adapter)` |
| `pdftext.py` | 追加資料のPDFのページ別テキストの抽出。公表一覧のPDFの表のセルの切り出し（照合に使う列だけを残し、担当官の列などの文字は捨てる）、行の対応、項目の照合（アダプタ仕様7章。D-002） | `extract_pages(pdf)`, `extract_cells(pdf, spec)`, `locate_row(...)`, `match_field(...)` |
| `reconcile.py` | 取り込みの照合（T6a）と原本照合（T6b）を `computations`（種類 `reconcile`）に記録 | `reconcile_import(...)`, `reconcile_source(...)` |
| `detect.py` | 検出SQLの実行、候補の登録、自動の基礎集計、同じ区分の件数、年度の系列 | `run_screens(conn, run)` |
| `calc.py` | 基礎集計（SQLで行を選び、Decimalで合計）、派生計算（決まった演算だけ）、文書値（引用の中の数値との照合） | `base_aggregate(req)`, `derive(op, args)` |
| `quotes.py` | 引用の照合（NFKC・空白除去、文末までの引用、年度の一致） | `verify_quote(source, position, quote)` |
| `inbox.py` | 提案ファイルの検査（場所、拡張子、大きさ、JSONの形）と、成功時の `accepted/` への移動 | `load_proposal(path)`, `accept(path, run)` |
| `record.py` | 提案の種類ごとの登録（6章） | `record(run_id, path)` |
| `query.py` | 探索用の読み取り専用SQL | `run_query(run_id, sql_path)` |
| `inspect_cmd.py` | `inspect` の表示 | `inspect(id, page)` |
| `check.py` | 検証V1〜V8、警告、巡回数 | `check_lead(run_id, lead_id)` |
| `hashing.py` | 正規化したJSONと検証記録のハッシュ（11章） | `check_hash(conn, lead_id)` |
| `render.py` | 候補のメモ、実行の概要、伏せた写し | `render_lead(...)`, `render_run(...)` |
| `publish.py` | `check --publish` | `check_publish(manifest)` |

`datasets/cao-procurement/` に `dataset.yaml`、`screens/*.sql`（検出SQL）、`populations/*.sql`（比べた組の数）、`series/*.sql`（年度の系列。D-038④）、`README.md`、`fixtures/` を置く（アダプタ仕様）。

## 3. 共通仕様

### 3.1 呼び出しと作業フォルダー

- 呼び出しは `kizashi <操作> ...`。調査では `.venv/bin` を `PATH` の先頭に置いた状態で呼ばれる（設計書9.5）。
- 起動時に、現在のフォルダーがリポジトリの直下（`pyproject.toml` と `datasets/` がある）でなければ、`E_CWD` で止まる。
- 起動時に `sqlite3.sqlite_version` が3.44未満なら、`E_USAGE` で止まる（1章）。
- 環境変数 `KIZASHI_PROFILE` の値を読み、`run` は `runs.profile` に記録する。値がなくても止めない（設計書3.4）。

### 3.2 出力

標準出力に1つのJSONだけを書く。人が読む補足は `message` に日本語で入れる。

```json
{"ok": true, "data": {...}, "warnings": [{"code": "W7_STRONG_WORD", "message": "...", "affected_ids": ["C0004"]}]}
```

```json
{"ok": false, "error": {"code": "V3B_REAGG_MISMATCH", "stage": "check", "message": "...", "affected_ids": ["K0007"], "retryable": false, "next_action": "..."}}
```

`check` と `render` の検証の不合格は、`code` を最初の不合格のコードにし、`failures` に不合格のすべて（`[{"code", "message", "affected_ids"}]`。9章の表の順）を入れる（D-041①）。

| 終了コード | 意味 |
| --- | --- |
| 0 | 成功（警告があっても0） |
| 1 | 検査の不合格・提案の不備（直せば通る） |
| 2 | 引数の誤り |
| 3 | 環境の問題（作業フォルダー、規則の版の食い違い、ファイルの欠落） |
| 4 | 通信の問題（`retryable` が true のことがある） |
| 5 | 想定外のエラー（バグ。スタックトレースは標準エラー出力に出し、JSONには要約だけ） |

1回の出力の上限：`inspect` は1ページ20件、`query` は50行、本文の抜粋は1件2,000字。超えた分は `next_page` を返す。

### 3.3 ID

実行ごとの台帳の中で一意にする。

| 対象 | 書式 | 例 |
| --- | --- | --- |
| 実行 | `YYYYMMDD-HHMMSS-` ＋英小文字と数字4桁 | `20261105-101500-a3f2` |
| 資料 | `S` ＋3桁 | `S004` |
| レコード（`row_ref`） | 資料ID ＋ `:` ＋論理レコード番号6桁 | `S004:000042` |
| 計算 | `K` ＋4桁 | `K0012` |
| 候補 | `L` ＋4桁 | `L0007` |
| 主張 | `C` ＋4桁 | `C0031` |
| 対応 | `M` ＋3桁 | `M002` |

CLIは、引数のIDがこの書式に合わなければ `E_BAD_ID` で拒否する。

### 3.4 パス

- `data/raw/<sha256>.<拡張子>`：原本。上書きしない。
- `data/runs/<run_id>/`：`ledger.sqlite`、`accepted/`、`queries/`、`evidence/<source_id>/pages/<n>.txt`、`memos/<lead_id>.v<n>.md`、`memos/run_summary.v<n>.md`、`public/`、`quality_report.md`。
- `inbox/`：Claudeが書く提案。CLIは `inbox/` 直下の `*.json` と `*.sql` だけを読む。サブフォルダー、他の拡張子、1MBを超えるファイル、シンボリックリンクは `E_INBOX_PATH` で拒否する。
- CLIの書き込み先は、実際のパスを解決したうえで、`data/` の中に限る。

### 3.5 版の記録

`run` の開始時に、次を `runs.rule_versions_json` に記録する。

```json
{"dataset_yaml_sha256": "...", "sql_sha256": "...(screens/*.sql、populations/*.sql、series/*.sql をパスの順に連結したハッシュ)",
 "rules_version": "1", "parser_version": "cao-procurement/1",
 "code_version": "0.1.0", "git_commit": "abc1234 または unknown"}
```

- **照合するもの**：`dataset_yaml_sha256`、`sql_sha256`、`rules_version`、`parser_version`。`check` と `render` は毎回いまの値と照らし、違えば `E_RULE_VERSION` で止まり、新しい実行を求める（設計書4.2）。
- **記録だけのもの**：`code_version` と `git_commit`。開発中のコミットのたびに既存の実行が使えなくなるのを避けるためである。
- **Skill とルールのファイル**（記録だけ。D-042④）：`run` の開始時に、`CLAUDE.md` と `.claude/skills/` の Markdown の一覧と SHA-256 を `rule_files`（`{"files": {"<パス>": "<SHA-256>"}, "commit": "<git_commit>", "matches_commit": true / false / null, "added": [...], "deleted": [...], "changed": [...]}`）に書く。`added`・`deleted`・`changed` は、記録したコミットのファイルの一覧と内容に比べた追加・削除・変更（ファイルの一覧も照らす。一覧は `git ls-tree -z` の NUL 区切りで読み、日本語などのパスを引用・エスケープの形で扱わない。D-043③）。確かめられなければ `matches_commit` を null にする。開始時の記録は上書きしない。
- `rules_version`（`versions.py` の定数）と `parser_version`（`dataset.yaml`）は、正規化・照合・検証の結果を変えるコードの変更をしたときに、人が手で上げる。上げ忘れを防ぐため、テストの期待値を変えたときは上げる、と共通ルールに書く。
- Claude Codeの版は、`scripts/investigate` が環境変数 `KIZASHI_CLAUDE_VERSION` に入れ、`run` が記録する。モデル名は `investigate` のSkillが `run_config` に書かせる（6.2）。

### 3.6 上限の定数

設計書4.3の値を `limits.py` に定数で置く。

| 定数 | 値 |
| --- | --- |
| `MAX_DEEP_LEADS` | 3 |
| `MAX_EVIDENCE_FETCH` | 12（`--index`・`--definition`・`--recheck` と初期データは数えない） |
| `MAX_HTTP_REQUESTS` | 40（すべての通信を数える） |
| `MAX_RESPONSE_BYTES` | 25MB |
| `MAX_RUN_BYTES` | 100MB |
| `MAX_RETRIES` | 2（一時的な通信の失敗だけ） |
| `MAX_ROUNDS` | 2（保留にした候補を調べ直す場合は1巡を追加で認める。「保留にした候補」は保留のメモを書いて `state` が `hold` になった候補で、保留の結論の案を登録しただけでは足さない。D-056②） |
| `QUERY_ROW_LIMIT` | 50 |
| `QUERY_TIMEOUT_SEC` | 10 |

### 3.7 エラーコードの一覧

検証（V1〜V8）のコードは9章。それ以外のコードと終了コードは次のとおり。`retryable` が true のものだけ、上限付きで再試行してよい。

| コード | 終了コード | 意味 |
| --- | --- | --- |
| `E_CWD` | 3 | リポジトリの直下で実行していない |
| `E_BAD_ID` | 2 | IDの書式の誤り、`--run` の欠け |
| `E_USAGE` | 2 | 引数の誤り |
| `E_INBOX_PATH` | 1 | 提案ファイルの場所・拡張子・大きさ・シンボリックリンクの誤り |
| `E_SCHEMA` | 1 | 提案の形の誤り（必須の項目の欠け、型の誤り） |
| `E_UNKNOWN_KIND` | 1 | 知らない `kind` |
| `E_SELF_DECLARED` | 1 | 管理項目の自己申告 |
| `E_REF_NOT_FOUND` | 1 | 存在しない参照 |
| `E_QUOTE_NOT_FOUND`・`E_QUOTE_TRUNCATED`・`E_QUOTE_YEAR` | 1 | 引用の照合の失敗（8章） |
| `E_BAD_EVIDENCE_SOURCE` | 1 | 根拠にできない資料 |
| `E_DOC_VALUE_MISMATCH` | 1 | 文書値が引用の数値と合わない |
| `E_BAD_EXPR`・`E_DIV_ZERO` | 1 | 派生計算の誤り（知らない演算、引数の数、計算ID以外の引数、同じ計算IDの重複）、0での割り算 |
| `E_NOT_ALLOWED_COLUMN` | 1 | 基礎集計に許可していない表・列 |
| `E_V7` | 1 | `record` の時点の数値・氏名の検査の不合格（中身は `V7_RAW_NUMBER` などのコードで返す）。`query` の SQL・結果・SQLiteのエラーの文と、`run` の実行設定の氏名の検査の不合格（D-037・D-040） |
| `E_LIMIT_LEADS`・`E_LIMIT_EVIDENCE`・`E_LIMIT_HTTP`・`E_LIMIT_BYTES` | 1 | 探索上限への到達（設計書4.3）。保留にする |
| `E_ROUNDS_EXCEEDED` | 1 | 修正の巡回数の上限。保留以外の結論を受け付けない |
| `E_HOST_NOT_ALLOWED`・`E_REDIRECT_NOT_ALLOWED` | 1 | 許可リスト外のホスト、許可リスト外へのリダイレクト |
| `E_FETCH_FAILED` | 4 | 通信の失敗（`retryable` は一時的な失敗のとき true） |
| `E_FETCH_INVALID` | 1 | 取得したファイルが期待と違う（空、エラーページ、年度の違い） |
| `E_FORMAT_CHANGED` | 3 | 見出し・列の数が期待と違う（未知の様式）。開発側で直す |
| `E_QUERY_REJECTED`・`E_QUERY_TIMEOUT` | 1 | 探索SQLの拒否、時間切れ |
| `E_NOT_RESUMABLE`・`E_INPUT_CHANGED` | 3 | 再開できない状態、入力の変化（新しい実行を作る） |
| `E_RULE_VERSION` | 3 | 規則の版の食い違い（新しい実行を作る） |
| `E_DENYLIST_MISSING` | 3 | 公開の検査に要る一覧がない、読めない、形が誤っている、補助資料4点がそろったと確かめられない（12.2。D-056③） |
| `P_MANIFEST_MISMATCH`・`P_DENYLIST_HASH`・`P_DENYLIST_NAME`・`P_PERSONAL_NAME`・`P_EXCLUDED_VALUE`・`P_COUNTERPARTY`・`P_CORP_NUMBER` | 1 | `check --publish` の不合格（12.2 の P0 の4項目。D-053①） |
| `E_INTERNAL` | 5 | 想定外のエラー |

## 4. 操作の仕様

### 4.1 `run`

```text
kizashi run <分野> --config inbox/<名前>.json
kizashi run --resume <run_id>
```

**新しい実行**

1. 設定ファイル（6.2の `run_config`）を検査する。比べる2年度（`compare_years`。連続した2年度）と、系列として読む年度（`series_years`。比べる2年度を含む）を分けて持つ。取り込むのは `series_years` のすべて、検出は `compare_years` の組だけで行う。
2. 実行IDを発行し、実行フォルダーと台帳を作る。`runs` に設定・版・`profile` を記録し、状態を `running` にする。設定は、氏名の検査（`normalize` の最初）を通るまで全文を書かない。この時点の `runs.config_json` は、検査済みの `kind`・`domain`・`compare_years`・`series_years` と、`"name_check": "pending"`、設定の全文の正規化JSON（11章。キーの順と空白を問わず、数値の型を区別する）の SHA-256 `config_sha256` だけとする。`question`・`scope`・`model` などの文字列は書かない（D-040①。検査で当たった実行の台帳と WAL に氏名を残さないため）。
3. 工程を順に行う。各工程は1つのトランザクションで確定し、`runs.stages_json` に状態（`done` / `failed`）と入力のハッシュを記録する。

| 工程 | 中身 | 入力のハッシュ |
| --- | --- | --- |
| `plan` | `dataset.yaml` の取得元から、対象の年度の論理資料（年度×方式×形式）の一覧を作り、`sources` に予定として登録する | 設定とdataset.yaml |
| `fetch` | 予定の資料を取得する（4.2の規則）。資料ごとに確定する。成功した資料は再開時に取り直さない | 資料の一覧 |
| `normalize` | 最初に、実行設定の文字列（`question`、`scope` の指示と条件の値など）に V7 の氏名の検査を当て、当たれば `E_V7` で止める（氏名の一覧は取得した原本から作るので、ここで行う。D-037）。氏名の一覧（と取り込まない列の値）を作る前に、各 CSV の様式（見出しの文字列・列の数。品質検査の Q02・Q03 と同じ）を確かめ、違えば担当官の列の位置を決められないので、氏名の検査ができたとみなさず `E_FORMAT_CHANGED` で止める（設定の全文を保存しない。D-059）。通れば、1つのトランザクションで `runs.config_json` を設定の全文にし、検査済みを `events`（`kind='check'`、対象は実行ID、`after` に `target: run_config`・`check: V7_PERSONAL_NAME`・`result: passed`・`config_sha256`）に残す（D-040①）。CSVを正規化の経路で読み、`records` と `procurement_contracts` に書く。氏名を捨て、取り込まない列を除く | 資料のSHA-256、dataset.yaml、parser_version |
| `quality` | 品質検査を行い、`quality_report.md` と `runs.counts_json` に書く。必須の検査が1つでも実行できなければ `failed` | 前の工程の出力 |
| `reconcile_import` | 別経路の読み込み処理で読み直し、正規化した値と行ごとに比べる（T6a）。資料ごとに照合の計算を1つ登録する | 同上 |
| `reconcile_source` | CSVの全行をPDFと照合する（T6b。アダプタ仕様7章）。資料の組ごとに照合の計算を1つ登録する | CSVとPDFのSHA-256 |
| `detect` | 検出SQLを実行し、候補・自動の基礎集計・同じ区分の件数・年度の系列を登録する（4.8） | 前の工程の出力と検出SQLのハッシュ |

4. すべて終われば `completed`。取得予定の一部が取れなかった、または一部の検出SQLが動かなかった場合は `partial` とし、欠けたものを `runs.stop_reason` に書く。欠けた資料に依存する検出SQL（`dataset.yaml` の `screens` の `requires`。4.8）は実行しない。それ以外の失敗は `failed`。
5. 成功したら設定ファイルを `accepted/` に写し、`inbox/` から消す。

**再開（`--resume`）**

- 対象は、状態が `running`（強制終了で残ったもの）か、`stop_reason` が一時的な通信の失敗の `failed` だけ。`partial` と `completed` は `E_NOT_RESUMABLE`。
- 再開で使う設定は、次のとおり決める（D-040①）。

| 台帳の設定 | `inbox/` の開始時の設定ファイル（最初の `state_change` の `config_file`） | 扱い |
| --- | --- | --- |
| 全文の保存の前（`name_check: pending`） | あり、正規化JSONのSHA-256が `config_sha256` と同じ（キーの順と空白だけの違いは同じ） | そのファイルの設定で再開する |
| 全文の保存の前 | ない | `E_NOT_RESUMABLE` |
| 全文の保存の前 | 中身か数値の型（`2024` と `2024.0` など）が違う、JSONとして読めない | `E_INPUT_CHANGED` |
| 検査済みの全文を保存した後 | 問わない | 台帳の設定で再開する（成功したときの設定ファイルの扱いは下のD-038⑬） |

- 終わった工程は、入力のハッシュがいまと同じなら飛ばす。違えば `E_INPUT_CHANGED` で止め、新しい実行を求める。入力のハッシュは、記録したときと同じ関数で、いまの台帳・`dataset.yaml`・分野のSQLから計算し直す。
- 規則の版（3.5の照合する4つ）が開始時と違えば、未実行の工程だけが残っていても `E_INPUT_CHANGED` にする（途中から違う規則で続けると `check`・`render` が `E_RULE_VERSION` になるため。D-038⑫）。
- 取得済みの原本（`kind` が `dataset` の CSV・PDF と `index` の公表ページの HTML）が `data/raw/` にないか、中身の SHA-256 が `sources.sha256` と違えば、`E_INPUT_CHANGED`（資料IDと「ない」「SHA-256 が違う」の別を書く）。工程の入力のハッシュは台帳の SHA-256 から作るので、原本そのものをここで照らす（D-038⑫）。
- 以上の拒否は、状態を変える前に行い、台帳を変えない（`events` にも残さない）。通信もしない。
- 取得は、予定のまま（`planned`）と取得できなかった（`failed`）資料を、一時的な失敗かどうかによらず取り直す（強制終了では一時的な失敗の印が残らないため）。非公表（`not_published`）は取り直さない。取得済みの資料と公表ページは取り直さず、照らした原本を使う。通信回数・バイト数の上限は開始時からの合計を引き継ぐ（D-038⑪）。
- `failed` から再開するときは、状態を `running` に戻し、`stop_reason`・`finished_at` を消す（`events` に `failed` → `running`）。
- `completed`・`partial` になったら、開始時の設定ファイル（最初の `state_change` の `events` の `config_file`）が `inbox/` に同じ中身で残っていれば `accepted/` に写して消す。同じ中身かは、キーの順と空白を問わず、数値の型（`2024` と `2024.0`、`true` と `1`）を区別する正規化JSON（11章の正規化）で照らす。なければ、または中身が違えば、台帳の設定（開始時のもの）を `accepted/` に書き、`inbox/` のファイルは触らない（D-038⑬）。
- `detect` が終わっていれば候補を作り直さない。途中で止まった `detect` は、トランザクションが確定していないので最初からやり直す（候補は `(screen_id, lead_key, change_category, period_from, period_to)` で一意。5章の `leads` のUNIQUE）。
- `events` に `resume` を記録する。

### 4.2 `fetch`

```text
kizashi fetch <run_id> <URL> [--index | --definition]
kizashi fetch <run_id> --recheck <source_id>
```

- 許可リスト（`dataset.yaml` の `fetch.allowed_hosts`。初期値は `*.go.jp` と `*.lg.jp`）のHTTPSだけ。リダイレクトは自動で追わず、1段ずつ許可リストで検査する（最大5段）。名前解決先の検査はP1。
- 1応答25MBを超えたら打ち切って `failed`。実行全体の合計が100MBを超えたら `E_LIMIT_BYTES`。
- 一時ファイルに保存し、形式（拡張子と中身の先頭）、空でないこと、HTMLのエラーページでないこと、期待する見出し・年度（データの場合）を検査してから、`data/raw/<sha256>.<拡張子>` に移し、`sources` に登録する。
- PDFとHTMLは、ページ別テキストを `evidence/<source_id>/pages/<n>.txt` に保存する。HTMLは1ページ扱いで、ページ内のリンクの一覧も返す。ページ別テキストとリンクの文は、書く前に、実行の担当官の氏名（V7 と同じ、姓名をつないだ3通り）を「（氏名）」に替える。原本（`data/raw`）は書き換えない。取得の結果には替えた数（`masked_names`）だけを返し、氏名は返さない。未知の氏名を見つけることは保証しない。氏名の一覧を作る原本の CSV が `data/raw` になければ、伏せられないので `E_V7` で止める（D-062）。ただし、公表一覧のPDF（`kind='dataset'`）はページ別テキストを作らない。担当官の氏名をファイルに書き出さないためである。原本照合は、PDFから照合に使う列のセルだけを切り出して行う（アダプタ仕様7.1。D-021）。
- `--index`：リンクをたどるためのページ。上限に数えず、`sources.kind='index'`。主張の根拠にできない（V2）。
- `--definition`：項目の定義や記入要領。上限に数えず、根拠に使える。
- 指定なし：追加資料（`kind='evidence'`）。`MAX_EVIDENCE_FETCH` に数える。
- 追加資料・`--index`・`--definition` では、公表一覧のデータ（`dataset.yaml` の `link_pattern` に合う URL と、run が取得した資料と同じ中身）を取得しない（`E_BAD_EVIDENCE_SOURCE`。ページ別テキストに担当官の氏名を書き出さないため。D-021・D-031⑪）。URL は、問い合わせ（`?…`）と断片（`#…`）を除き、`%` の符号を戻したパスで、大文字と小文字を区別せずに照らす。リダイレクトの途中と最後の URL も、その URL へ通信する前に照らし、合えば取得の失敗（`failed`）として残す（D-034）。
- `--recheck <source_id>`：資料を取り直す。`kind='dataset'` の資料は、公表ページから同じ論理資料（`logical_key`）のリンクをたどり直す。それ以外は元のURLで取り直す。ハッシュが同じなら何もしない。違えば新しい資料IDで登録し（`supersedes` に古い資料ID）、古い資料を根拠にした主張を `stale` にし、`events` に `recheck` を記録する。通信の上限には数え、追加資料の上限には数えない。
  - 取得済みの資料だけを受け付ける（ほかと、新しい版がある資料は `E_USAGE`）。取り直しに失敗しても資料を足さない（通信の計上だけ残す）。ハッシュが同じなら `events` も書かない。公表一覧の新しい版は正規化せず、V1・検証が依存する資料・引用の CSV の引き当ては、実行が取得した版（`supersedes` のないもの）に限る（D-053⑬）。
  - `stale` にするのは、根拠の資料か、参照する計算（入れ子を含む）の行・文書値・照合の資料が古い版の主張。主張のほかに、検証が依存する資料（9章。判定・対応・係数の根拠、該当する行のない依存する CSV を含む）が古い版の候補を `affected_leads` として返す。その候補のメモは失効と表示され（11章）、`check` と `render` は `V2_STALE_OR_WITHDRAWN` で通らない。追加資料なら新しい版を根拠に登録し直し、公表一覧なら同じ設定で新しい実行を作る（D-056①）。
- 取得に失敗した資料も `sources` に `failed` と理由を残す。ハッシュは求めない。

### 4.3 `inspect`

```text
kizashi inspect <run_id> [--page <n>]
kizashi inspect <ID> --run <run_id> [--page <n>]
```

IDは実行ごとの台帳の中でだけ一意なので、実行ID以外を見るときは `--run` を必須にする（ないと `E_BAD_ID`）。IDの接頭辞で対象を決める。台帳は変えない。読み取り専用の接続で開く。

| 対象 | 表示 |
| --- | --- |
| 実行ID | 問い、状態、工程の状態、件数、上限の消費、候補の一覧（区分ごとに差の絶対値の大きい順、1ページ20件、総数と未調査の件数）、次の作業 |
| 候補ID | 比較の単位、区分、計算（値・意味・単位）、年度の系列、同じ区分のほかの件数（同じ実行の検出の候補の数。探索の候補は数えない。4.8）、比較の条件、比較できない理由、仮説、判定（判定ごとの説明を反映した値 `recalc_calc_id`・残る差 `residual_calc_id`・説明によって足した条件 `reflected_conditions` を含む。`review-lead` が `V8_EXPLANATION_STATUS` の条件と、根拠と条件の意味の対応を点検できるように出す。D-045③。これらの列のない古い版の台帳では、台帳を書き換えずに未記録（null と `unrecorded`）として出す。D-047）、主張、取材質問、状態、巡回数、メモの番号と失効の有無、同種業務の対応（matches）、係数の提案（coefficients）。対応と係数の提案は、`review-lead` が1回の `inspect` で `V8_MATCH_UNCONFIRMED` と `V8_COEFFICIENT_PENDING` に当たる状態を確かめられるように出す（D-023） |
| 資料ID | URL、取得日時、SHA-256、状態、新しい版の有無、ページ数 |
| レコード（`row_ref`） | 元のセルの値（`cells_json`。キーは `take` のキーで、見出しを添える。氏名を除いた後）、分野の表の正規化した値、照合の結果 |
| 計算ID | 種類、依頼、結果、`row_refs` の件数とハッシュ（一覧はページ分け） |
| 主張ID | 種類、本文、状態、根拠 |

失効の判定：候補のメモがあれば、`hashing.check_hash` で計算し直したハッシュと `leads.memo_hash` を比べ、違えば「失効」と表示する。候補の `memo` に `memo_no`・`file`・`check_hash`・`current_hash`・`stale`・`status`（有効・失効・メモなし）・`note` を出す。規則の版の食い違いは表示を止めない（`check`・`render` が `E_RULE_VERSION`。D-053⑭）。

### 4.4 `query`

```text
kizashi query <run_id> inbox/<名前>.sql
```

- `file:<台帳>?mode=ro` で開き、`PRAGMA query_only = ON` にする。
- SQLiteのauthorizerで、SELECT と読み取りの関数以外（INSERT、UPDATE、DELETE、CREATE、DROP、ATTACH、PRAGMA、拡張の読み込み）を拒否する。
- 1つの文だけを実行する。`sqlite3.complete_statement` と文の区切りの検査で、複数の文を拒否する。
- 実行の前に、SQLの全文（文字列とコメントを含む）に V7 の氏名の検査（9章）を当て、当たれば `E_V7` で拒否する。`queries/` にも `accepted/` にも保存せず、提案は `inbox/` に残す（D-037）。
- 実行の後、`queries/` への保存・`events` の記録・結果の返却の前に、結果の列名と文字列のセルに同じ検査を当てる。SQLiteのエラーの文（`E_QUERY_REJECTED` に書く文）にも当てる。当たれば `E_V7` とし、元の結果もエラーの文も出さず、位置（`結果の3行目・2列目`、`列名の2列目`、`SQLite のエラーの文` と文字の位置）だけを書く。保存しないこと、提案を `inbox/` に残すことは本文の検査と同じ（文字列の結合などで本文の検査を通った氏名を出さないため。D-040②）。
- 結果は最大50行。実行時間が10秒を超えたら中断する（`set_progress_handler`）。
- SQLと結果を `queries/` に保存し、`events` に記録する。返すJSONに `"citable": false` を付ける。
- 成功したらSQLファイルを `accepted/` に写して消す。

### 4.5 `record`

```text
kizashi record <run_id> inbox/<名前>.json
```

- `inbox.load_proposal` で場所と形を検査し、`kind` ごとの処理（6章）に渡す。
- 管理項目（`state`、`checked`、`created_at`、`rounds` など）が提案にあれば `E_SELF_DECLARED` で拒否する。
- 参照先（候補、資料、計算、レコード、主張）が存在しなければ `E_REF_NOT_FOUND`。
- 引用があれば `quotes.verify_quote` で照合し、失敗すれば登録しない。
- V7の数値と氏名の検査（9章）を、Claudeが書く文字列に対して行う。不合格なら登録しない（巡回数は使わない）。強い語は警告。
- 1つのトランザクションで登録し、成功したら提案を `accepted/<連番>-<kind>.json` に写して消す。失敗したら提案を残し、直す箇所を返す。
- `events` に `record` を記録する。
- 最初に規則の版を照らし（3.5の照合する4つ。`check`・`render` と同じ）、違えば `E_RULE_VERSION`（終了コード3）で拒否する（版の違う台帳に登録しても `check`・`render` ができないため。古い台帳の表の形の違いを SQL の例外にしない。D-042⑤）。提案は `inbox/` に残し、台帳（開始時の版を含む）を変えず、`next_action` で新しい実行が要ることを示す。記録だけのもの（`code_version`・`git_commit`・Skill とルールのファイル）の違いでは拒否しない（D-043④）。

### 4.6 `check`

```text
kizashi check <lead_id> --run <run_id>
kizashi check --publish examples/publish.manifest --run <run_id>
```

- 規則の版を照らす（3.5）。
- V1〜V8（9章）を順に行い、不合格をすべて集めて返す（最初の1件で止めない。3.2の `failures`）。P0-6では V1〜V4・V8 を行い、V5・V6と check の時点の V7 は P0-8 で足す（V7 は `record` の時点で行っている）。
- 不合格でも、T6a・T6bの判定（9章の末尾）、巡回数、`events` の記録は確定する。主張は `checked` にしない（D-041②）。
- 結論の案が登録されている場合、合格・不合格にかかわらず `leads.rounds` を1増やす。ただし結論の案が保留の場合は数えない。`render` の中の検査も数えない。`rounds` が上限を超えた候補は、保留以外の結論の案を `E_ROUNDS_EXCEEDED` で拒否する。上限は `record` の結論の案に加えて `check` でも強制し、巡回数が上限に達した候補で保留以外の結論の案の `check` は、数えず検査もせずに `E_ROUNDS_EXCEEDED` にする（設計書4.3 の「強制する場所：check」。D-053⑫）。上限は `MAX_ROUNDS`、保留にした候補（3.6）は＋1。
- 合格したら、メモに出る主張を `checked` にする。
- 結果と検証記録のハッシュを `events` に記録する。

`--publish` は12章。

### 4.7 `render`

```text
kizashi render <lead_id> --run <run_id> [--public]
kizashi render <run_id> [--public]
```

- 候補：1つのトランザクションの中で、①`check` を行い（巡回数に数えない）、②合格したら主張を `checked`、`leads.state` を結論の案の値にし、③その後の台帳から検証記録のハッシュを計算し（11章）、④メモを `memos/<lead_id>.v<n>.md` に書き（`n` は1から。上書きしない）、⑤`leads.memo_no` と `leads.memo_hash` を更新してから確定する。確定に失敗したら書いたメモを消す。不合格ならエラーを返し、何も書かない。この順にしないと、書いた直後のメモが失効と表示される。
- 巡回数が上限に達した候補で、結論の案が保留以外なら、②の主張の更新の後・状態の更新の前の台帳の検証記録のハッシュが、最後に数えた `check`（`events` の `counted`）に合格したときのハッシュか、最後に書いたメモのハッシュと同じときだけ書く。違えば `E_ROUNDS_EXCEEDED` で何も書かない（`render` の中の検査は数えないので、上限に達した後に直した中身を `render` で通さない。2巡目に合格した中身はそのまま書ける。D-056②）。
- 実行：実行の概要を `memos/run_summary.v<n>.md` に書く（10.3）。
- `--public`：書いたメモの伏せた写しを `public/` に書く（12章）。

### 4.8 検出と自動の基礎集計（`run` の `detect`）

- `datasets/<分野>/screens/*.sql` を順に実行する。各SQLは、名前付きのパラメーター `:year_from` と `:year_to`（`compare_years`）を受け取り、次の列を返す（アダプタ仕様9章）。

| 列 | 中身 |
| --- | --- |
| `lead_key` | 比較の単位（例：同種業務のまとめのキー） |
| `change_category` | 変化の区分（例：`amount_up`、`kyoso_to_zuikei`、`reason_changed`） |
| `metric` | 指標（例：`amount_yen_sum`） |
| `period_from`・`period_to` | 比べた年度 |
| `row_refs_from`・`row_refs_to` | それぞれの年度の、候補に属する行すべて（JSONの配列） |
| `excluded_from`・`excluded_to` | そのうち合計から除いた行と理由（`[{"ref": "S004:000042", "reason": "unit_price"}]`。除く行がない検出SQLは `'[]'`） |
| `incomparable_reasons` | 比較できない理由（JSONの配列） |

- CLIは、`excluded_*` の `ref` がすべて `row_refs_*` に含まれ、理由が空でないことを確かめる（違えば検出SQLの誤りとして `E_INTERNAL` で止める）。
- CLIは、各候補について基礎集計（合計と件数）をDecimalで計算し直して登録する（`origin='run'`）。比べる2年度の基礎集計の計算IDを `leads.calc_from`・`leads.calc_to` に入れる（V8_MATCH_UNCONFIRMED とT6a・T6bの判定が使う）。合計の対象は `row_refs_*` から `excluded_*` を除いた行で、基礎集計の `row_refs` にはこの行だけを入れる。除いた行と理由と件数は、基礎集計の `request_json` に記録し、メモの比較の条件と記者の確認リストに出す。どの行を除くかを決めるのは分野のSQL（検出SQLと、同じ除き方の系列のSQL）だけとし、CLIは同じ判定を別に書かない。
- 金額の検出（S1）だけ、差額と増減率を派生計算として登録する。基準値が0以下なら増減率を作らない。`amount_incomparable`（片方の年度に合計できる行がない）では件数だけを登録し、差額と増減率を作らない。
- CLIは、`incomparable_reasons` の `null` を除き、候補の `row_refs_*` の全行（除いた行を含む）の分野の表の `incomparable_reasons_json`（重複、読めない日付など）と、`dataset.yaml` の年度の理由（比べる年度が年度途中なら `partial_year`）を合わせ、重複を除いて文字の順に並べる（D-038⑥）。
- 候補の比較の条件（`leads.comparison_json`：母集団、1行の意味、期間、単位、主体の対応、算式）を、`dataset.yaml` の `comparison_defaults`（検出SQLごと）で埋める。
- 基礎集計は年度ごとに、`row_refs_*` から `excluded_*` を除いた行の件数（単位 `件`）を必ず登録する。`metric` が `<列>_sum`（列は分野アダプタの `SUM_LABELS`）の検出SQLでは、この件数は「合計対象行の件数」で（`meaning` にそう書く）、金額の合計ではない。両年度に合計できる行があれば合計も登録し、`calc_from`・`calc_to` を合計にする。`amount_incomparable` では `calc_from`・`calc_to` を合計対象行の件数（単位 `件`）にする。件数と派生計算は `lead_id` で候補に結ぶ（D-038②）。
- 同じ実行の、同じ `change_category` の検出の候補（`screen_id` が検出SQLの名前のもの）の数から、自分を除いた数を `peer_count` に入れる。探索の候補（`screen_id='explore'`）は数えず、探索の候補の `peer_count` は空のまま。所属や部局の別では数えない（「ほかの部局の数」ではない。D-038①）。
- 年度の系列：検出SQLと同じ名前の `datasets/<分野>/series/<名前>.sql` を、`series_years` の年度ごとに名前付きのパラメーター `:year` で実行する。列は `lead_key`・`fiscal_year`・`row_refs`（その年度の、まとめに属する行すべて）・`excluded`（合計から除く行と理由。書式は `excluded_*` と同じ）。行の選び方と除き方は検出SQLと同じにする。CLIは、比べる2年度で、系列のSQLの `row_refs` と `excluded` が検出SQLと同じかを候補ごとに照らす（違えば分野のSQLの誤りとして `E_INTERNAL`）。
- 比べる2年度は `calc_from`・`calc_to` を系列に使う。ほかの年度も比べる2年度と同じ数え方で、合計対象行の件数を必ず登録し（除いた行と理由と件数を `request_json` に残す）、合計の候補で合計できる行があれば合計を登録して系列に入れる。件数の候補（`amount_incomparable` と S2・S3）は件数を系列に入れる。系列は `series_years` の順に計算IDを並べて `leads.calc_ids_json` に入れる。
- 合計の候補で、その年度に合計できる行がない（行がない、または行がすべて合計から除かれる）年度は、合計を作らず（0円にしない）系列に入れず、候補の `note` に年度と合計対象行の件数の計算IDを書く。その年度の資料（その検出SQLの `requires` の方式）が取得・品質検査を通っていなければ、系列のSQLを実行せず計算も作らず（0件・0円にしない。設計書5.2・M13）、`note` に年度と欠けた資料を書く。年度途中の年度（`dataset.yaml` の `files` の `partial_year`）の計算は `meaning` に「（年度途中）」を付ける（D-038④⑤）。
- 検出SQLごとに、比べた組の数と、比較できなかった組の数を `runs.counts_json` に入れる。数え方は、検出SQLと同じ名前の `datasets/<分野>/populations/<名前>.sql`（`compared` と `incomparable` の2列を返す）で定める。`screens/` に置くと検出SQLとして実行されるため、場所を分ける。
- 検出SQLを実行する前に、`dataset.yaml` の `screens.<名前>.requires`（`compare_years` の両年度でそろっていなければならない資料の種類）を確かめる。1つでも取得・品質検査を通っていなければ、検出SQLも母集団のSQLも実行せず、`runs.counts_json` に `{"<名前>": {"state": "not_run", "missing": ["kyoso:2025", ...]}}` と書く。`compared: 0` とは書かない（「比べた組が0」と「実行していない」を混同しないため）。実行した検出SQLは `{"state": "done", "compared": N, "incomparable": M, "leads": K}` と書く。資料がそろうかは、その年度・方式の CSV が `fetched` で、品質検査の結果（`counts_json` の `quality`）にあることで決める（D-038⑧）。実行しなかった検出SQLは `runs.stop_reason` の `screens_not_run`（`[{"screen": "<名前>", "missing": [...]}]`）に、取得できなかった資料（`missing`）と並べて書き、どちらかがあれば `partial` にする（D-038⑦）。
- 分野のSQL・`dataset.yaml` の誤り（`screens/` のSQLと `dataset.yaml` の `screens` の名前が合わない、`requires` が方式の配列でない、母集団のSQL・系列のSQLがない、`comparison_defaults` が6項目でない、列が違う、`excluded` の行が `row_refs` にない・理由が空、期間が `compare_years` と違う、`change_category` が `reconcile.fields_by_category` にない、同じ候補を2回返す、比べる2年度で系列のSQLの行が検出SQLと違う）は `E_INTERNAL` で `detect` を `failed` にし、トランザクションを巻き戻して候補を残さない（D-038⑨）。
- 候補の採番は、検出SQLの名前の順、その中で `(change_category, lead_key)` の順。検出の候補の `query_file` は空（D-038⑩）。

## 5. 台帳（DDL）

`src/kizashi/schema.sql` に置く。接続のたびに `PRAGMA foreign_keys = ON` と `PRAGMA journal_mode = WAL` を設定する。

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  config_json TEXT NOT NULL,
  profile TEXT,
  state TEXT NOT NULL CHECK (state IN ('running','completed','partial','failed')),
  stop_reason TEXT,
  stages_json TEXT NOT NULL DEFAULT '{}',
  rule_versions_json TEXT NOT NULL,
  knowledge_hashes_json TEXT,
  counts_json TEXT NOT NULL DEFAULT '{}',
  limits_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE sources (
  source_id TEXT PRIMARY KEY,
  logical_key TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('dataset','evidence','definition','index')),
  url TEXT NOT NULL,
  final_url TEXT,
  referrer_url TEXT,
  fetched_at TEXT,
  sha256 TEXT,
  supersedes TEXT REFERENCES sources(source_id),
  media_type TEXT,
  encoding TEXT,
  size_bytes INTEGER,
  page_count INTEGER,
  status TEXT NOT NULL CHECK (status IN ('planned','fetched','not_published','failed','out_of_scope')),
  failure_reason TEXT,
  published_date TEXT,
  period TEXT,
  terms_note TEXT
);

CREATE TABLE records (
  record_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  logical_no INTEGER NOT NULL,
  cells_json TEXT NOT NULL,
  cells_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('adopted','held','excluded')),
  status_reason TEXT,
  parser_version TEXT NOT NULL,
  UNIQUE (source_id, logical_no)
);

CREATE TABLE leads (
  lead_id TEXT PRIMARY KEY,
  screen_id TEXT NOT NULL,
  query_file TEXT,
  lead_key TEXT NOT NULL,
  change_category TEXT NOT NULL,
  metric TEXT,
  period_from TEXT,
  period_to TEXT,
  calc_from TEXT,
  calc_to TEXT,
  calc_ids_json TEXT NOT NULL DEFAULT '[]',
  peer_count INTEGER,
  comparison_json TEXT NOT NULL DEFAULT '{}',
  note TEXT,
  comparison_state TEXT CHECK (comparison_state IN ('comparable','adjusted','unresolved','not_applicable')),
  adjustment_json TEXT,
  incomparable_reasons_json TEXT NOT NULL DEFAULT '[]',
  hypothesis TEXT,
  withdraw_if TEXT,
  hypothesis_result TEXT CHECK (hypothesis_result IN ('kept','withdrawn')),
  questions_json TEXT NOT NULL DEFAULT '[]',
  coefficients_json TEXT NOT NULL DEFAULT '[]',
  proposed_outcome TEXT CHECK (proposed_outcome IN ('change','artifact','hold')),
  explanation_status TEXT CHECK (explanation_status IN ('none','part','full','unknown')),
  hold_reason TEXT CHECK (hold_reason IN ('comparison','source_check','limit')),
  state TEXT NOT NULL DEFAULT 'candidate' CHECK (state IN ('candidate','investigating','change','artifact','hold')),
  reopen_condition TEXT,
  rounds INTEGER NOT NULL DEFAULT 0,
  memo_no INTEGER NOT NULL DEFAULT 0,
  memo_hash TEXT,
  UNIQUE (screen_id, lead_key, change_category, period_from, period_to)
);

CREATE TABLE computations (
  calc_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('base','doc_value','derived','reconcile')),
  lead_id TEXT REFERENCES leads(lead_id),
  request_json TEXT NOT NULL,
  sql_text TEXT,
  sql_sha256 TEXT,
  formula TEXT,
  ref_calc_ids_json TEXT,
  value TEXT,
  unit TEXT,
  meaning TEXT NOT NULL,
  row_refs_json TEXT,
  row_refs_sha256 TEXT,
  row_count INTEGER,
  result_json TEXT,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','withdrawn')),
  origin TEXT NOT NULL CHECK (origin IN ('run','record')),
  created_at TEXT NOT NULL
);

CREATE TABLE claims (
  claim_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL REFERENCES leads(lead_id),
  type TEXT NOT NULL CHECK (type IN ('record','observation','publisher_explanation','hypothesis')),
  fiscal_year INTEGER,
  text TEXT NOT NULL,
  calc_ids_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','checked','held','withdrawn','stale')),
  replaces TEXT REFERENCES claims(claim_id),
  created_at TEXT NOT NULL,
  CHECK (type <> 'record' OR fiscal_year IS NOT NULL)
);

CREATE TABLE explanations (
  lead_id TEXT NOT NULL REFERENCES leads(lead_id),
  type TEXT NOT NULL CHECK (type IN ('T1','T2','T3','T4','T5','T6a','T6b','T6c')),
  judgment TEXT NOT NULL CHECK (judgment IN ('support','refute','unknown','na')),
  evidence_json TEXT NOT NULL DEFAULT '[]',
  searched TEXT,
  recalc_calc_id TEXT REFERENCES computations(calc_id),
  residual_calc_id TEXT REFERENCES computations(calc_id),
  reflected_conditions_json TEXT,
  scope TEXT CHECK (scope IN ('full','part','unknown')),
  no_quant_reason TEXT,
  context TEXT NOT NULL DEFAULT 'main_session',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (lead_id, type)
);

CREATE TABLE events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('state_change','record','query','check','render','correction','recheck','resume','stage')),
  before_json TEXT,
  after_json TEXT,
  reason TEXT,
  result_json TEXT,
  check_hash TEXT,
  at TEXT NOT NULL
);
```

`records.cells_json` は、`dataset.yaml` の `take` のキー（例 `title_raw`、`amount_raw`）をキー、CSVのセルの元の文字列（前後の空白だけ除いたもの）を値とするJSONオブジェクトである。`take` にない列（`drop`）は入れない。担当官のセル（`officer_raw`）は入れず、所属を分野の表の `officer_units_json` に入れる（氏名を保存しないため。アダプタ仕様4.3）。引用の照合（8章）、記者の確認リスト（10.1）、`inspect` はこのキーを使う。

分野の表（`procurement_contracts`、`procurement_matches`）はアダプタ仕様8章。根拠（`evidence_json`）、取材質問（`questions_json`）、`row_refs` はJSONの配列で持ち、参照先はCLIが検査する（外部キーでは検査できないため）。

## 6. 提案ファイル（inbox）の書式

### 6.1 共通

- 1ファイルに1件。UTF-8のJSON。最上位に `kind` を必ず置く。
- 文字列の中の数値の書き方：Claudeが書く本文（`text`、`hypothesis`、`question` など）には金額・件数・比率の数字を書かず、`{calc:K0012}` で参照する（V7）。年度、日付、条文番号、ID（型・検出・品質検査のIDを含む）は書いてよい（9章のV7の対象外の形）。
- 根拠の書式（`evidence` の要素）：

```json
{"source_id": "S010", "position": {"page": 3}, "quote": "…文末までの引用。", "role": "support"}
{"source_id": "S004", "position": {"row_ref": "S004:000042", "column": "amount_raw"}, "quote": "7,793,940", "role": "support"}
```

`role` は `support`（支持）・`oppose`（反対）・`background`（背景）。PDFとWebページは `page` を、CSVは `row_ref` と `column` を使う。公表一覧のPDF（`kind='dataset'`）は `page` で根拠にできない（`E_BAD_EVIDENCE_SOURCE`。ページ別テキストがないため。同じ記載は同じ年度・方式のCSVの `row_ref` で引用する。D-021）。`column` は `dataset.yaml` の `take` のキー（例 `amount_raw`。`records.cells_json` のキーと同じ）で書く。見出しの文字列（`契約金額`）は受け付けない。`cells_json` にないキーは `E_REF_NOT_FOUND` とし、エラーには使えるキーと見出しの対応を返す。メモの記者の確認リストでは、キーを `take` の `header`（原本の見出し）に置き換えて表示する。

### 6.2 種類

| `kind` | 必須の項目 | 処理 |
| --- | --- | --- |
| `run_config` | `domain`、`compare_years`（連続した2年度。例 `[2024, 2025]`）、`series_years`（系列として読む年度。`compare_years` を含む。例 `[2023, 2024, 2025]`）、`question`、`model`。任意で `scope`（母集団の絞り込み。記者の指示があるときだけ。6.3。D-013） | `run` が読む。`record` では受け付けない。Claude Codeの版は `KIZASHI_CLAUDE_VERSION` から記録する（3.5） |
| `calc` | `op`（`sum` / `count`）、`table`、`column`（sumのとき）、`filter`（`[{"col","op","value"}]`。`op` は `=`、`!=`、`in`、`is_null`、`not_null`。`fiscal_year` の値は整数（`in` は整数の配列）だけで、`"2024"` のような文字列は `E_SCHEMA`。D-042②）、`label`、`unit`。任意で `lead_id`（探索で見つけた候補を登録する前の計算では省く） | 7.1。計算IDを返す |
| `derive` | `op`（`diff`・`ratio`・`rate`・`sum`・`product`）、`args`（計算IDの配列）、`label`、`unit`。任意で `lead_id` | 7.3 |
| `doc_value` | `lead_id`、`source_id`、`position`、`quote`、`value`（十進数の文字列）、`label`、`unit` | 7.2 |
| `lead` | `query_file`（`accepted/` に写した探索SQLの名前）、`lead_key`、`change_category`、`period_from`、`period_to`、`calc_from`・`calc_to`（比べる2年度の基礎集計の計算ID）、`calc_ids`（ほかに関係する計算。空でよい）、`comparison`（母集団、1行の意味、期間、単位、主体の対応、算式）、`note` | 探索で見つけた候補を `screen_id='explore'` で登録し、`calc_from`・`calc_to`・`calc_ids` の計算の `lead_id` を埋める。`calc_from`・`calc_to` が基礎集計でない、または条件（`filter`）に `fiscal_year` = `period_from`・`period_to` をそれぞれ含まなければ `E_SCHEMA`。比べた組の数（探索）を1増やす |
| `hypothesis` | `lead_id`、`text`、`withdraw_if`。結果を書くときは `result`（`kept` / `withdrawn`） | 最初の登録で候補を `investigating` にする（調べる候補が3件を超えるなら `E_LIMIT_LEADS`） |
| `match` | `lead_id`、`left`・`right`（`row_ref` の配列）、`relation`（`1:1` / `1:n` / `n:1` / `n:m`）、`reason`、`evidence`、`status`（`confirmed` / `pending`） | 分野の対応表に登録する |
| `claim` | `lead_id`、`type`、`text`、`calc_ids`、`evidence`。`type` が `record` のときは `fiscal_year` | 主張を `draft` で登録する |
| `explanation` | `lead_id`、`type`（T1〜T5、T6c）、`judgment`、`evidence`、`searched`。支持のT1・T3・T4では `scope` と、`recalc_calc_id` または `no_quant_reason`。任意で `residual_calc_id`（説明を反映した後に残る差、または候補の行から根拠で説明できる行を除いた残りの件数の計算ID（D-046）。説明の状況を「全部」「一部」にするときは必須。9章の `V8_EXPLANATION_STATUS`。D-041⑩）と `reflected_conditions`（説明によって足した・変えた条件で、金額以外の候補では残りの行を選ぶ条件。`filter` の要素の形の配列。金額以外の候補で残る件数を残る差にするときは必須。D-043②・D-046） | 型ごとに上書きする（前の値は `events` に残る）。T6a・T6bはCLIが照合の計算から登録するため、提案では受け付けない（9章の末尾） |
| `question` | `lead_id`、`to`、`question`、`documents`、`decision_impact`、`purposes`（質問の目的の空でない配列：`verify_record` 元の記載の確認、`reason_breakdown` 理由・内訳の確認、`missing_documents` 不足資料の照会、`other` そのほか。D-041⑩）。任意で `foia` | `leads.questions_json` に足す |
| `coefficient` | `lead_id`、`key`（短い名前）、`value`（十進数の文字列）、`meaning`、`why_needed`、`apply_to`（どの計算にどう使うか）、`basis`（推測の手がかり。6.4）、`status`（`proposed` / `accepted` / `declined`）。`accepted`・`declined` では `reporter_note`（記者の判断の要約） | 係数の提案として `leads.coefficients_json` に `(lead_id, key)` で上書きする（前の値は `events` に残る）。計算には使わない（6.4。D-012） |
| `conclusion` | `lead_id`、`outcome`、`comparison_state`。`change` なら `explanation_status`、`adjusted` なら `adjustment`（方法、補正前後の計算ID）、`hold` なら `hold_reason`。任意で `reopen_condition`、`comparison`（比較の条件のうち既定値と違うもの） | 結論の案として登録する。`comparison` は `leads.comparison_json` に上書きする。確定は `render` |
| `withdraw` | `target`（主張ID・対応ID・計算ID）、`reason` | 取り下げ。主張は `withdrawn` にし、参照している主張を `stale` にする。訂正は新しい主張を登録したうえで古い主張を取り下げる |

`record-examples.md`（`investigate` のSkillのフォルダー）に、種類ごとの例を1件ずつ置く。

### 6.3 母集団の絞り込み（`run_config.scope`）

記者が担当部局や業務の種類などで対象を絞るよう指示したときだけ使う。指示がなければ書かない（Claudeの判断で絞らない。設計書の工程0「不都合な結果を理由に対象を黙って狭めない」）。

```json
"scope": {"instruction": "記者の指示の要約（例：食品安全委員会事務局の契約に絞る）",
          "filters": [{"field": "officer_unit", "op": "contains", "value": "食品安全委員会"}]}
```

- `field` は `dataset.yaml` の `scope_fields` にあるものだけ、`op` は `contains`・`not_contains`・`equals`・`not_equals` だけ。比べる前に両方をNFKCにし空白を除く。条件はすべてを満たす行を残す（AND）。`scope_fields` で `kind: json_array` の項目（所属の配列など）は、どれかの要素が `contains`・`equals` を満たせば満たすとし、`not_contains`・`not_equals` はどの要素も満たさないときに満たすとする。`instruction` が空、知らない `field`・`op` は `E_SCHEMA`。
- `normalize` の後に適用する。条件に合わない行は、`records.status='excluded'`（`status_reason='out_of_scope'`）と分野の表の `record_status='excluded'` にする。行は消さない。品質検査と、取り込みの照合・原本照合（T6a・T6b）は全行で行う（絞り込みで検査を省かない）。
- 検出SQLと母集団のSQLは `record_status='adopted'` の行だけを読むので、絞り込みは検出・比べた組の数・基礎集計に自動で効く（7.1）。
- 候補の比較の条件の「母集団」に、CLIが「（記者の指示で絞り込み：<instruction>。条件：…）」を書き足す。検出の候補、探索の候補（`record` の `kind: lead`）、結論（`kind: conclusion`）で比較の条件を変えたときのどれにも、同じ処理（`detect.scope_population`）で書き足す。結論で母集団を書き換えても実行時の絞り込みは消えない（同じ書き足しがすでにあれば重ねない。D-038⑮）。実行の概要の「処理の範囲」に、指示、条件、資料ごとの除いた行の数を書く。
- 絞り込みを変えるときは新しい実行にする（`run_config` は実行の入力のハッシュに入る）。

### 6.4 係数の提案（`kind: coefficient`）

資料にない係数（税込と税抜の換算、月数・年数での按分、物価の補正など）は、計算に使わない（派生計算は計算ID同士だけ。7.3）。係数が要ると判断したら、Claudeは計算せず、提案として登録し、記者に伝える。

- `basis` は推測の手がかりの配列で、各要素は `{"file": "playbook" | "case_studies" | "catalog", "locator": "C114 や事例の題名", "quote": "該当箇所の短い引用"}` か、取得した資料の根拠（6.1の書式）。CLIは、知識ファイルの引用が `references/` の該当のファイルにあることを、8章と同じ正規化の部分一致で確かめる（なければ `E_QUOTE_NOT_FOUND`。主張の根拠ではないので、文末までの規則は当てない）。手がかりがなければ空の配列にし、`why_needed` にその旨を書く。知識ファイルは提案の手がかりにだけ使え、主張の根拠にはできない（8章）。
- `why_needed`・`apply_to`・`reporter_note` はV7の対象（生の数値を書かない。係数の値は `value` にだけ書く）。
- P0では、`accepted`（記者が使うと判断）でも計算に使わない。記者が承認した係数を計算に使う仕組みは、承認がClaudeの書けない経路から来ることを確かめる必要があるため、P1とする（13章）。
- 提案が `proposed` か `accepted` のまま残っている候補は、比較成立にできない（`V8_COEFFICIENT_PENDING`）。係数を当てないと比べられないと判断した以上、比べる条件が確定していないためで、保留（`hold_reason: comparison`）にする。`declined`（記者が不要と判断）なら妨げない。
- メモの4節に「補正の係数の提案（推論・未適用）」として、値、意味、必要と考えた理由、手がかり、状態を出す。7節の先頭に「記者が判断すること」として載せる。

## 7. 計算

### 7.1 基礎集計

1. `filter` から、パラメーターを使ったSQL（`SELECT record_id, <column> FROM <table> WHERE ...`）をCLIが組み立てる。表と列は分野の表の許可リスト（アダプタ仕様8章）にあるものだけ。CLIは条件に必ず `record_status = 'adopted'` を加える（品質検査で保留した行と、絞り込みで除いた行を数えないため。6.3）。
2. 選ばれた行の `record_id` を並べ替えて `row_refs` とし、そのSHA-256と件数を記録する。
3. 合計は、列の値を `Decimal(str(value))` にして足す。`NULL` の行は合計に入れず、件数を `request_json` に記録する（「空欄の行」）。SQLiteのSUMは使わない。
4. 件数は行の数。
5. 結果を十進数の文字列で `value` に入れる。

### 7.2 文書値

- 引用を照合したうえで、引用の中の数値の並び（NFKCにした後の、数字と桁区切り `,` と小数点の連なり。前後が数字・`,`・`.` でないもの）を取り出し、桁区切りを除いてDecimalにしたもののどれかが `value` と等しいことを確かめる。部分一致では照らさない（`1,200` の中の `200` は一致としない）。等しいものがなければ `E_DOC_VALUE_MISMATCH`。
- 符号（D-033）：数値の並びの直前に `-`（NFKCの後。全角の `－` を含む）、`−`（U+2212）、`+` があり、その前が数字・英字でなければ、符号として値に含める（`増減は-12件` は `-12`。`10-15`・`2024-03-01`・`A-12` の `-` は符号にしない）。符号・印と数字の間と、符号の前の空白は飛ばして見る（8章の照合が空白を除いて比べるため。D-036）。先頭の `.` も数値の並びに含む（`.5` は `0.5`）。直前に `▲`・`△` がある数値は、負を示すかが資料ごとの決まりなので文書値にせず、`E_DOC_VALUE_MISMATCH` で記者に確かめるよう案内する（同じ引用に印のない同じ数値があれば、それと照らす）。

### 7.3 派生計算

- 演算は次の5つだけとし、引数はすべて同じ実行の計算ID（取り下げていないもの）にする。数値の定数は受け付けない。資料にある数値は文書値（7.2）として登録してから使う（設計書3.4：派生計算は基礎集計と文書値の計算ID同士の四則演算。`docs/decisions.md` の D-010）。

| `op` | 引数 | 計算 | 用途の例 |
| --- | --- | --- | --- |
| `diff` | `[a, b]` | a − b | 差額、説明で残る差 |
| `ratio` | `[a, b]` | a ÷ b | 単価、構成比 |
| `rate` | `[a, b]` | (a − b) ÷ b | 増減率（a が新しい値、b が基準） |
| `sum` | `[a, b, …]`（2つ以上） | a + b + … | 方式をまたぐ合計 |
| `product` | `[a, b]` | a × b | 数量の比で補正した前年度の額 |

- 引数には派生計算の計算IDも使える（V3(c)は基礎集計と文書値までさかのぼって計算し直す）。
- 1つの依頼の中で同じ計算IDを2回使わない（`E_BAD_EXPR`）。`ratio(K1, K1)` のように、資料にない定数を作れないようにするためである。値の等しい別の計算IDを並べれば同じことができるので、その意味の通らない組は `review-lead` と記者の確認に委ねる（残る限界）。
- `decimal.localcontext()` で精度28桁にして計算する。0での割り算は `E_DIV_ZERO`。`rate` の基準が0以下なら `E_DIV_ZERO` と同じく拒否する（V5_BAD_RATE と同じ考え方）。
- 丸めない。表示の丸めは `render` が単位ごとに行い、丸め方は四捨五入（`decimal.ROUND_HALF_UP`）とする。`unit` の文字列と `dataset.yaml` の `display` の対応は、`円` → 整数・桁区切り、`比率` → 100倍して小数1桁の `%`、`件` → 整数、それ以外 → 丸めずに表示とする。
- 提案の `label` は、計算の `meaning`（意味）として保存し、メモの展開に使う。

### 7.4 照合の記録

`kind='reconcile'` の `result_json` は次の形にする。

```json
{"type": "source",
 "pairs": [{"csv_source": "S004", "pdf_source": "S005", "pages": 38,
            "skipped_pages": [], "split_pages": []}],
 "rows": {"S004:000042": {"row_status": "matched", "pdf_page": 3,
                          "fields": {"amount_yen": "match", "contract_date": "match", "reason_raw": "unverifiable"}},
          "S004:000043": {"row_status": "ambiguous", "row_reason": "split_row", "pdf_pages": [3, 4],
                          "fields": {"amount_yen": "unverifiable", "contract_date": "unverifiable", "reason_raw": "unverifiable"}}},
 "counts": {"rows": 858, "matched": 856, "not_found": 0, "ambiguous": 2, "skipped": 0,
            "fields": {"amount_yen": {"match": 855, "mismatch": 1, "unverifiable": 2}}}}
```

項目の結果は `match`（一致）・`mismatch`（不一致）・`unverifiable`（照合不能）。行の結果は `matched`・`not_found`・`ambiguous`。1件が複数の罫線の行に分かれたおそれがある場合は `ambiguous` とし、`"row_reason": "split_row"` を添える（アダプタ仕様7.1）。項目のキーと照らし方はアダプタ仕様7.3（`dataset.yaml` の `reconcile.pdf_fields`）による。

- `counts` は行の結果ごとの件数と、項目ごとの件数（`fields`）にする。`skipped` は照合しなかったレコード（空のレコードと NO が数でない保留の行）の数。
- `pairs` の要素の `pages` は PDF のページ数、`skipped_pages` は列の数が違って使わなかったページ（`{"page", "columns"}`）、`split_pages` は分かれた行のあるページ。`ambiguous` の行には候補のページ `pdf_pages` を添える。
- PDF を取得できていない CSV は照合せず、計算を作らない（`runs.counts_json` に `not_run` と理由）。PDF を読めないときは、組に `error` を書き、全行を `not_found`（`"row_reason": "pdf_unreadable"`）、項目をすべて `unverifiable` にする。実行は止めず、`run` の出力の `message` に照合できなかった資料を書く。
- 照合の計算の `value`・`unit` は空（NULL）とし、`row_refs_json` に照合した行を入れる。

取り込みの照合（T6a）は資料（CSV）ごとに次の形にする。比べる項目は分野アダプタの `IMPORT_FIELDS`（アダプタ仕様6章の元の値）と、整数にした金額 `amount_yen`。片方にしかない行は `mismatch` とし、`row_reason` に `missing_in_ledger` か `missing_in_reader` を書く。

```json
{"type": "import", "source": "S004",
 "rows": {"S004:000042": {"row_status": "match", "fields": {"title_raw": "match", "amount_yen": "match"}}},
 "counts": {"rows": 422, "match": 422, "mismatch": 0,
            "fields": {"title_raw": {"match": 422, "mismatch": 0}}}}
```

（形の細部は D-030 で決めた）

## 8. 引用の照合（`quotes.py`）

- 資料のページ別テキスト（CSVは `records.cells_json` の `column` のキーの値）と引用を、どちらもNFKCで正規化し、空白と改行を除いて比べる。部分一致でよい。
- 引用は文末（。．！？で終わる、または資料の段落・セルの終わり）まで含むこと。途中で切れていれば `E_QUOTE_TRUNCATED`。資料の中で直後が数字の `.`（NFKCの後。全角の `．` を含む）は小数点なので文末にしない。
- 引用は、資料の数値の途中で始まっても、途中で終わってもいけない（`E_QUOTE_TRUNCATED`。D-033・D-036）。資料の数値は、空白を除いた後の、7.2 の数値の並び（前後が数字・`,`・`.` でない、数字と `,`・`.` の連なり。先頭の `.` を含む）に、直前の `▲`・`△` と符号（`-`・`−`・`+`。前が数字・英字でないもの）を含めたもので、段落・セルをまたいでつながない。`750,000` のセルの `000`、`1.5件。` の `1.`、`-12件。`・`▲12件。` の `12件。`、`.5` の `5` を通さない。範囲の `10-15` の `15` からの引用は通す。また、引用だけを読んだときの数値（同じ規則で、引用の始めの符号を含む）が、資料のその位置で読む数値と同じでなければ `E_QUOTE_TRUNCATED`（D-039）。資料で符号でない `-`・`+`（前が数字・英字のもの）から始めた引用（`10-15件。` の `-15件。`、`A-12型。` の `-12型。`）は、引用だけでは負・正の数値に読めるので通さない。
- 主張の種類が「原資料の記載」で、主張の `fiscal_year` と資料の年度（データの資料は論理資料のキーの年度）がどちらも分かり、食い違えば `E_QUOTE_YEAR`。追加資料の年度は機械では決めず、`review-lead` で確かめる。
- `sources.kind='index'` の資料、公表一覧のPDF（`kind='dataset'` で形式がPDFの資料。D-021）、`references/` の知識ファイルは根拠にできない（`E_BAD_EVIDENCE_SOURCE`）。公表一覧のPDFを指した場合は、`next_action` で同じ年度・方式のCSVの `row_ref` を使うよう案内する。知識ファイルの引用を照合するのは、係数の提案の手がかり（6.4）のときだけである。
- ページ別テキストが UTF-8 として読めない（壊れている）ときは、想定外のエラーにせず `E_QUOTE_NOT_FOUND` とする（`check` では `V4_QUOTE_NOT_FOUND` と `V4_PAGE_TEXT_MISMATCH`。D-066）。

## 9. 検証（`check.py`）

各検査の不合格はエラーコードで返す。設計書5.5のV1〜V8の内容と対応させる。

| 検査 | エラーコード | 条件 |
| --- | --- | --- |
| V1 | `V1_INPUT_EMPTY` | 候補が依存する資料の有効行が0。依存する資料は、取得できた行からではなく、集計の条件・年度・分野の規則から決める（この表の後の「依存する資料」。D-041⑤） |
| V1 | `V1_QUALITY_NOT_RUN` | 必須の品質検査が未実行 |
| V1 | `V1_RUN_FAILED` | 実行が `failed`、または候補が依存する検出が欠けた `partial` |
| V2 | `V2_REF_MISSING` | 存在しない資料・計算・レコード・主張への参照 |
| V2 | `V2_HASH_MISMATCH` | 原本のファイルがないか、ハッシュが台帳と違う。対象は検証が依存する資料のすべて（候補の行と率の分母の行のCSV、T6a・T6bの照合の計算が読んだCSVとPDF、根拠・文書値の資料。D-041⑥） |
| V2 | `V2_STALE_OR_WITHDRAWN` | `stale`・`withdrawn` の主張、取り下げた計算・対応への参照。検証が依存する資料（この表の後）に、`fetch --recheck` で取った中身の違う新しい版がある（D-056①） |
| V2 | `V2_BAD_EVIDENCE_SOURCE` | 知識ファイル・索引ページを根拠にした主張。「原資料の記載」に取得した公的資料の位置がない |
| V2 | `E_RULE_VERSION` | 規則の版の食い違い（3.5。終了コード3） |
| V3 | `V3A_RECALC_MISMATCH` | 基礎集計を同じ台帳で計算し直すと一致しない |
| V3 | `V3B_REAGG_MISMATCH` | メモに出る基礎集計（派生計算が参照するものを含む）を、別経路の読み込み処理で原本から数え直すと一致しない。数え直すのは `row_refs` が指す原本の行の値（件数、合計と空欄の行の数）で、条件（`filter`）を原本で当て直して行の選び方を独立に確かめるものではない（設計書5.5の限界。D-041⑦） |
| V3 | `V3B_HEADER_UNRESOLVED` | 見出しの文字列で列を一意に特定できない、または見出しが空の列を使っている |
| V3 | `V3D_DOC_VALUE_MISMATCH` | メモに出る文書値の値が、依頼に残した引用の中の数値の並びのどれとも等しくない（`record` の照合と同じ規則で、`check` でも照らし直す。引用が資料にあるかは V4。D-060） |
| V3 | `V3C_DERIVE_MISMATCH` | 派生計算を演算どおりに計算し直すと一致しない |
| V3 | `V3_UNCITED_NUMBER` | メモに出る数値が基礎集計・文書値・派生計算のどれでもない（`{calc:ID}` の参照先が探索SQLの結果や照合の計算など）。メモが `{calc:ID}` を展開する欄のすべて（この表の後の「メモに出る計算」）を見る（D-041⑧） |
| V3 | `V3_NO_MEANING` | 計算に意味・単位がない |
| V4 | `V4_QUOTE_NOT_FOUND`・`V4_QUOTE_TRUNCATED`・`V4_QUOTE_YEAR` | 8章 |
| V4 | `V4_PAGE_TEXT_MISMATCH` | 引用したページ別テキスト（主張・判定・対応・文書値と、係数の提案の手がかりのうち取得した資料の根拠。D-064）が、原本（V2 で SHA-256 を確かめた `data/raw`）から抽出し直したテキストと違う（ページの数か中身。UTF-8 として読めないテキストを含む。D-066）。どちらも、いまの担当官の氏名の一覧で伏せてから比べる。原本がない・違う場合は `V2_HASH_MISMATCH`。取得の後にページ別テキストだけを書き換えた引用を根拠にしない（D-061） |
| V5 | `V5_CONDITION_MISSING` | `leads.comparison_json` の項目（母集団、1行の意味、期間、単位、主体の対応、算式）に空欄。既定値は検出のときに `dataset.yaml` から入る |
| V5 | `V5_UNRESOLVED_COMPARISON` | `unresolved` なのに比較の主張がある。「比較の主張」は、年度間の比較（増減や変化の向き・大きさ）を述べる主張である。CLI が機械で見つけるのはその一部で、参照する計算（本文の `{calc:ID}` と派生計算の入れ子を含む）の基礎集計が2つ以上の年度にまたがる主張と、比較成立の結論の案だけ。結論の種類にかかわらず（保留でも）当てる。各年度の値を別々に書いた主張は当たらない。1つの年度の値だけを参照して文で比べる主張（「前年度より増え、…になった」）は機械では見分けず、`review-lead` が点検し、メモの7節で記者が確かめる（機械の検査と意味の確認の境界。D-053⑦・D-056④） |
| V5 | `V5_ADJUSTED_NO_VALUES` | `adjusted` に補正の方法と補正前後の値がない |
| V5 | `V5_BAD_RATE` | 分母のない率、0や負の値からの増加率。メモに出る派生計算の `ratio`・`rate` の第2引数（分母・基準）の値が、値なし・0（`ratio`）・0以下（`rate`）。`record` は作らせないので、台帳を書き換えた場合と古い台帳に当たる（D-053⑨） |
| V5 | `V5_CHANGE_NOT_COMPARABLE` | 比較成立の結論の案なのに、比較の条件が `comparable`・`adjusted` でない（`not_applicable` など。`unresolved` は `V5_UNRESOLVED_COMPARISON`）。比較成立は「同じ条件でそろえた変化」（設計書2.1）。見かけの変化と保留には制約を置かない（D-063） |
| V5 | `V5_PARTIAL_YEAR` | 比較成立なのに年度途中と年度全体を比べている。比べる2年度の片方だけが `dataset.yaml` の年度途中（どちらかの方式で `partial_year`）なら、`adjusted` でも不合格（D-053⑧） |
| V6 | `V6_TYPE_MISSING` | 6つの型（T6は3つ）のどれかに判定がない |
| V6 | `V6_NO_EVIDENCE` | 根拠の位置がない「支持」「否定」（T6a・T6bは照合の計算IDが根拠になる） |
| V6 | `V6_SELF_EVIDENCE` | T1〜T5の「支持」の根拠が、説明する対象のレコードそのもの（随意契約の理由の欄など）だけ。根拠のすべてが候補の行（`candidate_rows`）の `row_ref` を位置に持つもの。6つの型の判定の欠けは、`V6_TYPE_MISSING` と `V8_REQUIRED_MISSING` の両方を返す（D-053⑩） |
| V6 | `V6_NO_SEARCH_SCOPE` | 「未確認」に探した範囲がない |
| V7 | `V7_RAW_NUMBER` | Claudeが書く欄に生の数値（算用数字・全角数字。NFKCにしてから見る）。次の形に当たる部分を、この順に取り除いてから見る：`{calc:ID}`、ID `S\d{3}:\d{6}`・`[SKLCM]\d{3,4}`・型 `T[1-6][abc]?`・検出 `S[1-3](?![\d:])`・品質検査 `Q\d{2}`、年度 `令和(\d+\|元)(・\d+)*年度`・`平成\d+年度`・`\d{4}(・\d{4})*年度`・`R\d+(\.\d+\.\d+)?`（`R6.4.1` の形の日付を含む）、日付 `\d+年\d+月(\d+日)?`・`\d+月\d+日`、条文 `(第\|(会計法令?\|予算決算及び会計令\|予決令\|地方自治法施行令)第?)\d+条(の\d+)*(第?\d+項)?(第?\d+号)?`（「第」のない `予算決算及び会計令99条の5` も対象外。法令名も「第」もない `3条件` は対象）、照合済みの引用の中 |
| V7 | `V7_PERSONAL_NAME` | 氏名の一覧（検査のたびに原本の3列目から作り、保存しない）にある氏名を、姓と名をつないだ全体（空白なし・半角空白・全角空白の3通り）で照らして見つかったもの、または取り込まない列の値（担当官の所在地と相手方の住所だけ。範囲と切り出し方は12.2。D-032）。姓や名の1語だけでは照らさない（1字の姓が件名に含まれるなど誤検出が多いため）。氏名の検査は、Claudeが書くすべての文字列に当てる。`V7_RAW_NUMBER` と `W7_STRONG_WORD` を当てない欄（比較の条件 `comparison` の6項目、計算・派生計算・文書値の `unit`、候補の `lead_key`、係数の `key`。数字を書いてよい欄）にも当てる（D-035）。`record` では、提案の中のすべての文字列の値（入れ子を含む。根拠の引用 `evidence[].quote`、集計の条件の値 `filter[].value`、係数の手がかりの引用 `basis[].quote`、結論での比較の条件の変更を含む）に当てる（D-036）。`check` の時点の V7 は、`record` と同じ欄の分け方で台帳の文字列を見る。CLI が書いた文字列（検出の候補の `lead_key`・`note`、比較の条件の既定値、T6a・T6b の判定、`run` の計算）には数値と強い語を当てない（氏名と取り込まない列の値は当てる）。原本の CSV が `data/raw` になく氏名の一覧を作れなければ `V7_PERSONAL_NAME`（飛ばして合格にしない）。原本の様式が期待と違うときも、担当官の列の位置を決められないので `V7_PERSONAL_NAME`（D-059）。強い語の警告は `check` の出力の `warnings`（D-053⑪） |
| V7 | `W7_STRONG_WORD`（警告） | 強い語（`不正`、`談合`、`違法`、`癒着`、`疑惑`、`異例`、`不透明`、`隠した`）。照合済みの引用の中は対象外。例外語（`不正確`、`不正アクセス`）は警告しない。比較の条件（`comparison`。候補の登録と、結論での変更）にも当てる（D-036） |
| V8 | `V8_REQUIRED_MISSING` | 設計書2.1の表の必須の中身がない |
| V8 | `V8_MATCH_UNCONFIRMED` | 比較成立なのに、比べる両年度の行が同種業務の対応で覆われていない。具体的には、`leads.calc_from` の基礎集計の `row_refs` をF、`leads.calc_to` の `row_refs` をTとして、その候補の `status='confirmed'` の対応の `left` の和集合がFを、`right` の和集合がTを含まない。対応がない、一部の行だけ、`pending` だけ、取り下げ済みだけ、の場合を含む。同種と確かめられなければ保留（`hold_reason: comparison`）にする（設計書5.3） |
| V8 | `V8_COEFFICIENT_PENDING` | 比較成立なのに、係数の提案が `proposed` か `accepted` のまま残っている（6.4） |
| V8 | `V8_CHANGE_NOT_NEGATED` | 比較成立なのに、T6a・T6b（アダプタが対象外とした場合を除く）が否定されていない。T2（受注先の変化の区分ではT5も）が否定されていない、かつ `adjusted` で補正前後の値がない |
| V8 | `V8_ARTIFACT_NO_FACTOR` | 見かけの変化なのに、T2・T5・T6a・T6bのどれも根拠付きで支持されていない |
| V8 | `V8_EXPLANATION_STATUS` | T1・T3・T4のどれかが支持なのに説明の状況が `none`。`full`・`part` なのに、支持の T1・T3・T4（`scope` が `full`・`part`）のどれも、この表の後の「説明を反映した値と残る差」を満たさない。`unknown` なのに目的が `reason_breakdown` の質問がない（D-041⑩・D-042③）。メモに残りの件数（基礎集計の残る差）を出す判定のどれかで、残りを選ぶ条件の列が空（NULL）の候補の行を `=`・`!=`・`in`・`not_null` で残りから外している（判定ごとに確かめ、別の説明の合格や結論の種類で免除しない。免除するのは、その判定の `reflected_conditions` に宣言した説明用の条件で空でない値によって外れる行だけ。この表の後の「説明を反映した値と残る差」。D-048・D-049） |
| V8 | `V8_NO_WITHDRAW_RESULT` | 比較成立なのに取り下げ条件の結果がない |
| V8 | `V8_HOLD_REASON` | 保留なのに `hold_reason` がない、または目的が `missing_documents`（不足資料の照会）の質問がない（照会先は質問の `to`、情報公開請求の候補は `foia`。D-041⑩） |
| V8 | `V8_T6C_QUESTION` | 比較成立なのに、目的が `verify_record`（元の記載の確認）の質問がない（質問の中身の十分さは記者が確かめる。D-041⑩） |

T6aとT6bの判定は機械で行い、CLIが `check` のたびに `explanations` に登録する（`context='cli'`、根拠は照合の計算ID）。候補の行（メモに出る基礎集計の `row_refs` から、率の分母だけに使う行を除いたもの）について：

- T6a：すべてが取り込みの照合で一致なら「否定」、1つでも不一致なら「支持」。
- T6b：すべてが原本照合で `matched` で、変化の区分ごとの照合項目（アダプタ仕様7.4）がすべて `match` なら「否定」。受注先を扱う主張があれば、照合項目に受注者名と法人番号を加える。「受注先を扱う主張」は、メモに出る記載の根拠（主張・判定・対応・係数の手がかり・文書値。D-065）の位置の列が受注先の列（`counterparty_raw`・`corp_number_raw`）か、メモに出る基礎集計の条件に受注先の列（`corp_number_state`）があるもの（機械で見分けられる範囲。文で受注先を述べる主張は `review-lead` と記者が確かめる）。メモの7節も同じ項目で照合できなかった行を先頭に出す（D-058）。照合項目から除くのは、分野の規則でその行に当たらない項目（その年度・方式の `take` に `pdf_fields` の列がない項目。競争入札の行の `reason_raw` など）だけとし、当たる項目の照合の結果がなければ「未確認」とする（D-041④）。1つでも `mismatch` があれば「支持」（見かけの変化の検討へ）。`mismatch` がなく `unverifiable`・`not_found`・`ambiguous` があれば「未確認」（保留の理由 `source_check`）。分野アダプタが原本照合を対象外とした場合は「対象外」。
- CLIの判定の行（`context='cli'`）は、判定・根拠・`searched` が前と同じなら `updated_at` を変えない（内容の同じ再検査で検証記録のハッシュを変えないため。検査の日時は `events` に残る。D-041⑬）。

**依存する資料**（V1。D-041⑤。D-042①②）：候補が依存する資料（CSV）は、メモに出る基礎集計ごとに、年度と方式から決める。年度は条件の `fiscal_year`（`=`・`in`）、なければ実行の `series_years` のすべて。方式は、`run` の検出の計算（`request_json` に `screen_id` がある）なら `dataset.yaml` の `screens.<名前>.requires`、`record` の計算なら条件の `method_family`（`=`・`in`）、どちらでも方式を限ると機械で確かめられなければ分野の方式のすべて（内閣府では競争入札と随意契約）。取得できなかった資料（`partial`）は `V1_RUN_FAILED`、品質検査の結果がなければ `V1_QUALITY_NOT_RUN`、採用の行が0なら `V1_INPUT_EMPTY`。取得できた行の資料から方式を推し量らない。条件の年度が整数でなく解釈できなければ、年度を限らないものとして `series_years` のすべてを求める（「依存する資料なし」にしない。`record` は文字列の年度を `E_SCHEMA` にする）。

**説明を反映した値と残る差**（`V8_EXPLANATION_STATUS`。D-042③）：
- 金額の候補（候補の `metric` が `<列>_sum`、または `calc_from` が合計）：残る差は、説明を反映した値（`recalc_calc_id`）と比較対象（`calc_from` か `calc_to`）の `diff` の派生計算だけとする（`diff(説明を反映した値, 比較対象)` か `diff(比較対象, 説明を反映した値)`。メモに比較対象と引き算の向きを出す）。説明を反映した値・比較対象・残る差の単位は同じ。掛け算などを残る差としては受け付けない。件数を残る差にしない。
- 説明を反映した値は、候補の比べる2年度・系列の計算と同じ集計であってはならない。同じ集計かは、値ではなく、基礎集計なら表・演算・列・対象行（`row_refs`）、派生計算なら演算と引数で判定する（別のIDで作り直した同じ集計を拒否する）。
- 金額以外の候補（方式・理由の変化など）：残る差は、候補の対象行から、根拠によって説明できる行を除いた残りの行を数えた基礎集計の件数とし（D-046）、P0では説明を反映した値と同じ計算にする（`recalc_calc_id` = `residual_calc_id`。別の計算を説明を反映した値にしない。D-043①）。その計算は、候補の比べる年度のどちらか1つの `fiscal_year` を条件に持ち、表と単位が候補の計算と同じで、対象行がその年度の候補の行の部分集合であること（無関係な業務・年度の行を数えない）。件数が減ることは求めない。
- 判定には、説明によって足した・変えた条件を `reflected_conditions` に書く。金額以外の候補では、これは残りの行（根拠で説明できない行）を選ぶ条件である（説明できる行を選ぶ条件ではない。D-046）。それぞれの条件は、残る件数の条件にあり、その年度の候補の計算の条件になく、候補を選び直すだけの列（`fiscal_year`・`record_id`・`group_key`・`method_family`）の条件でないこと（条件の書き換えだけを説明の反映としない。D-043②・D-044②）。`method_family` は集計の条件には使えるが、説明によって足した条件にはしない（S3 では随意契約であることが母集団の前提で、S2 でも比べる年度の側の方式を指定するのは観測対象の選び直しになるため）。
- さらに、その年度の候補の行（`calc_from` か `calc_to` の `row_refs`）に `reflected_conditions` だけを当てた行の集まりが、残る件数の計算の `row_refs` と一致すること（宣言していない条件で行を除いた件数を、説明を反映した後の残る件数として通さない。一般の条件式の同値は判定しない。D-044①）。宣言した条件で正当に0件になる場合、条件を当てても件数が変わらない場合、一部だけ残る場合は、どれも合格する（D-046）。条件を比べる前に、単純な同値の表現をそろえる：重複した条件を1つにし、`in` の値を並べて重複を除き、値が1つの `in` を `=` にする。一般の条件式の意味の同値までは判定しない。
- どちらでも、その判定（支持の T1・T3・T4）に根拠（`evidence`）があること。CLI が確かめるのは根拠の引用が資料にあることまでで、根拠が説明を裏付けるかは `review-lead` と記者が確かめる。
- 金額以外の候補の残りの件数について、CLI は形（同じ計算、その年度の候補の行の部分集合、足した条件だけを当てた行と一致）だけを確かめ、件数から説明の状況を決めない。0件でも「全部」とは判定せず、残りを選ぶ条件が根拠の説明する範囲と合うかは `review-lead` と記者が確かめる。条件の列が空（NULL）の行は、`is_null` 以外のどの条件（`=`・`!=`・`in`・`not_null`）にも当たらず残りから外れ、説明できる行として扱われてしまう（未確認の行を説明済みとして除くことになる。D-046）。
- 空の値で外れた行の検査（D-048）：メモの4節に残りの件数（基礎集計の残る差）を出す判定ごとに、CLI は次を確かめ、当たれば `V8_EXPLANATION_STATUS`（不合格の文に判定・列・行を書く）とする。検査元は、残りの件数が参照する年度（条件の `fiscal_year`。限っていなければ比べる2年度）の候補の行（`calc_from`・`calc_to` の `row_refs`）。そのうち残りに入らなかった行で、残りの件数の条件のうち `=`・`!=`・`in`・`not_null` の条件の列が空の行があれば不合格。ただし、その判定の `reflected_conditions` に宣言し、説明用として認める条件（候補を選び直すだけの列 `fiscal_year`・`record_id`・`group_key`・`method_family` でないもの）で、空でない値によって外れる行は対象にしない。宣言していない条件（集計にだけ足した条件）や、宣言しても説明用に認めない条件では免除しない（D-049）。`is_null` で空の行を残りに残す使い方は拒否しない（根拠との対応は記者が確かめる）。検査は判定ごとに独立して行い、別の説明の合格で免除しない。結論の種類（比較成立・見かけの変化・保留）にかかわらず、メモに残りの件数を出す判定なら行う。空の値の行を定量化できなければ、残りの件数を説明に使わず、`scope` を `unknown` にして `no_quant_reason` と、目的が `reason_breakdown` の質問で扱う（それだけで比較成立を保留にしなくてよい）。

**メモに出る計算**（V2・V3・11章で共有。D-041③⑧）：候補が持つ計算、`calc_from`・`calc_to`・系列、主張の `calc_ids`、補正の前後、判定の再計算と残る差、派生計算が参照する計算（入れ子）と、メモが `{calc:ID}` を展開する欄のすべての参照。展開する欄は、主張の本文、判定の `searched`・`no_quant_reason`、仮説・取り下げ条件・候補の注記・再開の条件、質問のすべての文字列、比較の条件の6項目、補正の方法、係数の提案の文字列、対応の理由で、`render` は同じ一覧を展開する。取り下げた計算への参照は `V2_STALE_OR_WITHDRAWN`。

**検証が依存する資料**（V2・11章・メモの資料の一覧で共有。D-041⑥・D-042①）：V1の依存する資料のCSV（該当する行が0の資料も含む。「その資料には該当する行がなかった」という判断もその資料に依存するため）、メモに出る基礎集計の行の資料（率の分母だけに使う行も含む。候補の行から除くことと、依存から除くことは別）、T6a・T6bの根拠の照合の計算が読んだCSVとPDF（`request_json` の `source`・`csv_source`・`pdf_source`）、主張・判定・対応の根拠と文書値の資料。

## 10. メモと実行の概要の書式

### 10.1 候補のメモ

`render.py` が台帳から組み立てる。Claudeの文章は、主張・仮説・判定・質問の本文として登録されたものだけを使う。

```markdown
# <区分の日本語名>：<比較の単位の表示名>

> 調査用・記者未確認。調査の端緒であり、不正の指摘ではない。
> 調査用の設定で実行：はい／いいえ　実行ID：… メモ：v2　検証記録のハッシュ：…

## 1 状態と範囲
## 2 検出した変化
## 3 照合済みの記載と計算
## 4 説明の検討
## 5 言えないこと
## 6 次の取材質問
## 7 記者の確認リスト
## 8 再現情報
```

各節の中身は設計書6章。次の規則で表示する。

- 計算の参照 `{calc:K0012}` は「7,793,940円（令和6年度の合計。計算K0012）」の形に展開する。
- 6つの型と状態は日本語の名前で表示する（T1 制度・定義の変更、T2 集計範囲・期間の違い、T3 業務・数量の変化、T4 価格・市場の条件、T5 主体の同一性、T6a 取り込みの誤り、T6b 公表形式間の食い違い、T6c 元の記載の誤り。支持・否定・未確認・対象外）。
- 「CSVとPDFの一致は元の記載の正しさを示さない」を5節に定型文で入れる。
- 母集団の絞り込み（6.3）があれば、1節に記者の指示と条件を出す。係数の提案（6.4）は4節と7節の先頭に出す。
- 4節の判定には、説明を反映した値、残る差（金額の候補は比較対象と引き算の向き、金額以外の候補は「説明できる行を除いた残りの行」。D-046）、説明によって足した・変えた条件（`reflected_conditions`）を出す（D-043②）。
- 3節の末尾に「資料の一覧」を置き、検証が依存する資料（9章）のすべてについて、資料ID、URL、取得日時、保存場所（`data/raw/<sha256>.<拡張子>`）、SHA-256を出す。引用と行の表示は資料IDでこの一覧を引く（D-041⑭）。
- 8節の版：Skill とルールのファイル（`CLAUDE.md`、`.claude/skills/` の Markdown）は、開始時の記録（3.5の `rule_files`。コミットとの一致を含む）と、`render` の時点の一覧・ハッシュを分けて出し、開始時からの追加・削除・変更を書く。開始時の記録のない実行は「未記録」とし、`render` の時点の値で補わない（D-041⑭・D-042④）。
- 7節の各行：ファイル名、手元の原本の場所（`data/raw/<sha256>.csv`）、「NO」の値、表計算ソフトで開いたときの行番号（見出し4行＋論理レコード番号。セル内の改行がある行より後はずれうるため「目安」と書く）、PDFのページ、確かめる項目と値。照合できなかった行と項目を先頭に置く。

### 10.2 失効と規則の版

メモの冒頭に検証記録のハッシュを書く。`inspect` が失効を表示する（4.3）。

### 10.3 実行の概要

```markdown
# 実行の概要：<問い>
> 調査用・記者未確認。実行ID：… 状態：completed／partial／failed
## 取得（予定と結果。取得失敗・非公表・対象外の区別）
## 処理の範囲（処理した範囲、処理していない範囲。母集団の絞り込みがあれば、記者の指示、条件、資料ごとの除いた行の数）
## 品質レポートの要約
## 検出（検出SQLごとの比べた組の数、候補の総数、比較できない理由の付いた候補の数・比較できない組の数）
## 候補（調べた候補の結論とメモへのリンク、調べずに残した候補、取り下げた仮説と理由・再開の条件）
```

「比較できない理由の付いた候補の数」は、検出SQLごとの、比較できない理由（単価契約・年度途中・重複など）の付いた候補の数で、同種業務の対応の確認待ち（対応表の `pending`）や記者の原本の確認の未了とは別の数として書く（D-053⑥）。品質レポートの要約は、資料ごとの合否の数と、不合格・未実行の項目だけを書き、報告の項目は品質レポートの全文に任せる。番号は `memos/` の `run_summary.v<n>.md` の最大の次とし、`events` に `render`（対象は実行ID）を残す。

候補が0件のときは「比べられたN組の中で該当なし」と書く。`partial`・`failed` のときは「候補なし」と書かない。`counts_json` が `not_run` の検出SQLは、比べた組の数の代わりに「未実行」と欠けた資料を書く。

## 11. 失効のハッシュ（`hashing.py`）

1. 候補について、`render` が読む行をすべて集める：`leads` の行（`memo_no`・`memo_hash`・`rounds` を除く全列。`state` は含める）、その候補の `claims`（`withdrawn` を除く）、`explanations`、メモに出る計算（9章）と T6a・T6b の根拠の照合の計算の `computations` と、その `row_refs` のレコードの `cells_sha256`、対応表の行、検証が依存する資料（9章。照合のPDFを含む）の `sha256`（`fetch --recheck` で取った新しい版があれば、その版の資料IDと `sha256` も。新しい版のない資料の値は変えない。D-056①）、`runs.rule_versions_json`。
2. それぞれを、キーを並べ替え、区切りの空白をなくしたJSON（`json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`）にし、表の名前とIDの順に並べて連結する。
3. 連結したUTF-8のバイト列のSHA-256を「検証記録のハッシュ」とする。
4. `render` は、主張と候補の状態を更新した後に計算する（4.7）。`inspect` は同じ手順で計算し直して比べる。

## 12. 公開

### 12.1 伏せた写し（`render --public`）

- 実行の `procurement_contracts.counterparty_raw` から受注者名の一覧を作り（セルを改行で分けた各行）、法人番号（13桁。`corp_numbers_json`）と合わせて、メモの中の出現を「受注者A」「受注者B」…（実行の中で同じ名前には同じ記号）と「法人番号（伏せ字）」に置き換える。記号は、実行の中で最初に現れた行の順（`record_id` の順）に A〜Z、AA… とし、`disclosure.allow` の名前も含めて割り当てる（公開を認めた名前を変えても、同じ実行の中でほかの受注者の記号は変わらない。D-053⑤・D-056⑤）。全角の数字なども、1文字ずつ NFKC にした文字列で探して元の位置を替える。
- `examples/disclosure.allow`（1行に1つの名前。記者が書く）にある名前は伏せない（記号は割り当てるが置き換えない）。値は NFKC で比べ、受注者名と法人番号の両方に効く。
- 写しは `public/<メモと同じ名前>`（`public/L0405.v2.md`、`public/run_summary.v1.md`）に書く。
- CSVの受注者名にASCIIの `?` がある場合（PDFの文字が置き換わったもの。アダプタ仕様2章）、`?` を空白以外の任意の1文字として照らす。元の表記と、NFKCにした表記の両方から、`?` 以外の文字を `re.escape` し `?` を `\S` にした正規表現を作り、メモの原文に当てる。`?` の位置が違う文字で書かれた出現（例：架空の `（公財）テスト?研究所` に対して `（公財）テスト−研究所`）にも、同じ名前として同じ記号を付ける。`?` 以外の文字が3文字未満の名前は、任意の1文字として照らさず、元の表記どおりにだけ照らす（短い名前で照らしすぎないため）。D-022
- 写しの1行目に「受注者名は伏せている」と書く。

### 12.2 `check --publish`

```text
kizashi check --publish examples/publish.manifest --run <run_id> [--run <run_id> ...]
```

`examples/publish.manifest`（1行に1つのパス。リポジトリの直下から、`examples/` の中だけ。空行と `#` で始まる行は読まない。一覧のファイル自身は書かなくても一覧に入れる。`examples/.gitkeep` と `disclosure.allow` も、コミットするなら一覧に書く。HEAD のないリポジトリは最新のコミットの一覧を空とする。D-053③）を読み、次を検査する。どれかに当たれば終了コード1（不合格のコードは3.7。D-053①）。

| 検査 | P0／P1 |
| --- | --- |
| 一覧のファイルと、`git ls-files examples/`（ステージの一覧）と、`git ls-tree -r --name-only HEAD examples/`（最新のコミット）が一致する | P0 |
| 補助資料4点の中身のSHA-256が、検査の対象（下）のどのファイルの中身とも一致しない。ファイル名が、対象のパスと履歴のパス（`git log --all --name-only --format=`）にない | P0 |
| 検査の対象のテキストに、氏名の一覧（原本から作る）と取り込まない列の値がない | P0 |
| 検査の対象のテキストに、`disclosure.allow` にない受注者名・法人番号がない | P0 |
| APIキーなどの秘密の書式、個人の絶対パス、表計算で数式として評価される文字列（`=`・`+`・`-`・`@` で始まるセル） | P1 |

**取り込まない列の値**（V7 と、この検査で照らすもの。D-032）は、原本の CSV の担当官の所在地（0始まりの index 3）と相手方の住所（index 6）の値だけとし、空欄と `-`・`－`・`―` は除く。index 3 は、複数の担当官の所在地が改行で並ぶセルがあるため、セルを改行で分けた各行（前後の空白を除く）を照らす。index 6 は、セル全体（前後の空白を除く）と、セル内の改行と連続する空白を空白1つに替えた文字列の2つを照らし、改行では分けない（分けると、ビル名だけが切り出されて件名と重なる）。ほかの取り込まない列（公益法人の区分、所管の区分、応札・応募者数、再就職の役員の数、随意契約の内部用の列）は照らさない。これらには個人名や住所がなく、短い数字や記号、取り込む列やメモにそのまま出る公的な部局名・一般語が入っているためで、正規化で台帳から除くこと（Q04）で守る。値の一覧は氏名の一覧と同じく、検査のたびに原本から作り、保存しない。

受注者名と取り込まない列の値にASCIIの `?` がある場合は、12.1と同じ規則（`?` を空白以外の任意の1文字として照らす。`?` 以外の文字が3文字未満なら元の表記どおりだけ）で検査する。氏名（担当官の列）には `?` がない（アダプタ仕様2章。D-022）。

**検査の対象**：Gitで公開されるのはファイル名の一覧ではなく、ステージとコミットの中身である。そこで次の3つの中身をすべて対象にする。

1. 作業フォルダーの、一覧（manifest）にあるファイル
2. ステージにある中身（`git ls-files -s` が示すblobを `git cat-file -p` で読む）
3. すべての参照からたどれる履歴の中身（`git rev-list --objects --all` のblobを `git cat-file --batch` で読む。別名でコミットしてから消したファイルも、ここで見つかる）

- 中身のSHA-256は3つの対象のすべてのblobで照らす。氏名・受注者名の検査は、UTF-8として読めて5MB以下のblobだけを対象にし、読めないblobはハッシュの照合だけにする。
- テキストは、元の文字列と NFKC にした文字列の両方で照らす。氏名は V7 と同じ姓名をつないだ3通り。法人番号は実行の13桁の値。`disclosure.allow` の値は NFKC で比べ、受注者名と法人番号の両方に効く。不合格には場所（作業フォルダー・ステージ・履歴、パス、blob、行、履歴ならそれを含むコミット）だけを書き、語を書かない（D-053④）。
- 当たったら、パス、blob、そのblobを含むコミット（履歴の場合）を返す。履歴にあった場合、作業フォルダーを直しても公開物からは消えない。履歴の書き換えは自動では行わず、新しいリポジトリを作り直すよう案内する。
- 限界：まだ取り込んでいない遠隔の履歴、`--all` に入らない参照（reflogだけに残るもの）は検査しない。公開するのはこの検査を通した後のコミットだけにする（`README.md` に書く）。

補助資料4点のファイル名とハッシュは `publish_denylist.txt`（リポジトリの外の、利用者の手元に置く。パスは環境変数 `KIZASHI_DENYLIST`）から読む。リポジトリに補助資料の名前を書かないためである。環境変数がない、またはファイルを読めないときは `E_DENYLIST_MISSING`（終了コード3）とし、合格にしない。書式は1行に `<SHA-256> <ファイル名>`（`sha256sum` の出力の形。空行と `#` で始まる行は読まない）で、4点のそれぞれを1行ずつ書く。形の誤った行がある、行が4つでない、ハッシュか名前（パスの最後の名前を NFC にし、大文字と小文字を区別しない）が重なる一覧は、4点がそろったと確かめられないので `E_DENYLIST_MISSING` とする（不足や重複で見かけ上そろった一覧を合格にしない。不合格の文に補助資料の名前を書かない。D-053②・D-056③）。

氏名の一覧と受注者名の一覧は、`check --publish --run <run_id>`（複数可）で指定した実行の原本（`data/raw`）と台帳から作る。`--run` がなければ `E_BAD_ID`。

## 13. P1の概要

| P1 | 内容 | 着手の条件 |
| --- | --- | --- |
| 承認した係数の適用 | 記者が承認した係数（6.4の `accepted`）を計算に使う。承認は、調査のセッションのClaudeが書けない経路（例：記者が別の端末で `data/runs/<run_id>/approvals.json` を書く。Claudeには `data/` の書き込みを許していない）から受け取り、メモに「記者が承認した係数（資料の根拠なし）」と出す | P0がすべて動いた |
| 別セッションでの読み直し | `scripts/review-memo <run_id> <lead_id>`。設計書9.3のコマンドで起動し、メモとページ別テキストを読ませる。指摘は記者が読み、必要なら主張を直す | P0がすべて動いた |
| 法人番号の補完 | PDFの13桁を、設計書7.4の3条件（検査用数字、有効桁、件名と金額）で採用し、`corp_number_state='valid_from_pdf'` を加える | 同上 |
| PRTRの骨組み | `datasets/prtr/`。設計書8.4。着手するときにアダプタ仕様を書く | 同上 |
| サンドボックス | 設計書9.5の設定を試す | 同上 |
| 新しい版の自動検出、接続先のアドレスの検査、ZIPの安全な展開、公開の検査の残り | 設計書の各節 | 同上 |
