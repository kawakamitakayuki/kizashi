# Claude Code 実装プロンプト集

KIZASHIを、Claude Codeで3日（11/5〜11/7）で実装するためのプロンプトである。11/8は提出の作業だけにする。

---

## 0. 使い方

1. リポジトリを作り、`docs/` に次を置く：`harness_design_m_v06.md`、本フォルダーの資料一式（`README.md`、`implementation_spec.md`、`adapter_cao_procurement.md`、`claude_code_config.md`、`test_plan.md`、`day1_checklist.md`、`decisions.md`、本書）。`references/` に知識ファイル3点（v13.0）を置く。補助資料4点はリポジトリに置かない。
2. 開発は通常のClaude Codeのセッションで行う。リポジトリの直下で `claude --permission-mode acceptEdits`（または `default`）と起動する。`scripts/investigate` は調査のためのもので、開発には使わない。
3. 1つのプロンプトを1つのセッションで進め、終わったら `/clear` してから次へ進む。長くなったら途中で区切ってよい。区切るときは「4. 補助のプロンプト」の「続きから再開」を使う。
4. 各プロンプトは、先頭に「共通ルール」を含めて貼る。共通ルールを毎回貼る代わりに、「`docs/implementation_prompts.md` の共通ルールに従う」と1行書いてもよい。
5. 終わるたびに、Claudeの報告（変更したファイル、テストの結果、仕様との違い、決めてほしいこと）を読み、決めたことを `docs/decisions.md` に書かせる。

| 日 | プロンプト |
| --- | --- |
| 11/5 | P0-0、初日の確認（利用者）、P0-1、P0-2 |
| 11/6 | P0-3、P0-4、P0-5、P0-6、P0-7（1件を通す） |
| 11/7 | P0-8、P0-9、デモの準備 |
| 余裕があれば | P1-a〜P1-d |

遅れた場合は、設計書7.5の「削る順」に従う。削らないもの（計算の検査、引用の照合、結論の要件、「記者未確認」の表示、氏名の除去、公開物の伏せ字、失効の判定）は残す。削る順とプロンプトの対応は次のとおり（テストの扱いは `test_plan.md` 2.1）。

| 削る順 | 影響するプロンプト | すること |
| --- | --- | --- |
| ① 見逃しの点検と同種業務の標本 | P0-9 | 評価に「未実施」と書く |
| ② 実行の概要を簡易版に | P0-8 の2 | 件数と候補の一覧だけを出す |
| ③ 随意契約の検出を外す | P0-5 の1・2 | S2・S3 を作らず、S1 とデータの問題に絞る。`populations/` も S1 だけ |
| ④ `run --resume` を外す | P0-5 の3 | 中断したら新しい実行でやり直す。RS-1・RS-2 を外す |
| ⑤ 母集団の絞り込みを外す（設計書にない追加の段。D-013） | P0-2 の6 | `scope` を受け付けず `E_SCHEMA` にする。記者に絞り込みを指示されたら、P0ではできないと伝える。SC-1 を外す |

---

## 1. 共通ルール

