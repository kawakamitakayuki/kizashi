# 内閣府アダプタ仕様（`datasets/cao-procurement/`）

- 対象：内閣府「公共調達の適正化について」に基づく、競争入札と随意契約（物品・役務等）の公表データ。
- P0の範囲：令和6・7年度（2024・2025年度）の競争入札と随意契約のCSVとPDF。令和5年度と令和8年度（年度途中）は系列の参考として読めるようにするが、P0の完了条件には含めない。
- 事実の出所：2026年11月3日に取得したCSV 8本とPDF 1本、公表ページ（`kohyo.html`）を本書の作成時に読み直して確かめた【確認済み】。初日に取り直して変わっていないか確かめる。→ 2026-11-05 に令和5〜8年度のCSV 8本とPDF 8本を取り直して確かめた。数値の事実はすべて一致し、本書にない事実を足した（D-005）。

---

## 1. 取得元と論理資料

| 項目 | 値 |
| --- | --- |
| 公表ページ | `https://www.cao.go.jp/chotatsu/kohyo/kohyo.html` |
| リンクの形 | `tekiseika/<令和の年>nendo/<日付>[-<枝番>]_kohyo_<kyoso\|zuikei>.<csv\|pdf>`（ページからの相対パス）【確認済み】 |
| 年度 | 令和の年＋2018（`6nendo` → 2024年度） |
| 論理資料のキー | `cao-procurement\|<年度>\|<kyoso\|zuikei>\|<csv\|pdf>` |
| 公表ページの注記 | 「PDF形式とCSV形式のデータは同一のものです。」【確認済み】 |
| 更新 | 月1回。当年度のファイルは差し替わり、ファイル名の日付が変わる。過去年度も差し替えられうる |

取得時のファイル名（参考。URLは固定しない）：令和5年度 競争 `20241105-2`・随意 `20240711-2`、令和6年度 競争 `20250711-2`・随意 `20250514`、令和7年度 競争 `20260512-02`・随意 `20260512-03`、令和8年度 競争・随意 `20261009`。

## 2. ファイルの形【確認済み】

| 項目 | 内容 |
| --- | --- |
| 文字コード | 令和5〜7年度はcp932、令和8年度はBOM付きUTF-8 |
| 先頭の行 | 1行目は表題、2行目は作成日（例「令和７年７月11日現在」）。作成日は1列目ではなく、最後の公表の列にある（0始まりで、令和5〜7年度の競争入札は15列、令和5〜7年度の随意契約と令和8年度の競争入札は16列、令和8年度の随意契約は17列）【確認済み：D-005】。令和8年度の随意契約は2行目に「×」と「←公表時削除すべき項目」の印がある |
| 見出し | 3・4行目の2行（0始まりで2と3）。列ごとに縦に連結し、NFKC正規化と空白・改行の除去をした文字列を「見出しの文字列」とする |
| データ | 5行目から。末尾に注記の行はない |
| セル | セルの中に改行がある（担当官、方式、理由、金額など）。CSVのレコードの順番（論理レコード番号）は物理的な行番号と違う。PDFの文字がASCIIの `?` に置き換わったセルがある。取り込む列では、相手方に3件（令和6年度の随意契約1件、令和7年度の競争入札2件）、金額に2件（令和5年度の競争入札と随意契約に各1件）。取り込まない相手方の住所の列に13件。PDFでは `−`、`一`、`㎥`、`ℓ` などだった。cp932で表せない文字を書き出すときに置き換えたとみられる（確かめていない）。担当官の列に `?` はない。`?` は直さずに元の値として扱う。原本照合では `mismatch` になる（7.3）。受注者名の伏せ字と公開の検査では、`?` を任意の1文字として照らす（実装仕様12章。D-022）【確認済み：D-005】 |
| 件数 | 令和5年度 競争802・随意669、令和6年度 競争858・随意422、令和7年度 競争881・随意391、令和8年度 競争611・随意326 |

**論理レコード番号**：見出しの2行の後のCSVのレコードを、空のレコードも含めて1から数える。正規化の経路と別経路の読み込み処理は、同じ数え方をする。

## 3. 列の対応

番号は0始まり。「見出し」は見出しの文字列（2章）。「取り込む名前」が空の列は取り込まない。見出しが空の列は別経路の読み込み処理で特定できないため、取り込まない（設計書7.2）。

### 3.1 競争入札 令和5〜7年度（16列。3年度で見出しが同じ）

| 番号 | 見出し | 取り込む名前 | 備考 |
| --- | --- | --- | --- |
| 0 | `NO` | `no` | |
| 1 | `物品役務等の名称及び数量` | `title_raw` | |
| 2 | `契約担当官等の氏名並びにその所属する部局の名称及び所在地` | `officer_raw`（氏名を除く。4.3） | |
| 3 | （空） | — | 担当官の所在地 |
| 4 | `契約を締結した日` | `contract_date_raw` | |
| 5 | `契約の相手方の商号又は名称及び住所` | `counterparty_raw` | 実際は名称だけ |
| 6 | （空） | — | 相手方の住所 |
| 7 | `法人番号` | `corp_number_raw` | |
| 8 | `一般競争入札・指名競争入札の別(総合評価の実施)` | `method_raw` | |
| 9 | `予定価格` | `estimate_raw` | P0では指標にしない |
| 10 | `契約金額` | `amount_raw` | |
| 11 | `落札率` | `rate_raw` | P0では指標にしない |
| 12 | `公益法人の場合公益法人の区分` | — | |
| 13 | `国所管、都道府県所管の区分` | — | |
| 14 | `応札・応募者数` | — | 各年度1〜12件しか記入がない |
| 15 | `備考` | `note_raw` | |

### 3.2 随意契約 令和5・6年度（17列）

| 番号 | 見出し | 取り込む名前 |
| --- | --- | --- |
| 0〜7 | 3.1の0〜7と同じ | 同じ |
| 8 | `随意契約によることとした会計法令の根拠条文及び理由(企画競争又は公募)` | `reason_raw` |
| 9 | `予定価格` | `estimate_raw` |
| 10 | `契約金額` | `amount_raw` |
| 11 | `落札率` | `rate_raw` |
| 12 | `再就職の役員の数` | — |
| 13 | `公益法人の場合公益法人の区分` | — |
| 14 | `国所管、都道府県所管の区分` | — |
| 15 | `応札・応募者数` | — |
| 16 | `備考` | `note_raw` |

### 3.3 随意契約 令和7年度（27列）

0〜16は3.2と同じ。17〜26は取り込まない（令和8年度に「公表時削除すべき項目」とされた列と同じ種類の内部用の列）：`契約内容【該当するものをプルダウンから選択】`、`電子応札【未対応案件であれば"○"を選択】`、`電子契約【対応案件であれば○を選択】`、`応札者数【数字を記入】`、`うち電子応札者数`、`発議係`、`部局名称`、空の見出し3列。

### 3.4 令和8年度（参考。P0では必須にしない）