```text
# 共通ルール（KIZASHIの開発）

あなたはKIZASHI（調査報道の支援ツール）の実装を担当する。次の資料が正であり、作業の前に該当の節を読むこと。
- docs/harness_design_m_v06.md（設計書。設計の意図はこれが優先）
- docs/implementation_spec.md（実装仕様。名前・書式・手順はこれが優先）
- docs/adapter_cao_procurement.md（内閣府のデータの仕様）
- docs/claude_code_config.md（設定とSkillの原案）
- docs/test_plan.md（テストのIDと優先度）
- docs/decisions.md（決定記録）

守ること：
1. 指示された範囲だけを作る。P1と「将来」の機能、資料にない機能を先回りして作らない。
2. テストを先に書き、test_plan.md のIDをテスト名かdocstringに入れる。期待値は合成データから手で計算した値にし、実装の出力を期待値にしない。
3. 既定のテストは通信なしで動くようにする。通信を使うテストには @pytest.mark.network を付ける。
4. data/raw の原本と references/ の知識ファイルを書き換えない。実データ（内閣府のCSVやPDFの抜粋）を tests/ や fixtures/ に入れない（docs/decisions.md の D-007 が決まるまで）。テストには合成データを使い、受注者名と法人番号は明らかな架空の値にする。
5. 依存は implementation_spec.md の1章のものだけにする。ほかが要るときは、入れる前に理由を書いて確認を求める。
6. 仕様と食い違う点、仕様で決まっていない点、実データが仕様と違う点に出会ったら、推測で進めない。作業を止めて報告し、docs/decisions.md の「未決」に追記する。
7. 金額の合計は Decimal で行い、SQLite の SUM をメモの値に使わない。氏名を保存しない。
8. 終える前に、ruff check、ruff format --check、pytest を通す。
9. 最後に、次の形で報告する：(1) 作ったもの・変えたファイル、(2) テストの結果（件数と失敗）、(3) 仕様との違いとその理由、(4) 決めてほしいこと、(5) 次の作業。
10. 区切りごとに git commit してよい（メッセージは日本語で、何を作ったかを1行）。push はしない。
11. 正規化・照合・検証の結果を変える変更をして、テストの期待値を変えたときは、versions.py の rules_version（分野の規則なら dataset.yaml の parser_version）を1つ上げる（implementation_spec.md 3.5）。
12. ID の書式（implementation_spec.md 3.3。候補は L＋4桁など）と、実行ID以外のIDを読むときの --run を守る。
```

---

## 2. P0のプロンプト

### P0-0 リポジトリの骨組みと設定（11/5朝。約1時間）

```text
（共通ルール）

目的：KIZASHIのリポジトリの骨組みと、Claude Codeの設定を作る。初日の権限の試験（docs/day1_checklist.md の1）ができる状態にする。

読む資料：設計書3.3・9.1・9.2・9.5、implementation_spec.md の1・2・3章、claude_code_config.md 全体。

作るもの：
1. pyproject.toml（Python 3.12以上、パッケージ名 kizashi、src レイアウト、[build-system] は hatchling、[project.scripts] kizashi = "kizashi.cli:main"、依存は pyyaml と httpx、開発用に pytest と ruff。PDFのライブラリはまだ入れない）と uv.lock（uv lock で作る）。[build-system] がないと kizashi が .venv/bin に入らない。
2. 設計書3.3のフォルダー構成（src/kizashi/、tests/、datasets/cao-procurement/{screens,populations,fixtures}/、eval/、examples/、scripts/）。data/ と inbox/ は作らず .gitignore に入れる。
3. claude_code_config.md の 1〜5 のとおりに、.claude/settings.json、investigate.settings.json、scripts/investigate（実行権限付き）、.gitignore、CLAUDE.md を作る。
4. claude_code_config.md の 6〜9 のとおりに、.claude/skills/investigate/{SKILL.md, record-examples.md} と .claude/skills/review-lead/{SKILL.md, sources.md} を作る。sources.md の題名は references/ の原本の見出しを Grep して写す（推測で書かない）。
5. 仮の CLI：src/kizashi/cli.py に、7つの操作（run、fetch、inspect、query、record、check、render）の引数だけを受け付け、{"ok": true, "data": {"stub": "<操作名>"}} を返す仮の実装を作る。errors.py と、起動時の検査（implementation_spec.md 3.1 の作業フォルダーと SQLite の版）だけは本実装にする。inspect は `inspect <run_id>` と `inspect <ID> --run <run_id>` の両方の形を受け付ける。
6. テスト：CLI-1、CLI-3。

完了条件：uv sync が通り、.venv/bin/kizashi inspect 20261105-000000-test と .venv/bin/kizashi inspect L0001 --run 20261105-000000-test が {"ok": true, ...} を返す。リポジトリの外で実行すると E_CWD になる。pytest が通る。

止まる条件：Skill や設定の書き方が導入されている Claude Code の版で通らない（起動時に警告が出るなど）場合は、直さずに報告する。

終わったら、利用者に「scripts/investigate で起動して docs/day1_checklist.md の1を試してください」と伝える。
```