- 競争入札（17列）：8は `一般競争入札・指名競争入札の別(総合評価の実施、単価契約の有無)` に変わる。15に `調査研究における成果物の活用(URL及び公表時期又は公表予定時期)` が入り、16が `備考`。
- 随意契約（28列）：12〜14が公益法人と応札・応募者数、15が `再就職の役員の数`、16が成果物、17が `備考`。18〜27は取り込まない（2行目の印で19〜25列目＝0始まりの18〜24が「公表時削除すべき項目」）。
- 随意契約の18〜27列の見出しの文字列は、`契約内容【該当するものをプルダウンから選択】`、`電子応札【未対応案件であれば"○"を選択】`、`電子契約締結案件【該当の場合○を選択】`、`企画競争における提案者数公募における参加者数不調・不落における競争参加者数【数字を記入】`、`うち電子応札者数`、`発議係`、`名称`、空の3列（25〜27列）【確認済み：D-005】。
- PDFの列の数は、競争入札17列、随意契約18列で、CSVの公表の列（随意契約は0〜17列）と同じ並び【確認済み：D-002】。
- 年度途中のデータなので、`dataset.yaml` で `partial_year: true` とし、比較できない理由に `partial_year` を付ける。

## 4. 正規化の規則

正規化の経路（`readers.py` と `adapters/cao_procurement.py`）の規則である。元の値は `records.cells_json` に、10章の `take` のキーで残す（担当官のセル `officer_raw` と取り込まない列を除く。実装仕様5章）。

### 4.1 共通

- 文字列は前後の空白を除く。比較やキーに使うときだけNFKCにし、空白・改行を除く。元の値は変えない。
- 空欄、`-`、`－`、`―` は値の欠けとして区別して記録する（ゼロにしない）。

### 4.2 日付

- 形式は `R<年>.<月>.<日>`（例 `R6.4.1`）【確認済み】。令和の年＋2018で西暦にし、ISO（`2024-04-01`）にする。
- `令和6年4月1日` の形にも対応する。どちらでもなければ `NULL` とし、品質検査（Q12）に出す。

### 4.3 契約担当官のセル（3列目）

- 1つのセルに、役職（`支出負担行為担当官`、`分任支出負担行為担当官`、`支出負担行為担当官代理` など）、所属、氏名が改行や空白で並ぶ。2人以上の担当官が入ったセル（共同の調達）がある（競争入札 令和6年度33行・令和7年度28行、随意契約 36行・33行）【確認済み】。令和5年度は競争入札34行・随意契約31行、令和8年度は競争入札14行・随意契約30行【確認済み：D-005】。
- 分け方：
  1. `(分任)?支出負担行為担当官(代理)?` が現れるたびに、担当官の区切りとする。
  2. 各担当官について、役職の後の文字列を空白（全角を含む）と改行で分けた語の並びにする。
  3. 最後の2語を氏名とみなして捨て、残りを所属とする。語が2つしかなければ最後の1語を氏名として捨てる。1つ以下なら分けられないとして所属を `NULL` にし、品質検査（Q05）に出す。
- 所属は `officer_units_json`（配列）に入れる。氏名はどこにも保存しない。V7と `check --publish` が使う氏名の一覧は、検査のたびに原本からこの規則で取り出す。同じ2つの検査で照らす取り込まない列の値は、担当官の所在地（index 3）と相手方の住所（index 6）だけで、検査のたびに原本から取り出す（範囲と切り出し方は実装仕様12.2。D-032）。
- 初日に、取り出した所属の一覧（異なり数は数十の見込み）を人が見て、氏名が混じっていないかを確かめ、結果を `docs/decisions.md` に書く。
- 3列目の所属は契約担当官の所属であり、発注を求めた部局とは限らない。表示では「契約担当官の所属」と書く。

### 4.4 金額

- `^\d{1,3}(,\d{3})*$` または `^\d+$` なら整数の円にして `amount_yen`（INTEGER）に入れる。
- 整数の形でも、台帳の整数（SQLite の INTEGER）の範囲（2^63−1）を超える値は `amount_yen` を `NULL`、`amount_note` を `out_of_range` にする（数値なので `non_numeric` にしない。元の値は `amount_raw` に残し、合計から除き、Q11 に出す。2^63−1 は取り込む。別経路の読み込み処理にも同じ上限を独立に書く。D-091③）。検出SQL（S1）は、除く理由を `amount_out_of_range` と書き分け（D-092③）、原本照合（7.3）では数値として照らす（D-092④）。
- それ以外は `amount_yen` を `NULL` にし、`amount_note` に種類を入れる：空欄・`-`・`－` は `missing`、外貨（`ドル`、`USD`、`EUR`、`ポンド`、`ウォン`、`ユーロ` などを含む）は `foreign_currency`、`単価` を含むものは `unit_price_text`、それ以外は `non_numeric`（例 `9ヵ月＠…円×…台ほか`）【確認済み】。

### 4.5 単価契約

- 全年度・両方式で、方式の列（競争入札）または金額の列に `単価` を含めば `unit_price = 1` とする。令和5年度の競争入札は、方式の列に `単価` を含む行が74行、金額の列に含む行が58行（その一部）あり、片方だけを見ると見落とす【確認済み】。
- 単価契約の行は金額の合計に入れない（検出SQL）。

### 4.6 法人番号

| 状態 | 条件 |
| --- | --- |
| `digits13` | 13桁の数字がちょうど1つ（末尾の空白は除く） |
| `exponent` | `\d\.\d+E\+\d+` の形（桁が失われている）。復元しない |
| `missing` | 空欄、`-`、`－` |
| `multiple` | 13桁の数字が2つ以上（改行で区切られる） |
| `invalid` | それ以外 |
| `valid_from_pdf` | 正規化では付けない。`exponent` の行のうち、原本照合（T6b。7.2で `matched`）の PDF の法人番号の欄（7列目）が13桁ちょうど1つで、設計書7.4の3条件をすべて満たすものに、工程 `reconcile_source` が付け、13桁を `corp_numbers_json` に入れる（P1-b。D-077） |

- 13桁の文字列は `corp_numbers_json`（配列）に入れる。
- 補完の3条件（P1-b。D-077）：①検査用数字が合う（法人番号の指定等に関する省令第2条の算式。D-076）。②CSV の指数表記に残る有効桁と合う（表示の小数点以下の桁の数で、13桁の値を四捨五入して仮数と比べる。表示は末尾の0を省いた形で、桁の数は1〜5桁）。③同じ行の件名（`title_raw`）と金額（`amount_yen`）の照合が `match`。1つでも満たさない行と、PDF の行が対応しない行は `exponent` のまま（未確定）とし、同一法人として数えない。CSV と PDF のどちらかを正解にはしない。結果と満たさない条件は、照合の記録の行の `corp_completion` に書く（実装仕様7.4）。品質検査の Q07 は補完の前に数える。【確認済み：2026-11-07 に令和5〜7年度の6本（P0-7 の実行）で照らし、指数表記の 3,427 行のうち 3,419 行が3条件を満たし、検査用数字が合わない4行と件名か金額が合わない4行が未確定。有効桁は 3,427 行すべてが四捨五入で合い、切り捨てでだけ合う行はなかった】
- 指数表記の分布：競争入札は全年度、随意契約は令和5・6年度。令和7年度の随意契約は13桁で入っている【確認済み】。

### 4.7 方式と理由