### 初日の確認（利用者が行う）

`docs/day1_checklist.md` の1（権限の試験）を、利用者が `scripts/investigate` で起動して行う。結果を開発のセッションで伝え、`docs/decisions.md` の D-001 に書かせる。

```text
docs/day1_checklist.md の1（権限の試験）の結果を伝える。docs/decisions.md の D-001 に、Claude Code の版、18項目の結果、起動のモードの決定を書いてほしい。結果：
（ここに 1〜18 の結果を貼る）
```

### P0-1 PDFの試験とデータの取り直し（11/5午前〜昼。約2時間）

```text
（共通ルール）

目的：docs/day1_checklist.md の2（PDFの抽出と照合の試験）と4（データの取り直しと差の確認）を行い、結果を決定記録に書く。本実装はまだしない。

読む資料：adapter_cao_procurement.md の1・2・3・7章、day1_checklist.md の2・4、設計書7.2・7.3。

作業：
1. scratch/ フォルダー（.gitignore に足す）に試験用のスクリプトを書く。src/ には入れない。
2. 公表ページ（adapter 1章）から、令和5〜8年度の競争入札と随意契約のCSVとPDFのリンクを取り出し、data/raw/ に保存する（SHA-256 の名前で。上書きしない）。
3. CSVについて、adapter 2・3章の事実（文字コード、行数、列の数、見出しの文字列、空の見出しの列、指数表記の件数、NO以外が同じ行の組、担当官が2人以上のセルの件数）を数え直し、違いを表にする。
4. pdfplumber を一時的に入れ（uv add --dev pdfplumber。本採用は結果を見て決める）、令和6・7年度の競争入札と随意契約のPDF（4本）でページ別テキストを取り出す。各PDFから20件を抜き出し、adapter 7.2 の行の対応と、7.3 の項目の照合を、方法(a)（座標で列を切り出す）と方法(b)（範囲の行を順に見て、項目の全文を取り除けるか。割合は使わない）で試す。1文字変えた変異と金額の変異（先頭に1桁足す、末尾の1桁を変える）が1件も match にならないこと、変えていない値が mismatch にならないことも確かめる（day1_checklist.md の2の基準）。崩れる場合は pypdfium2 も試す。
5. 試験の数字（行の対応の成功率、項目ごとの一致率、変異の検出率）を表にする。

書くもの：docs/decisions.md に D-002（ライブラリと版、照合の方法）、D-003（随意契約で照合できるか、方式・理由の候補の扱い）、D-005（データの事実との差）を追記する。差があれば adapter_cao_procurement.md の該当箇所に「D-005 で確認」と注記を足す（本文の事実は利用者の確認の後に直す）。

完了条件：上の3項目が決定記録にあり、採るライブラリが1つに決まっている（決められなければ理由を書いて止まる）。

止まる条件：合格の基準（day1_checklist.md の2）を満たす方法がない場合は、そのことを報告して止まる。
```

### P0-2 取得・正規化・品質検査（11/5午後。約3〜4時間）

```text
（共通ルール）

目的：kizashi run の工程 plan・fetch・normalize・quality を本実装する（令和6・7年度の競争入札と随意契約）。

読む資料：implementation_spec.md の2・3・4.1・4.2・5章・6.3、adapter_cao_procurement.md の1〜5章（4.8a を含む）・8章・10章、test_plan.md の FT・NR・QC、docs/decisions.md の D-002・D-005。

作るもの：
1. schema.sql（implementation_spec 5章の8つの表すべてと、adapter 8章の procurement_contracts・procurement_matches）、ledger.py、paths.py、versions.py、limits.py。外部キーで参照し合うため、表は最初からすべて作る（中身を使うのは後の工程）。
2. fetcher.py（許可リスト、リダイレクトの1段ずつの検査、サイズの上限、一時ファイル、SHA-256 での保存、sources への登録。取得失敗も記録）。
3. datasets/cao-procurement/dataset.yaml（adapter 10章。令和6・7年度の4ファイルを必須、令和5・8年度は参考）と adapters/。
4. readers.py、normalize.py、adapters/cao_procurement.py（adapter 4章の規則。担当官のセルから氏名を除く規則を含む）。records.cells_json は dataset.yaml の take のキー（amount_raw など）をキーにする（implementation_spec.md 5章の DDL の後の説明）。
5. quality.py（adapter 5章の Q01〜Q16。Q13 のファイル単位の突合は、別経路の読み込み処理ができるまで正規化の経路の件数と合計だけを記録し、P0-3で別経路と比べる形に仕上げる）。
6. run の新しい実行（plan〜quality）。工程ごとのトランザクションと runs.stages_json の記録。run_config の検査（compare_years は連続した2年度、series_years は compare_years を含む、scope は implementation_spec.md 6.3 の書式）。scope があれば normalize の後に適用し、条件に合わない行を excluded（out_of_scope）にする。品質検査と照合は全行で行う。
7. テスト用データ：test_plan.md の1章のうち、kyoso_2024_mini.csv・kyoso_2025_mini.csv・zuikei_2024_mini.csv・zuikei_2025_mini.csv・kyoso_bad_header.csv・web/kohyo.html（合成）。取得のテストは httpx.MockTransport でこのHTMLとCSVを返す。
8. テスト：FT-1〜FT-3、NR-1〜NR-9、QC-1〜QC-2、CLI-2、CF-1、SC-1（取り込みでの除外まで）、IN-1 の一部（パスの検査）。

完了条件：合成データで run が completed になり、品質レポートが出る。見出しの違うファイルで failed になる。氏名が台帳のどこにもない（テストで全文検索する）。実データ（data/raw の令和6・7年度）で run を手で1回動かし、品質レポートの件数を adapter 2章と比べて報告する（実データはテストに入れない）。

止まる条件：実データで担当官のセルを分けられない行がある、または所属の一覧に氏名らしい語が混じる場合は、一覧を報告して止まる（利用者が day1_checklist.md の5で確かめる）。所属の一覧は台帳やファイルに保存せず、報告の中でだけ示す。
```

### P0-3 別経路の読み込み処理と照合（11/6朝。約3時間）

```text
（共通ルール）

目的：別経路の読み込み処理（T6a と V3(b) で共有）、取り込みの照合（reconcile_import）、原本照合（reconcile_source）を実装する。

読む資料：設計書5.5、implementation_spec.md の4.1（工程）・7.4、adapter_cao_procurement.md の5章（Q13）・6章・7章（7.4 の変化の区分ごとの照合項目を含む）、docs/decisions.md の D-002・D-003・D-021、test_plan.md の IR・PD・QC-3。

作るもの：
1. independent_reader.py（見出しの行を「NO」で探し、縦に連結した見出しの文字列で列を特定する。正規化の経路の関数を呼ばない）。
2. reconcile.py の reconcile_import（論理レコード番号ごとの比較を computations の kind='reconcile' に記録）。
3. pdftext.py と reconcile.py の reconcile_source（D-002 の方法(a)。adapter 7.1〜7.3：罫線の座標でセルを切り出し、NOの列と照合に使う列以外の文字は捨てる。NOのセルで行を対応させ、行の対応と項目の照合を分け、結果を match・mismatch・unverifiable で記録）。項目の照合は adapter 7.3 のとおりにする：金額・法人番号は数値の並びの等しさ（部分一致にしない）、契約日は前後の境界付きの一致、件名・相手方・方式・理由と数値でない金額はセルとの完全一致。割合で一致にしない。迷ったら unverifiable。公表一覧のPDFのページ別テキストは作らない（D-021）。pdfplumber は D-002 で実行時の依存に入れ、版を固定済み。
4. quality.py の Q13 を、別経路と正規化の経路の件数・合計の突合に仕上げる。
5. run の工程 reconcile_import と reconcile_source。
6. テスト：IR-1、IR-2、PD-1、PD-2、QC-3（PDFのテストは pdftext の中で pdfplumber を呼ぶ関数を monkeypatch し、tests/fixtures/pdf_cells/*.json の合成の罫線と文字の座標を返す。PDFのライブラリをテストで動かさない）。

完了条件：合成データで照合の記録が7.4の形で残る。わざと列をずらした正規化の結果を T6a が不一致として出す。実データの令和6・7年度で run を手で動かし、照合の件数（matched・not_found・ambiguous、項目ごとの一致・不一致・照合不能）を報告する。

止まる条件：実データで照合不能が多く（例：随意契約の理由の半分以上）、D-003 の判断と食い違う場合は報告して止まる。
```