- 競争入札の方式：`一般競争`、`一般競争(総合評価)`、`一般競争(単価)`、`一般競争(総合評価・単価)` の4種類（令和6・7年度）【確認済み】。`method_family = 'kyoso'`、`method_detail` に総合評価・単価の有無を入れる。
- 随意契約：`method_family = 'zuikei'`。理由の分類（`reason_category`）は、NFKCにし、空白（全角を含む）と改行を除いた理由の文字列（4.1）に次の語が含まれるかを、上から順に見て決める（語の途中の改行で取りこぼさない。元の値は `reason_raw` に残す。D-090⑤）。

| 順 | 含む語 | 分類 |
| --- | --- | --- |
| 1 | `企画競争` | `planning_competition` |
| 2 | `不調` | `failed_tender` |
| 3 | `公募` | `open_call` |
| 4 | `競争を許さない` | `nature_no_competition` |
| 5 | `慈善` | `charity` |
| 6 | （どれでもない） | `other` |

- 根拠条文（`reason_articles`）：NFKCにし、空白と改行を除いた理由の文字列から（D-090⑤）、`(会計法令?|予算決算及び会計令|予決令)第?\d+条(の\d+)*(第\d+項)?(第\d+号)?` の出現を取り出す。`予算決算及び会計令` と `予決令` は `予決令` にそろえ、`第` の有無をそろえて、並べ替えた配列にする。`予算決算及び会計令99条の5` のように「第」がない表記や、`会計法令第99条の2` のような表記もある【確認済み】。

### 4.8 件名と同種業務のまとめ

- `group_key`：件名をNFKCにし、空白を除き、次の形を上から順に取り除いたもの（括弧の形を先に取り除く。年度を先に取り除くと空の括弧が残るため）。取り除いた文字列は `title_removed_parts` に残す（業務量の変化を見落とさないため）。本書の作成時の検証では、この規則で令和6・7年度の両方に現れるキーは約500だった【確認済み：規則の素案による試算】。

| 取り除く形（正規表現） | 例 |
| --- | --- |
| `[（(][^）)]*年(度\|版)[^）)]*[）)]` | （令和７年度） |
| `令和(\d+\|元)年(度\|版)` | 令和６年度、令和６年版 |
| `平成\d+年度`、`\d{4}年度`、`R\d+年度` | |
| `第\d+期` | 第３期 |
| `\d+月分`、`\d+か?月間` | |
| `[（(][）)]` | 取り除いた後に残った空の括弧 |

- 同じ `group_key` を同種業務の候補とする。名称が似ているだけで同種と確定しない（設計書5.3）。確定はClaudeの「対応」の登録（`procurement_matches`）で行う。

### 4.8a 母集団の絞り込みに使える項目

記者の指示で母集団を絞るときに使える項目は、10章の `scope_fields` の4つ（契約担当官の所属、件名に含む語、方式、随意契約の理由の分類）である。

- 「担当部局」で絞る場合、データにあるのは3列目の契約担当官の所属で、発注を求めた部局とは限らない。令和6・7年度の4ファイル（2,552行）では、1,902行（74.5%）の契約担当官が会計担当の参事官（`内閣府大臣官房会計担当参事官` など）で、部局ごとの契約担当官がいるのは内閣衛星情報センター、原子力防災担当、食品安全委員会事務局などに限られる【確認済み：資料の作成時の試算】。所属で絞れるのはこうした部局だけで、ほかの部局（例：政策統括官（防災担当））の契約は所属では絞れない。その場合は件名に含む語で近似するかを記者に確かめる。メモと実行の概要には「契約担当官の所属による絞り込み」と書く。
- 「業界」で絞る列はない。受注者の業種は公表データに含まれず、件名に含む語（例：システム、清掃）で業務の種類を近似するしかない。受注者の業種を法人番号で外部のデータ（gBizINFOなど）から引く方法は、指数表記で桁が落ちた法人番号が多いこともあり、P1以降に検討する。記者から業界での絞り込みを指示されたら、Claudeはこの制約を伝え、件名の語で近似するかを記者に確かめる。

### 4.9 複合キーと重複

- 複合キー：`group_key`ではなく元の値の `title_raw`・`contract_date`・`amount_raw`・`counterparty_raw`。同じ版の中で一意でない行は `no` で区別する。
- NO以外のすべての列が同じ行の組に、同じ `dup_group` を付ける（令和7年度の競争入札に3組、随意契約に令和5年度8組・令和6・7年度各1組）【確認済み】。黙って除かない。
- 複合キーが一意でない行も品質検査（Q09）に出す。件数は初日に、このキーで数え直して `docs/decisions.md`（D-005）に書く（本書の作成時に別のキーで数えた件数とは違う）。このキーで一意でない組は、令和5年度の競争入札1組（2行）・随意契約8組（20行）、令和6年度の競争入札0組・随意契約1組（3行）、令和7年度の競争入札3組（6行）・随意契約1組（3行）、令和8年度は両方式とも0組。随意契約では、どの組もNO以外が同じ行の組と同じだった【確認済み：D-005】。

## 5. 品質検査

`quality.py` が行い、`quality_report.md` に書く。「必須」が1つでも実行できなければ `run` は `failed`。

| ID | 検査 | 必須 | 結果の扱い |
| --- | --- | --- | --- |
| Q01 | 文字コードで読めるか | 必須 | 読めなければ停止 |
| Q02 | 見出しの文字列が `dataset.yaml` の期待と一致するか（取り込む列は一意に特定できるか） | 必須 | 違えば停止（未知の様式） |
| Q03 | 列の数が期待どおりか | 必須 | 違えば停止 |
| Q04 | 取り込まない列（氏名の入りうる列、内部用の列）を除いたか | 必須 | 除けなければ停止 |
| Q05 | 担当官のセルから氏名を除けたか（4.3で分けられないセルの件数） | 必須 | 件数を報告。0でなければ人が確かめる |
| Q06 | 所属の一覧 | — | 異なり数を報告。一覧は品質レポートに書かない（データの更新で氏名の断片が混じるおそれを避けるため。行ごとの所属は `officer_units_json` にある。D-028） |
| Q07 | 法人番号の状態ごとの件数 | — | 報告。`exponent` は比較できない理由（受注先の主張） |
| Q08 | NO以外が同じ行の組 | 必須 | 報告。比較できない理由 `duplicate_rows` |
| Q09 | 複合キーが一意でない行 | — | 報告 |
| Q10 | 単価契約の件数 | — | 報告 |
| Q11 | 数値にならない金額の件数（種類ごと。整数の範囲外の `out_of_range` を含む。D-091③） | — | 報告 |
| Q12 | 日付を読めない行 | 必須 | 報告。比較できない理由 `bad_date` |
| Q13 | ファイル単位の突合：CSVの行数と、取り込んだ行の数。金額の合計（Decimal）を、別経路の読み込み処理と正規化の経路で比べる | 必須 | 違えば停止 |
| Q14 | NOの欠番・重複 | — | 報告 |
| Q15 | 予定価格の非公表・落札率の「-」の件数 | — | 報告（指標にしない理由の根拠） |
| Q16 | 年度途中のファイル | — | `partial_year` を付ける |

## 6. 別経路の読み込み処理（T6aとV3(b)で共有）