### P0-4 台帳の残り、提案、記録、表示、探索、追加取得（11/6午前。約3時間）

```text
（共通ルール）

目的：inbox の提案を登録する record、inspect、query、fetch（追加資料・--index・--definition）を実装する。

読む資料：implementation_spec.md の3・4.2〜4.5・6・7・8章、test_plan.md の IN・CA・QU・QT・FT-4、claude_code_config.md の7（提案の例）。

作るもの：
1. inbox.py（場所・拡張子・大きさ・シンボリックリンクの検査、成功したときだけ accepted/ に移す）。
2. record.py（implementation_spec 6.2 の種類すべて。coefficient は 6.4：知識ファイルの手がかりの引用を references/ で照合し、計算には使わない。管理項目の自己申告と存在しない参照を拒否。V7 の数値と氏名の検査は check.py の関数を使う形にし、この時点では関数の骨組みと単体テストだけでよい）。
3. calc.py（基礎集計：SQLで行を選び Decimal で合計。条件に record_status = 'adopted' を必ず加える。派生計算：diff・ratio・rate・sum・product の5つの演算だけで、引数は計算IDだけ。数値の定数と同じ計算IDの重複は拒否。文書値：引用の中の数値の並びと値が等しいかで照合し、部分一致にしない）。
4. quotes.py（implementation_spec 8章）。
5. query.py（mode=ro、query_only、authorizer、1つの文だけ、50行、10秒、citable: false）。
6. inspect_cmd.py（implementation_spec 4.3。inspect <run_id> と inspect <ID> --run <run_id>。候補IDでは同種業務の対応（matches）と係数の提案（coefficients）も表示する（D-023）。失効の表示は P0-8 で仕上げるため、ここでは memo_hash の有無だけ）。
7. fetch の追加資料・--index・--definition（ページ別テキストの保存、探索上限の計上）。
8. テスト：IN-1、IN-2、CA-1〜CA-3、QU-1、QT-1、FT-4、KN-2、CP-1（登録まで）。

完了条件：claude_code_config.md の7の例（run_config を除き、値を合成データに合わせて直したもの）が、すべて record で登録できる（KN-2。lead の例で探索の候補ができ、explanation に T6a・T6b を書くと拒否される）。不備のある例はエラーを返し、ファイルが inbox/ に残る。
```

### P0-5 検出と再開（11/6昼。約2〜3時間）

```text
（共通ルール）

目的：run の detect 工程（検出SQL、候補の登録、自動の基礎集計、同じ区分の件数、年度の系列、比べた組の数）と、run --resume を実装する。

読む資料：implementation_spec.md の4.1（再開）・4.8、adapter_cao_procurement.md の9章、設計書5.2、test_plan.md の DT・RS。

作るもの：
1. datasets/cao-procurement/screens/s1_amount_change.sql、s2_method_change.sql、s3_reason_change.sql と、同じ名前の populations/*.sql（adapter 9章。どれも :year_from・:year_to を受け取る）。
2. detect.py（候補を (screen_id, lead_key, change_category, period_from, period_to) で一意に登録。row_refs_* から excluded_* を除いた行で Decimal で基礎集計を計算し直して登録し、除いた行と理由と件数を request_json に残す。比べる2年度の基礎集計を leads.calc_from・calc_to に入れる。差額と増減率の派生計算。基準値0以下の増減率と、amount_incomparable の差額・増減率は作らない。dataset.yaml から partial_year などの理由を足す。各検出SQLの前に dataset.yaml の screens.<名前>.requires を確かめ、そろわなければ検出SQLも母集団のSQLも実行せず、counts_json に not_run と欠けた資料を書いて partial にする。compared: 0 とは書かない）。
3. run --resume（再開できる状態、工程ごとの入力のハッシュ、入力が変わったときの拒否、候補を作り直さない）。
4. テスト：DT-1〜DT-5、RS-1、RS-2、SC-1（検出と母集団の数）。

完了条件：合成データで3本の検出が期待どおりの候補を出す。実データの令和6・7年度で run を通し、検出SQLごとの候補の数と比べた組の数を報告する（候補の中身の判断はしない）。参考として、資料の作成時に簡易の取り込みで試した数（S1が394件で、うち amount_incomparable が15件、比べた組432・比較できない組79。S2が15件。S3が14件）と大きく違えば、理由を調べて報告する。簡易の取り込みは単価・理由の判定が粗いので、一致は求めない。
```