- 正規化の経路とは別のモジュール（`independent_reader.py`）で、csvモジュールでファイルを読む。
- 見出しの行は、最初のセルをNFKCにして `NO` になる行として探し（固定の行番号を使わない）、その行と次の行を縦に連結して見出しの文字列にする。上の行の空欄を左の値で埋めない。
- `dataset.yaml` の期待する見出しの文字列で列を探す。比べる前に、両方の文字列をNFKCにし、改行を含む空白をすべて除く（実データの見出しは全角の `ＮＯ` や全角の括弧で、セルの中に改行がある【確認済み】）。一致する列がちょうど1つでなければ `V3B_HEADER_UNRESOLVED`。見出しが空の列は使えない。
- 値の読み方：金額は4.4と同じ正規表現で整数にする（正規化の経路の関数を呼ばない。同じ規則を独立に書く）。台帳の整数の範囲（2^63−1）を超える値は、正規化の経路と同じく数値として扱わない（同じ上限を独立に書く。D-091③）。
- 取り込みの照合（T6a）：論理レコード番号ごとに、`no`、`title_raw`、`contract_date_raw`、`counterparty_raw`、`corp_number_raw`、`method_raw`／`reason_raw`、`amount_raw`、整数にした金額を、正規化の経路の値と比べる。
- V3(b)：基礎集計の `row_refs` の行を、この読み込み処理で読み直し、合計・件数を数え直す。

限界：同じ列を両方の経路で誤読する故障、行の入れ替え、過大と過小の相殺は残りうる（設計書5.5）。

## 7. 原本照合（PDF。T6b）

`run` の `reconcile_source` が、同じ年度・方式のCSVとPDFの組ごとに、CSVの全行について行う。結果は実装仕様7.4の形で記録する。

### 7.1 セルの切り出し（方法(a)。D-002）

- pdfplumber 0.11.10 で、各ページの細い四角形（`rects`）と文字（`chars`）を読む。罫線は線のオブジェクトではなく、細い四角形で描かれている【確認済み：令和5〜8年度の8本。D-002】。
- 列の境界：幅が2pt未満で高さが3ptを超える四角形の中心の x を、差が1.5pt以内のものをまとめて、列の境界とする。列の数（境界の数−1）が `dataset.yaml` の `pdf_columns` と違うページは、切り出せないページとして使わない（そのページの件は7.2で `not_found` になる）。
- 行の境界：高さが2pt未満で幅が3ptを超える四角形のうち、NOの列（0列目）の中央の x を横切るものだけについて、中心の y を同じようにまとめ、行の境界とする。セルの中だけにある横線を行の境界にしないためである（令和8年度の随意契約には、成果物の活用の列などにセルの中の横線がある。すべての横線を行の境界にすると、1件が複数の行に分かれ、件名・理由・相手方の不一致が95件出た【確認済み：D-002】）。
- 文字の振り分け：空白でない文字を、文字の中心の座標で、行と列のセルに振り分ける。このとき、NOの列と照合に使う列（7.3の表の列）以外の文字は捨て、文字列にしない。担当官の列（2列目）は照合に使わないので、縦に1文字ずつ並んだ氏名もここで捨てる。【確認済み：8本で、CSVから4.3の規則で取った2字以上の氏名の語（99語）は、照合に使う列には、CSVの同じ列にもある語（地名・会社名）としてしか出なかった。1字の語は調べていない。D-002】
- セルの文字列：セルの中の文字を、上下の中心の順に見る。文字の上下の中心が、その行の最初の文字の上端と下端の間にあれば同じ行にまとめる。行ごとに x の順に並べ、前の文字との間が文字の大きさの0.8倍を超えるところと、行の区切りに半角の空白を入れて連結する。上端の差で行をまとめると、別のフォントで描かれた文字（例：康熙部首の「⼊」U+2F0A）が別の行になり、並びが変わる【確認済み：令和8年度の随意契約の件名1件。D-002】。
- 見出し：ページの上から、NOのセルが `NO` の行と、NOのセルが空で、照合に使う列の文字がどれも期待する見出しの文字列（`take` の `header`。NFKC・空白除去）の一部である行を、見出しとする。
- 分かれた行：見出しより下で、NOのセルが数字でなく、照合に使う列に文字がある行は、1件が複数の罫線の行に分かれたものとみなす（前のページから続く行を含む）。そのページのすべての件と、その行がページの最初にあるときは前のページの最後の件を、7.2で `ambiguous`（理由 `split_row`）にする。【確認済み：8本とも、見出しはどのページも1行で、分かれた行はなかった。D-002】
- 公表一覧のPDF（`sources.kind='dataset'`）は、ページ別テキストを作らない（担当官の氏名をファイルに書き出さないため。D-021）。記者の確認リストには、照合の記録のPDFのページ（実装仕様7.4の `pdf_page`）を載せる。

### 7.2 1段目：行の対応

1. 切り出した行のうち、NOのセル（NFKC・空白除去）がCSVの `no` と等しい行を、PDFの全ページから探す。
2. ちょうど1つなら `matched`。2つ以上なら、契約日のセル（4列目）にCSVの契約日の表記（NFKC・空白除去）を含むものに絞り、1つなら `matched`、それでも2つ以上か、絞って0になれば `ambiguous`（同じ NO の行はあるので、見つからないとはしない。D-030⑦・D-090⑭）。NO の候補そのものが0なら `not_found`。7.1の分かれた行に当たる件は `ambiguous`（理由 `split_row`）。
3. 金額は行を探すのに使わない（金額の食い違いを `mismatch` として検出するため）。

【確認済み：令和5〜8年度の8本の全4,960行で `matched`。D-002・D-003】

### 7.3 2段目：項目の照合

行が `not_found`・`ambiguous` なら、その行の項目はすべて `unverifiable`。`matched` なら、項目ごとに、対応する列のセルの文字列で照らす。PDFの列の番号は、CSVの公表の列と同じである【確認済み：8本。令和7年度の随意契約のPDFは17列で、CSVの内部用の10列はない。令和8年度の随意契約のPDFは18列】。

| 項目（照合の記録のキー） | 列 | 照らし方 |
| --- | --- | --- |
| `amount_yen` | 10 | CSVの金額が数値（4.4で `amount_yen` が整数）のとき：セルをNFKCにし、数値の並び（数字と桁区切り `,` の連なりで、前後が数字・`,`・`.` でないもの）を取り出す。桁区切りを除いた値がCSVの値と**等しい**ものがあれば `match`、数値の並びはあるが等しいものがなければ `mismatch`、なければ `unverifiable`。部分一致では照らさない（CSVの `1,000,000` はPDFの `11,000,000` に一致しない）。CSVの金額が数値でない（`amount_note` が `unit_price_text`・`foreign_currency`・`non_numeric`）とき：`amount_raw` とセルをNFKC・空白除去して、完全一致なら `match`、違えば `mismatch`、どちらかが空なら `unverifiable`。`amount_note` が `missing` なら `unverifiable`。`out_of_range`（台帳の整数の範囲を超える整数。4.4）は、`amount_raw` の数値の並び（ちょうど1つ）を、数値のときと同じ等しさで照らす（D-092④） |
| `corp_numbers` | 7 | CSVの法人番号が13桁（4.6の `digits13`）のとき、金額と同じ数値の並びの等しさで照らす。ほかの状態は `unverifiable`。PDFの13桁で補完した行（`valid_from_pdf`）も、CSV では有効桁までしか確かめられないので `unverifiable`（4.6。D-077） |
| `contract_date` | 4 | CSVの表記（`R6.4.1` の形。NFKC・空白除去）が、セルの中に前後が数字・`.` でない形であれば `match`。セルに `R数字.数字.数字` の形があり等しいものがなければ `mismatch`、なければ `unverifiable`。CSVの表記が `R…` の形でなければ、セルとの完全一致で照らす（8本の日付はすべて `R…` の形だった） |
| `title_raw`・`counterparty_raw`・`method_raw`・`reason_raw` | 1・5・8・8 | CSVの値とセルの文字列をNFKC・空白除去して、完全一致なら `match`、違えば `mismatch`、どちらかが空なら `unverifiable` |

- 割合による一致（断片の何%が見つかったか）は使わない。曖昧な一致を `match` に格上げしない。迷う場合は `unverifiable`（保留の理由 `source_check`）にする。
- CSVの `?`（2章）は、PDFの文字と違うので `mismatch` になる。CSVとPDFの実際の差であり、照合の誤りではない【確認済み：8本の不一致は、相手方3件と数値でない金額2件で、どれもCSVの `?`。D-002】。
- 旧版の方法(b)（範囲の行の断片を順に取り除く方式）と、前後のNOの行の間を範囲にする行の対応は使わない。初日の試験で、範囲の中のほかの文字に置き換えた変異が `match` になり、範囲に入った前後の件の方式の語を拾って、変えていない方式が `mismatch` になった。また、レイアウトを保ったテキストではNOの後の空白が1つになり、`^\s*<NO>\s{2,}` で行が見つからなかった（D-002）。
- 初日の合格の基準（U13）は次の4つで、方法(a)は令和6・7年度の各20件で満たした。全行（8本・4,960行）でも、抽出の誤りによる不一致は0件、1文字などを変えた変異106,434個のうち `match` は2個で、2個ともCSVの `?` の位置をPDFの文字に置き換えてPDFと同じ値になったものだった（D-002・D-003）。(1) 件名・理由の照合が95%以上の件で `match` になる、(2) わざと1文字変えた値（変異）が1件も `match` にならない、(3) 変えていない値が `mismatch` にならない（誤検出が0件）、(4) 金額の変異（先頭に1桁足す、末尾の1桁を変える）が `mismatch` になる。

### 7.4 変化の区分ごとの照合項目

T6bの判定（実装仕様9章の末尾）で、候補の行について `match` を求める項目である。`dataset.yaml` の `reconcile.fields_by_category` に書く。

| 変化の区分 | 照合項目 |
| --- | --- |
| `amount_up`・`amount_down`・`amount_incomparable` | `amount_yen`（数値でない金額は表記。7.3）、`contract_date`、`title_raw` |
| `kyoso_to_zuikei`・`zuikei_to_kyoso` | `title_raw`、`contract_date`、競争入札の行は `method_raw`、随意契約の行は `reason_raw` |
| `reason_changed` | `title_raw`、`reason_raw` |
| 受注先を扱う主張がある場合（どの区分でも） | 上に加えて `counterparty_raw`、`corp_numbers` |

「受注先を扱う主張がある場合」は、メモに出る記載の根拠（主張・判定・対応・係数の手がかり・文書値。D-065）の位置の列が受注先の列（`counterparty_raw`・`corp_number_raw`）か、メモに出る基礎集計の条件に受注先の列（`corp_number_state`）がある場合とする（機械で見分けられる範囲。D-058）。法人番号が13桁でない行（指数表記など）は法人番号の項目が照合不能なので、T6b は「未確認」になり、比較成立にできない（保留の理由 `source_check`）。メモの7節も同じ項目を使う。

## 8. 分野の表

```sql
CREATE TABLE procurement_contracts (
  record_id TEXT PRIMARY KEY REFERENCES records(record_id),
  record_status TEXT NOT NULL CHECK (record_status IN ('adopted','held','excluded')),
  fiscal_year INTEGER NOT NULL,
  partial_year INTEGER NOT NULL DEFAULT 0,
  method_family TEXT NOT NULL CHECK (method_family IN ('kyoso','zuikei')),
  no INTEGER NOT NULL,
  title_raw TEXT NOT NULL,
  group_key TEXT NOT NULL,
  title_removed_parts TEXT,
  officer_units_json TEXT NOT NULL DEFAULT '[]',
  officer_count INTEGER NOT NULL DEFAULT 1,
  contract_date TEXT,
  counterparty_raw TEXT,
  corp_numbers_json TEXT NOT NULL DEFAULT '[]',
  corp_number_state TEXT NOT NULL CHECK (corp_number_state IN ('digits13','exponent','missing','multiple','invalid','valid_from_pdf')),
  method_raw TEXT,
  method_detail_json TEXT,
  unit_price INTEGER NOT NULL CHECK (unit_price IN (0,1)),
  reason_raw TEXT,
  reason_category TEXT,
  reason_articles_json TEXT,
  amount_raw TEXT,
  amount_yen INTEGER,
  amount_note TEXT,
  estimate_raw TEXT,
  rate_raw TEXT,
  note_raw TEXT,
  dup_group TEXT,
  incomparable_reasons_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE procurement_matches (
  match_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL REFERENCES leads(lead_id),
  left_json TEXT NOT NULL,
  right_json TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('1:1','1:n','n:1','n:m')),
  reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK (status IN ('confirmed','pending','withdrawn')),
  created_at TEXT NOT NULL
);
```

基礎集計に使える表と列（許可リスト）：`procurement_contracts` の `amount_yen`（合計）と、件数。条件に使える列：`fiscal_year`、`method_family`、`group_key`、`unit_price`、`reason_category`、`corp_number_state`、`dup_group`、`record_id`。`record_status = 'adopted'` はCLIが必ず加える（実装仕様7.1）。

## 9. 検出SQL

- 検出SQLは `screens/` に、比較の母集団を数えるSQLは同じ名前で `populations/` に、年度の系列の行を返すSQLは同じ名前で `series/` に置く（実装仕様4.8。9.4）。
- 検出SQLと母集団のSQLは、名前付きのパラメーター `:year_from`・`:year_to`（`run_config` の `compare_years`）を受け取る。年度の組は1つだけ比べる。系列のSQLは `:year`（`series_years` の1つの年度）を受け取る。
- 実行の前に、CLIが `dataset.yaml` の `screens.<名前>.requires` の資料が両年度でそろっているかを確かめる。欠けていれば、検出SQLも母集団のSQLも実行せず `not_run` と記録する（実装仕様4.8）。
- 集約の中の `ORDER BY`（`group_concat(x, '|' ORDER BY x)`）を使うため、SQLite 3.44以上を要る。CLIは起動時に `sqlite3.sqlite_version` を確かめ、古ければ `E_FORMAT_CHANGED` ではなく `E_USAGE` で止める（環境の問題として案内する）。
- どの検出SQLも、実装仕様4.8の列（`excluded_from`・`excluded_to` を含む）を返す。SQLの中の合計は候補を選ぶためだけに使い、メモの値はCLIが `row_refs` から `excluded` を除いた行でDecimalで計算し直す。`json_array` の `null` はCLIが除く。行ごとの比較できない理由（重複、読めない日付）と年度の理由（`partial_year`）はCLIが足す。