### P0-6 検証（V1〜V4、V8）と候補のメモ（11/6午後。約3時間）

```text
（共通ルール）

目的：check（V1〜V4、V8）と render（候補のメモ）を実装し、1件を入力からメモまで通せるようにする。

読む資料：設計書2.1・5.4・5.5・6章、implementation_spec.md の4.6・4.7・9・10.1・11章、test_plan.md の CK-1（V1〜V4、V8）・CK-2・RD-1・RD-2。

作るもの：
1. check.py の V1〜V4 と V8（implementation_spec 9章の表のエラーコード。T6a・T6b の否定は機械で判定し、Claude の登録と食い違えば不合格。比較成立には、calc_from・calc_to の行をすべて覆う confirmed の対応を求める V8_MATCH_UNCONFIRMED と、係数の提案が残っていないことを求める V8_COEFFICIENT_PENDING を含む）。不合格はすべて集めて返す。合格したらメモに出る主張を checked にする。
2. hashing.py（implementation_spec 11章。この時点で検証記録のハッシュをメモに入れる。失効の表示は P0-8）。
3. render.py の候補のメモ（implementation_spec 10.1 の8節。絞り込みは1節、係数の提案は4節と7節の先頭に出す。{calc:ID} の展開、型と状態の日本語表示、記者の確認リスト、上書きしない番号付け）。
4. テスト：CK-1（V1〜V4、V8 の各コード）、CK-2、CK-5、CP-1（V8 とメモ）、RD-1、RD-2。

完了条件：合成データの候補1件について、record で仮説・対応（confirmed）・計算・主張・判定・質問・結論の案を登録し、check に合格し、render がメモを書く。わざと必須の中身を欠いた候補（対応がない比較成立を含む）は不合格になり、メモが書かれない。
```

### P0-7 1件を通す（11/6夕方。利用者が調査のセッションで行う）

開発のセッションで、まず次を頼む。

```text
（共通ルール）

目的：調査のセッションで1件を通す前に、Skill と CLAUDE.md を実装に合わせて見直す。

作業：
1. .claude/skills/investigate/SKILL.md、record-examples.md、.claude/skills/review-lead/SKILL.md、CLAUDE.md を、ここまでの実装（コマンド、引数、提案の種類、エラーコード）と照らし、食い違いを直す。とくに、inspect <ID> --run <run_id> の形、/review-lead [lead_id] [run_id] の引数、T6a・T6b を提案しないこと、run_config の compare_years・series_years を確かめる。SKILL.md の本文は5,000トークン以内に保つ。あわせて、adapter_cao_procurement.md の要点（対象年度、比較の単位、単価・指数表記・担当官の所属の注意点、実データの受注者名は書かないこと）を短くまとめた datasets/cao-procurement/README.md を作る（D-024）。
2. 直した点を報告する。設計書や実装仕様と食い違う直しが要る場合は、直さずに報告する。
```

次に、利用者が `scripts/investigate` で調査のセッションを起動し、次のように始める。