### 9.1 `screens/s1_amount_change.sql`（金額の変化）

```sql
WITH base AS (
  SELECT * FROM procurement_contracts
  WHERE record_status = 'adopted' AND fiscal_year IN (:year_from, :year_to)
),
g AS (
  SELECT group_key, fiscal_year,
         json_group_array(record_id) AS row_refs,
         json_group_array(json_object('ref', record_id,
             'reason', CASE WHEN unit_price = 1 THEN 'unit_price'
                            WHEN amount_note = 'out_of_range' THEN 'amount_out_of_range'  -- D-092③
                            ELSE 'amount_not_numeric' END))
           FILTER (WHERE unit_price = 1 OR amount_yen IS NULL) AS excluded,
         SUM(amount_yen) FILTER (WHERE unit_price = 0 AND amount_yen IS NOT NULL) AS total,
         SUM(unit_price = 0 AND amount_yen IS NOT NULL) AS n_sum,
         SUM(dup_group IS NOT NULL) AS n_dup
  FROM base GROUP BY group_key, fiscal_year
),
p AS (
  SELECT a.group_key, a.row_refs AS rf, b.row_refs AS rt, a.excluded AS xf, b.excluded AS xt,
         a.total AS tf, b.total AS tt, a.n_sum AS sf, b.n_sum AS st, a.n_dup + b.n_dup AS nd
  FROM g a JOIN g b ON b.group_key = a.group_key
  WHERE a.fiscal_year = :year_from AND b.fiscal_year = :year_to
)
SELECT group_key AS lead_key,
       CASE WHEN sf > 0 AND st > 0 THEN (CASE WHEN tt > tf THEN 'amount_up' ELSE 'amount_down' END)
            ELSE 'amount_incomparable' END AS change_category,
       'amount_yen_sum' AS metric, :year_from AS period_from, :year_to AS period_to,
       rf AS row_refs_from, rt AS row_refs_to, xf AS excluded_from, xt AS excluded_to,
       json_array(
         CASE WHEN NOT (sf > 0 AND st > 0) THEN 'no_summable_rows_one_side' END,
         CASE WHEN json_array_length(xf) + json_array_length(xt) > 0 THEN 'excluded_unit_price_or_non_numeric' END,
         CASE WHEN nd > 0 THEN 'duplicate_rows' END) AS incomparable_reasons
FROM p
WHERE (sf > 0 AND st > 0 AND tf <> tt) OR ((sf > 0) <> (st > 0));
```

- `row_refs_*` は、候補に属するその年度の行すべてである。合計から除く行（単価契約 `unit_price`、整数の範囲外の金額 `amount_out_of_range`（4.4。D-092③）、数値にならない金額 `amount_not_numeric`）は `excluded_*` に理由と一緒に返す。CLIは `row_refs_*` から `excluded_*` を除いた行で合計し、除いた行と理由と件数を記録する（実装仕様4.8）。
- 両年度とも合計できる行があり、合計が違えば `amount_up`・`amount_down`。片方の年度だけ合計できる行がなければ `amount_incomparable` とし、CLIは件数だけを登録して、差額と増減率を作らない。
- 両方の年度とも合計できる行がない組（両年度とも単価契約など）は候補にせず、母集団の `incomparable` に数える。
- 集約の `FILTER` 句（SQLite 3.30以上）を使う。3.44以上の条件に含まれる。
- 資料の作成時に、合成データ（合計できる組、片方だけ単価契約の組、合計が同じ組、両方単価契約の組）で、除いた行が理由付きで返ること、出るべき組だけが出ることを確かめた。簡易の取り込み（単価と理由の判定を簡略にしたもの）で令和6・7年度の実データに当てたところ、SQLite 3.53で動き、S1は394件（うち `amount_incomparable` 15件）、母集団は比べた組432・比較できない組79、S2は15件、S3は14件だった【確認済み：作成時の試算。本実装の値とは一致しなくてよい】。

`populations/s1_amount_change.sql`：

```sql
WITH g AS (
  SELECT group_key, fiscal_year, SUM(unit_price = 0 AND amount_yen IS NOT NULL) AS n_sum
  FROM procurement_contracts
  WHERE record_status = 'adopted' AND fiscal_year IN (:year_from, :year_to)
  GROUP BY group_key, fiscal_year
),
p AS (
  SELECT a.n_sum AS na, b.n_sum AS nb FROM g a
  JOIN g b ON b.group_key = a.group_key
  WHERE a.fiscal_year = :year_from AND b.fiscal_year = :year_to
)
SELECT COALESCE(SUM(na > 0 AND nb > 0), 0) AS compared,
       COALESCE(SUM(NOT (na > 0 AND nb > 0)), 0) AS incomparable
FROM p;
```

### 9.2 `screens/s2_method_change.sql`（契約方式の変化）

実行の前に、CLIが `dataset.yaml` の `screens.s2_method_change.requires` に従って、両年度の競争入札と随意契約のCSVがすべて取得済みかを確かめ、欠けていれば検出SQLも母集団のSQLも実行せず、`not_run` と `partial` の理由にする（片方の資料にないことだけで方式が変わったとしないため。実装仕様4.8）。

```sql
WITH fam AS (
  SELECT group_key, fiscal_year, method_family, json_group_array(record_id) AS row_refs
  FROM procurement_contracts
  WHERE record_status = 'adopted' AND fiscal_year IN (:year_from, :year_to)
  GROUP BY group_key, fiscal_year, method_family
)
SELECT a.group_key AS lead_key,
       CASE WHEN a.method_family = 'kyoso' THEN 'kyoso_to_zuikei' ELSE 'zuikei_to_kyoso' END AS change_category,
       'method_family' AS metric, :year_from AS period_from, :year_to AS period_to,
       a.row_refs AS row_refs_from, b.row_refs AS row_refs_to,
       '[]' AS excluded_from, '[]' AS excluded_to, '[]' AS incomparable_reasons
FROM fam a
JOIN fam b ON b.group_key = a.group_key AND b.method_family <> a.method_family
WHERE a.fiscal_year = :year_from AND b.fiscal_year = :year_to
  AND NOT EXISTS (SELECT 1 FROM fam c WHERE c.group_key = a.group_key
                  AND c.fiscal_year = :year_to AND c.method_family = a.method_family)
  AND NOT EXISTS (SELECT 1 FROM fam d WHERE d.group_key = a.group_key
                  AND d.fiscal_year = :year_from AND d.method_family = b.method_family);
```

`populations/s2_method_change.sql`：両方の年度にどちらかの方式で現れる `group_key` の数を `compared`、`incomparable` は0とする。

### 9.3 `screens/s3_reason_change.sql`（随意契約の理由の変化）