```text
/investigate cao-procurement

令和6・7年度の内閣府の公共調達で、同種の業務の契約金額・契約方式・随意契約の理由の変化を調べたい。
まず1件だけ、入力からメモまで通すことを目標にする。候補を選んだら、選んだ理由を教えてほしい。
```

通ったら、開発のセッションに戻り、詰まった箇所（拒否された操作、分かりにくかったエラー、Skill の指示の不足）を伝えて直させる。

```text
（共通ルール）

調査のセッションで1件を通した。詰まった箇所は次のとおり。原因を調べ、仕様の範囲で直してほしい。仕様を変える必要があるものは、直さずに報告してほしい。
（ここに詰まった箇所を貼る）
```

### P0-8 残りの検証、実行の概要、公開、失効（11/7午前。約4時間）

```text
（共通ルール）

目的：V5〜V7、巡回数、render <run_id>、render --public、check --publish（P0の範囲）、失効の判定（ハッシュ、規則の版、fetch --recheck）を実装する。

読む資料：設計書4.2・4.3・5.5・9.12、implementation_spec.md の3.5・4.2（--recheck）・4.3（失効の表示）・4.6・4.7・9・10.2・10.3・11・12章、test_plan.md の CK-1（V5〜V7）・CK-3・CK-4・RD-3・RD-4・HS・PB-1・KN-1・SF-1。

作るもの：
1. check.py の V5〜V7（V7 の氏名の一覧と取り込まない列の値は検査のたびに原本から作り保存しない。取り込まない列の値は担当官の所在地（index 3）と相手方の住所（index 6）だけで、切り出し方は implementation_spec.md 12.2。D-032。強い語は警告）と巡回数の規則。record の時点の V7 の検査をつなぐ（取り込まない列の値も照らす）。
2. render <run_id>（実行の概要。候補0件と partial の書き分け。絞り込みがあれば、記者の指示、条件、資料ごとの除いた行の数を「処理の範囲」に出す）。
3. render --public（受注者名と法人番号の伏せ字、examples/disclosure.allow。CSVの受注者名の `?` は implementation_spec.md 12.1 の規則で照らす。D-022）。
4. publish.py（check --publish の P0 の4項目。取り込まない列の値の範囲は 1. と同じ（D-032）。`?` を含む値は implementation_spec.md 12.2 の規則で照らす。補助資料のファイル名とハッシュは環境変数 KIZASHI_DENYLIST のファイルから読む）。検査の対象は、ファイル名の一覧ではなく中身にする：作業フォルダーの manifest のファイル、ステージの blob（git ls-files -s）、すべての参照からたどれる履歴の blob（git rev-list --objects --all）。テストは一時の Git リポジトリで行う（PB-1）。
5. 失効：inspect の失効の表示、check と render の規則の版の照合（E_RULE_VERSION）、fetch --recheck（新しい版の保存、supersedes、古い版に依存する主張の stale、events の recheck）。
6. テスト：CK-1（V5〜V7）、CK-3、CK-4、RD-3、RD-4、HS-1〜HS-3、PB-1、KN-1、SF-1、SC-1（実行の概要）。

完了条件：上のテストが通る。P0-7 で作ったメモについて、主張を1つ取り下げると inspect が失効と表示し、check と render をやり直すと新しい番号のメモができる。
```

### P0-9 評価と公開物の準備（11/7午後。約3時間）

```text
（共通ルール）

目的：評価の記録の場所と、README と公開物の準備を整える。評価そのもの（記者の判断）は利用者が行う。

読む資料：設計書7.6・7.7・9.12、test_plan.md の3・4章。

作るもの：
1. eval/cases.md、eval/misses.md、eval/grouping.md、eval/summary.md のひな形（test_plan.md 4章の項目）。評価ケース6件の候補を、実行の台帳から選ぶための手順（どの inspect を使うか）も書く。
2. README.md：何をするツールか、記者の役割、導入（uv sync、Claude Code の版）、調査の始め方（scripts/investigate、/investigate）、メモの読み方（結論、説明の状況、記者の確認リスト、記者未確認の意味）、再現の手順（4種類の再現の範囲）、限界（設計書の残るリスクの要点）、イベント中に作ったコードと既存の知識ファイルの区別、ライセンス（D-008）、公開しないもの。
3. examples/ の準備：publish.manifest のひな形と、disclosure.allow の説明（記者が書く）。
4. 全テストと ruff を通し、テストの件数を区分ごとに報告する。

完了条件：README だけを読んだ第三者が、導入から調査の開始とメモの確認まで進める。check --publish が、ひな形の状態で何を検査するかを説明できる。

止まる条件：README に書く内容が、設計書・実装・決定記録と食い違う場合は報告して止まる。
```