```sql
WITH z AS (
  SELECT * FROM procurement_contracts
  WHERE method_family = 'zuikei' AND record_status = 'adopted' AND fiscal_year IN (:year_from, :year_to)
),
zc AS (SELECT DISTINCT group_key, fiscal_year, COALESCE(reason_category, '') AS v FROM z),
za AS (SELECT DISTINCT group_key, fiscal_year, COALESCE(reason_articles_json, '[]') AS v FROM z),
cats AS (SELECT group_key, fiscal_year, group_concat(v, '|' ORDER BY v) AS s FROM zc GROUP BY group_key, fiscal_year),
arts AS (SELECT group_key, fiscal_year, group_concat(v, '|' ORDER BY v) AS s FROM za GROUP BY group_key, fiscal_year),
rr AS (SELECT group_key, fiscal_year, json_group_array(record_id) AS row_refs FROM z GROUP BY group_key, fiscal_year)
SELECT r1.group_key AS lead_key, 'reason_changed' AS change_category, 'reason_category' AS metric,
       :year_from AS period_from, :year_to AS period_to,
       r1.row_refs AS row_refs_from, r2.row_refs AS row_refs_to,
       '[]' AS excluded_from, '[]' AS excluded_to, '[]' AS incomparable_reasons
FROM rr r1
JOIN rr r2   ON r2.group_key = r1.group_key AND r2.fiscal_year = :year_to
JOIN cats c1 ON c1.group_key = r1.group_key AND c1.fiscal_year = :year_from
JOIN cats c2 ON c2.group_key = r1.group_key AND c2.fiscal_year = :year_to
JOIN arts a1 ON a1.group_key = r1.group_key AND a1.fiscal_year = :year_from
JOIN arts a2 ON a2.group_key = r1.group_key AND a2.fiscal_year = :year_to
WHERE r1.fiscal_year = :year_from AND (c1.s <> c2.s OR a1.s <> a2.s);
```

`populations/s3_reason_change.sql`：両方の年度に随意契約で現れる `group_key` の数を `compared`、`incomparable` は0とする。

### 9.4 `series/*.sql`（年度の系列）

検出SQLと同じ名前で置き、`:year` の年度の、まとめ（`group_key`）ごとの行すべて（`row_refs`）と、合計から除く行と理由（`excluded`。書式は検出SQLの `excluded_*` と同じ）を、`lead_key`・`fiscal_year`・`row_refs`・`excluded` の4列で返す。行の選び方と除き方は検出SQLと同じにする（比べる年度と系列の年度で数え方を変えない。D-038④）。CLIは、比べる2年度で、この出力が検出SQLの `row_refs_*`・`excluded_*` と同じかを候補ごとに照らし、違えば `E_INTERNAL` で止める（実装仕様4.8）。

`series/s1_amount_change.sql`：

```sql
SELECT group_key AS lead_key, fiscal_year,
       json_group_array(record_id) AS row_refs,
       json_group_array(json_object('ref', record_id,
           'reason', CASE WHEN unit_price = 1 THEN 'unit_price'
                          WHEN amount_note = 'out_of_range' THEN 'amount_out_of_range'  -- D-092③
                          ELSE 'amount_not_numeric' END))
         FILTER (WHERE unit_price = 1 OR amount_yen IS NULL) AS excluded
FROM procurement_contracts
WHERE record_status = 'adopted' AND fiscal_year = :year
GROUP BY group_key, fiscal_year;
```

`series/s2_method_change.sql`：同じまとめの、方式を問わない行すべてを返し、`excluded` は `'[]'`。S2の候補は、比べる2年度とも1つの方式だけで現れるまとめなので、比べる2年度の行は検出SQLの `row_refs_*` と同じになる。

`series/s3_reason_change.sql`：同じまとめの随意契約（`method_family = 'zuikei'`）の行すべてを返し、`excluded` は `'[]'`。

- 合計の候補（S1の `amount_up`・`amount_down`）で、ある年度に合計できる行がない（行がない、または行がすべて単価契約・数値にならない金額）ときは、合計を作らず（0円にしない）、合計対象行の件数を登録して候補の `note` に書く。資料がそろわない年度は、系列のSQLを実行しない（実装仕様4.8）。

**検出の値（基礎集計）**：S1は年度ごとの金額の合計と合計対象行の件数、差額と増減率（`amount_incomparable` は合計対象行の件数だけ）。S2・S3は年度ごとの件数だけとし、P0では金額の合計を作らない（S2・S3の検出SQLは合計から除く行を返さないので、合計すると単価契約の金額が混じるため。D-038③）。

**令和5年度を比べない理由**：令和5年度の随意契約は理由の分類が `other` になる行が多い（4.7の規則で数えると、令和5年度286行、令和6年度11行、令和7年度1行【確認済み：資料の作成時に実データで計算】）。書式の違いによる見かけの候補が増えるため、P0では令和6・7年度だけを比べ、令和5年度は系列として読む。

## 10. `dataset.yaml`（案）