---

## 3. P1のプロンプト（P0がすべて動いた場合だけ）

### P1-a 別セッションでの読み直し

```text
（共通ルール）

目的：設計書9.3の P1「別セッションでの読み直し」の起動スクリプト scripts/review-memo を作る。

作るもの：scripts/review-memo <run_id> <lead_id>。次を実行する：
claude --permission-mode dontAsk --setting-sources project --tools "Read,Grep,Glob" --disallowedTools "mcp__*"
最初の指示として、メモのパスと資料のページ別テキストの場所を渡し、「否定の根拠が支えになっていない箇所、言い過ぎ、根拠の位置が主張を支えていない箇所を挙げる。書き込みはしない」と頼む文を用意する。
完了条件：起動したセッションでファイルを書けず、MCPのツールがないことを確かめる手順を README に書く。
```

### P1-b 法人番号の補完

```text
（共通ルール）

目的：設計書7.4の3条件で、PDFの13桁の法人番号を採用する。検査用数字の計算方法は国税庁の公表仕様で確かめ、docs/decisions.md に出典を書いてから実装する（U8）。推測で計算方法を書かない。
```

### P1-c PRTRの骨組み

```text
（共通ルール）

目的：設計書8.4のPRTRの骨組みを作る。最初に docs/adapter_prtr.md を、NITEの公表ページとファイル構成の説明を実際に取得して書き、利用者の確認を受ける。その後、datasets/prtr/ の dataset.yaml、型付きの表、検出SQL 1本、合成のテスト用データだけを作る。メモまで通せなければ「分野を追加できる設計」と README に書き、「2分野で実証済み」とは書かない。
```

### P1-d サンドボックス

```text
（共通ルール）

目的：設計書9.5のサンドボックスを試す。investigate.settings.json の写しに sandbox の設定（failIfUnavailable、autoAllowBashIfSandboxed: false、allowUnsandboxedCommands: false、allowedDomains）を加え、kizashi と取得が動くかを1時間を上限に試す。共通設定の Edit(/data/**) の拒否が CLI の書き込みを塞ぐかも確かめる。動かなければ元に戻し、結果を docs/decisions.md に書く。
```

---

## 4. 補助のプロンプト

### 続きから再開

```text
（共通ルール）

前のセッションで <プロンプトの番号> の途中まで進めた。git log と git status、docs/decisions.md、test_plan.md の該当のテストの状態を見て、終わったことと残りを整理し、残りから続けてほしい。最初に、残りの作業の一覧を示すこと。
```

### 実装と仕様の突き合わせ（区切りごとのレビュー）

```text
（共通ルール）

ここまでの実装を、docs/implementation_spec.md と docs/adapter_cao_procurement.md に照らしてレビューしてほしい。コードは変えない。次を、ファイルと行を付けて挙げること：
1. 仕様と違う動き（エラーコード、出力の形、状態の遷移、上限）
2. 仕様にあるのに実装もテストもないもの（test_plan.md のIDで）
3. 仕様にない機能
4. 金額の計算に浮動小数点が入る箇所、氏名が保存されうる箇所、data/ の外に書く箇所
```

### 不具合の調査

```text
（共通ルール）

次の不具合を調べてほしい。まず再現するテストを書き、原因を特定してから直すこと。仕様の変更が要る場合は直さずに報告すること。
（ここに、実行したコマンド、出力のJSON、期待した結果を貼る）
```