```yaml
domain: cao-procurement
parser_version: cao-procurement/2   # D-090⑤・D-091③で上げた（決定の表の D-090・D-091）
source:
  page: https://www.cao.go.jp/chotatsu/kohyo/kohyo.html
  link_pattern: 'tekiseika/(?P<reiwa>\d+)nendo/[^/]+_kohyo_(?P<family>kyoso|zuikei)\.(?P<format>csv|pdf)$'
  fiscal_year_from_reiwa: 2018
  terms_note: "利用条件はU1で確認する"
fetch:
  allowed_hosts: ["*.go.jp", "*.lg.jp"]
scope_fields:                 # 母集団の絞り込み（実装仕様6.3）に使える項目。記者の指示があるときだけ使う
  officer_unit:    {column: officer_units_json, kind: json_array}   # 契約担当官の所属（発注を求めた部局とは限らない）
  title:           {column: title_raw}                              # 件名に含む語
  method_family:   {column: method_family}                          # kyoso / zuikei
  reason_category: {column: reason_category}                        # 随意契約の理由の分類
screens:                      # compare_years の両年度で、取得と品質検査を通っていなければならない資料の種類
  s1_amount_change: {requires: [kyoso, zuikei]}   # 同じまとめが両方式にまたがるため、両方そろわないと合計が偏る
  s2_method_change: {requires: [kyoso, zuikei]}
  s3_reason_change: {requires: [zuikei]}
defaults:
  compare_years: [2024, 2025]
  series_years: [2023, 2024, 2025]
reconcile:
  source_check: pdf            # ほかの形式がない分野では not_applicable と理由
  fields_by_category:
    amount_up: [amount_yen, contract_date, title_raw]
    amount_down: [amount_yen, contract_date, title_raw]
    amount_incomparable: [amount_yen, contract_date, title_raw]
    kyoso_to_zuikei: [title_raw, contract_date, method_raw, reason_raw]
    zuikei_to_kyoso: [title_raw, contract_date, method_raw, reason_raw]
    reason_changed: [title_raw, reason_raw]
  fields_counterparty: [counterparty_raw, corp_numbers]
  pdf_fields:                  # 照合の記録のキー → take のキー。PDFの列の番号は take の index と同じ（7.3。D-002）
    amount_yen: amount_raw
    contract_date: contract_date_raw
    title_raw: title_raw
    counterparty_raw: counterparty_raw
    corp_numbers: corp_number_raw
    method_raw: method_raw
    reason_raw: reason_raw
comparison_defaults:
  s1_amount_change:
    population: 内閣府が公表した競争入札・随意契約（物品・役務等）のうち、件名のまとめのキーが両年度にある契約
    row_meaning: 1件の契約
    period: 会計年度（契約を締結した日の年度）
    unit: 円（契約金額。単価契約と数値にならない金額を除く）
    entity_mapping: 件名のまとめのキーによる同種業務の候補（確定は対応の登録）
    formula: 年度ごとの契約金額の合計の差と増減率
  s2_method_change:
    population: 内閣府が公表した競争入札・随意契約（物品・役務等）のうち、件名のまとめのキーが両年度にある契約
    row_meaning: 1件の契約
    period: 会計年度
    unit: 件
    entity_mapping: 件名のまとめのキー
    formula: 年度ごとの契約方式（競争入札・随意契約）の別
  s3_reason_change:
    population: 随意契約のうち、件名のまとめのキーが両年度にある契約
    row_meaning: 1件の契約
    period: 会計年度
    unit: 件
    entity_mapping: 件名のまとめのキー
    formula: 年度ごとの随意契約の理由の分類と根拠条文の組
display:
  円: {places: 0, thousands: true}
  比率: {places: 1, scale: 100, suffix: "%"}
  件: {places: 0, thousands: true}
group_key:
  remove_patterns:            # 上から順に適用する
    - '[（(][^）)]*年(度|版)[^）)]*[）)]'
    - '令和(\d+|元)年(度|版)'
    - '平成\d+年度'
    - '\d{4}年度'
    - 'R\d+年度'
    - '第\d+期'
    - '\d+月分'
    - '\d+か?月間'
    - '[（(][）)]'
files:
  - {fiscal_year: 2024, family: kyoso,  encoding: cp932, columns: kyoso_16}
  - {fiscal_year: 2025, family: kyoso,  encoding: cp932, columns: kyoso_16}
  - {fiscal_year: 2024, family: zuikei, encoding: cp932, columns: zuikei_17}
  - {fiscal_year: 2025, family: zuikei, encoding: cp932, columns: zuikei_27}
  - {fiscal_year: 2023, family: kyoso,  encoding: cp932, columns: kyoso_16}
  - {fiscal_year: 2023, family: zuikei, encoding: cp932, columns: zuikei_17}
  # 令和8年度（年度途中）は P0 では読まない。読む場合は3.4に従って kyoso_17_r8（pdf_columns: 17）・zuikei_28_r8（pdf_columns: 18）を書き、partial_year: true
column_sets:
  kyoso_16:
    expected_columns: 16
    pdf_columns: 16             # 同じ年度・方式のPDFの列の数（7.1。D-002）
    take: &kyoso_take
      "no":              {index: 0,  header: "NO"}
      title_raw:         {index: 1,  header: "物品役務等の名称及び数量"}
      officer_raw:       {index: 2,  header: "契約担当官等の氏名並びにその所属する部局の名称及び所在地"}
      contract_date_raw: {index: 4,  header: "契約を締結した日"}
      counterparty_raw:  {index: 5,  header: "契約の相手方の商号又は名称及び住所"}
      corp_number_raw:   {index: 7,  header: "法人番号"}
      method_raw:        {index: 8,  header: "一般競争入札・指名競争入札の別(総合評価の実施)"}
      estimate_raw:      {index: 9,  header: "予定価格"}
      amount_raw:        {index: 10, header: "契約金額"}
      rate_raw:          {index: 11, header: "落札率"}
      note_raw:          {index: 15, header: "備考"}
    drop: [3, 6, 12, 13, 14]
  zuikei_17:
    expected_columns: 17
    pdf_columns: 17
    take: &zuikei_take
      "no":              {index: 0,  header: "NO"}
      title_raw:         {index: 1,  header: "物品役務等の名称及び数量"}
      officer_raw:       {index: 2,  header: "契約担当官等の氏名並びにその所属する部局の名称及び所在地"}
      contract_date_raw: {index: 4,  header: "契約を締結した日"}
      counterparty_raw:  {index: 5,  header: "契約の相手方の商号又は名称及び住所"}
      corp_number_raw:   {index: 7,  header: "法人番号"}
      reason_raw:        {index: 8,  header: "随意契約によることとした会計法令の根拠条文及び理由(企画競争又は公募)"}
      estimate_raw:      {index: 9,  header: "予定価格"}
      amount_raw:        {index: 10, header: "契約金額"}
      rate_raw:          {index: 11, header: "落札率"}
      note_raw:          {index: 16, header: "備考"}
    drop: [3, 6, 12, 13, 14, 15]
  zuikei_27:
    expected_columns: 27
    pdf_columns: 17             # PDFには内部用の10列がない
    take: *zuikei_take
    drop: [3, 6, 12, 13, 14, 15, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26]
```

→ D-026 で改めた：`datasets/cao-procurement/dataset.yaml` には、令和8年度の2ファイル（`kyoso_17_r8`・`zuikei_28_r8`。`encoding: utf-8-sig`、`partial_year: true`）も3.4の列の対応で書いた（参考。P0 の完了条件には含めない）。上の案のコメント「令和8年度（年度途中）は P0 では読まない」は、この決定で改めた。

`take` の `header` が、別経路の読み込み処理の「期待する見出しの文字列」になる（照合の前の正規化は6章）。キーの `"no"` は引用符で囲む。囲まないと、YAMLの読み込みで真偽値の `false` になる【確認済み：PyYAMLの `safe_load`】。

資料の作成時に、この `files` と `column_sets` を令和5〜7年度の実データの見出しと照らし、6ファイルすべてで列の数と見出しが一致することを確かめた【確認済み】。

## 11. テスト用データ

- 実データの抜粋は、利用条件（U1）を確かめるまでリポジトリに入れない。
- それまでは、3章の見出しと2章の形をまねた合成のCSV（各20行程度）を `fixtures/` に作る。次を必ず含める：セル内の改行、指数表記と13桁と `-` と2つの法人番号、2人の担当官のセル、単価契約（競争の方式の欄・随意の金額の欄）、外貨と文字列の金額、NO以外が同じ行の組、`R6.4.1` 形式と `令和6年4月1日` 形式の日付、件名の年度の表記違い（`令和６年度`／`令和7年度`）、前年度は単価契約で翌年度は総額の組（S1の `amount_incomparable`）、方式が変わる組（S2）、理由の分類が変わる組（S3）、期待する見出しと違う見出しのファイル（停止の確認用）。見出しは実データと同じ文字列（全角の `ＮＯ`、全角の括弧、セル内の改行）にする。
- PDFの照合のテストは、PDFのライブラリを動かさず、罫線の四角形と文字の座標を模した合成のデータ（ページごとの `rects` と `chars`）で行う。次の罠を必ず含める：複数行のセル、セルの中だけにある横線、別のフォントで上端がずれた文字（康熙部首の「⼊」）、担当官の列に縦に1文字ずつ並んだ架空の氏名、NOが空の見出しの2行目、分かれた行（前のページから続く行を含む）、列の数が違うページ、同じNOの行が2つある場合、CSVの金額がPDFの金額の一部になる組（`1,000,000` と `11,000,000`）、長い理由の1字だけを変えた値、数値でない金額（単価の記載・外貨）、CSVの `?` とPDFの文字の差。受注者名は明らかな架空の名前にする。
- 期待値（合計、件数、照合の結果）は、テストを書く人が手で計算して書く。実装の出力から作らない（設計書7.7）。
