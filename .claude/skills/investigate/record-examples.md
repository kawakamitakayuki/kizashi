# 提案の例（inbox/ に1件ずつ書く）

例の ID（実際の ID は record が返す。例のあいだでは同じものを同じ ID で書いている）：
- 金額の候補 L0003（探索で登録。calc_from K0020・calc_to K0021）：K0022 増減率、K0023 差額、K0024・K0025 令和7・6年度の配送数量（文書値）、K0026 数量の比、K0027 数量の比で補正した令和6年度の合計（説明を反映した値）、K0028 残る差、C0006・C0007 主張。
- 理由の変化の候補 L0005（検出の候補）：K0040 根拠で説明できる行を除いた残りの行の件数（説明を反映した値と残る差）。
- 資料：S004・S006 は令和6・7年度の競争入札の CSV、S012 は仕様書、S013 は公表元の告知。

## run_config（inbox/run-config.json）
```json
{"kind": "run_config", "domain": "cao-procurement", "compare_years": [2024, 2025],
 "series_years": [2023, 2024, 2025],
 "question": "同種の業務で、契約金額・契約方式・随意契約の理由が年度間で変わったものはあるか",
 "model": "（使っているモデル名）"}
```
Claude Code の版は書かない（`scripts/investigate` が `KIZASHI_CLAUDE_VERSION` で渡す）。
記者が絞り込みを指示したときだけ、次のように scope を足す（指示がなければ書かない）。
```json
{"kind": "run_config", "domain": "cao-procurement", "compare_years": [2024, 2025],
 "series_years": [2023, 2024, 2025],
 "question": "食品安全委員会事務局の契約で、同種の業務の契約金額が年度間で変わったものはあるか",
 "scope": {"instruction": "記者の指示：食品安全委員会事務局の契約に絞る",
           "filters": [{"field": "officer_unit", "op": "contains", "value": "食品安全委員会"}]},
 "model": "（使っているモデル名）"}
```

## calc
```json
{"kind": "calc", "op": "sum", "table": "procurement_contracts", "column": "amount_yen",
 "filter": [{"col": "group_key", "op": "=", "value": "荷物等の配送業務"}, {"col": "fiscal_year", "op": "=", "value": 2025},
            {"col": "unit_price", "op": "=", "value": 0}],
 "label": "令和7年度の契約金額の合計", "unit": "円"}
```
これが K0021。令和6年度の K0020 は、`fiscal_year` だけを 2024 にして同じ形で登録する。探索の候補（lead）の `calc_from`・`calc_to` にする計算には `lead_id` を付けない。登録済みの候補の計算には `"lead_id"` を付ける。

## lead（探索で見つけた変化。先に calc で両年度の値を計算しておく）
```json
{"kind": "lead", "query_file": "q-002.sql", "lead_key": "荷物等の配送業務", "change_category": "amount_up",
 "period_from": 2024, "period_to": 2025, "calc_from": "K0020", "calc_to": "K0021", "calc_ids": [],
 "comparison": {"population": "内閣府の競争入札・随意契約のうち、件名のまとめのキーが両年度にある契約",
                "row_meaning": "1件の契約", "period": "会計年度（契約を締結した日の年度）",
                "unit": "円（単価契約を除く）", "entity_mapping": "件名のまとめのキー", "formula": "年度ごとの契約金額の合計の差"},
 "note": "検出SQLでは件名の表記の違いで別のまとめになっていた。探索で同じ業務の可能性を見つけた"}
```
登録すると候補ID（例では L0003）が返る。`calc_from`・`calc_to` は、条件にそれぞれ `{"col": "fiscal_year", "op": "=", "value": <period_from・period_to>}` を含む基礎集計。`query_file` は `kizashi query` が通った後に `accepted/` に写った探索SQLの名前。

## derive
```json
{"kind": "derive", "lead_id": "L0003", "op": "rate", "args": ["K0021", "K0020"], "label": "前年度からの増減率", "unit": "比率"}
```
演算は diff（a−b）・ratio（a÷b）・rate（(a−b)÷b）・sum（a+b+…）・product（a×b）だけ。引数は計算IDだけで、数値は書けない。資料の数値は doc_value で登録してから使う。
```json
{"kind": "derive", "lead_id": "L0003", "op": "diff", "args": ["K0021", "K0020"], "label": "前年度からの差額", "unit": "円"}
```

## doc_value
```json
{"kind": "doc_value", "lead_id": "L0003", "source_id": "S012", "position": {"page": 4},
 "quote": "配送予定数量は年間1,200件とする。", "value": "1200", "label": "令和7年度の仕様書の年間配送数量", "unit": "件"}
```
```json
{"kind": "doc_value", "lead_id": "L0003", "source_id": "S012", "position": {"page": 2},
 "quote": "令和6年度の配送予定数量は年間1,000件とした。", "value": "1000", "label": "令和6年度の年間配送数量（仕様書の記載）", "unit": "件"}
```

数値は途中で切らずに、直前の符号や `▲`・`△` も含めて全体を引用する（`1,200` の `200` だけ、`1.5` の `1.` まで、`-12件` の `12件` だけの引用は通らない）。範囲の `10-15件` の `-15件` のように、符号でない `-` から始めた引用も通らない（`範囲は10-15件。` のように引用する）。負の数は符号ごと書く（`増減は-12件。` なら `"value": "-12"`）。`▲`・`△` の付いた数値は文書値にできないので、意味を記者に確かめる。PDF の行の終わりは段落の終わりにならない。見出しや表の行のように「。」で終わらない文は、空行かページの終わりの直前まで含めないと通らない。前の行の終わりの数字と次の行の始めの数字は1つの数値として読む（「別紙２」の次の行の「２０２４年度…」は「22024」）。こうした箇所は、数値を含まず文末で終わる別の文を引用する。どの提案でも、引用・条件の値・比較の条件を含め、担当官などの氏名を書かない（氏名を含まない範囲を引用する）。

## hypothesis
```json
{"kind": "hypothesis", "lead_id": "L0003", "text": "業務の範囲が広がった可能性がある",
 "withdraw_if": "仕様書で数量・範囲が前年度と同じと分かった場合"}
```
取り下げ条件を確かめたら、同じ `text`・`withdraw_if` に `result`（満たされていなければ `kept`、満たされたら `withdrawn`）を足して登録し直す。比較成立には結果が要る（`V8_NO_WITHDRAW_RESULT`）。
```json
{"kind": "hypothesis", "lead_id": "L0003", "text": "業務の範囲が広がった可能性がある",
 "withdraw_if": "仕様書で数量・範囲が前年度と同じと分かった場合", "result": "kept"}
```

## match
```json
{"kind": "match", "lead_id": "L0003", "left": ["S004:000001"], "right": ["S006:000003"], "relation": "1:1",
 "reason": "仕様書の業務内容と配送先が同じ", "status": "confirmed",
 "evidence": [{"source_id": "S012", "position": {"page": 1}, "quote": "本業務は、…の配送を行うものである。", "role": "support"}]}
```
`left` は calc_from（K0020）の行を、`right` は calc_to（K0021）の行をすべて覆う。

## claim（observation。計算から言えること）
```json
{"kind": "claim", "lead_id": "L0003", "type": "observation",
 "text": "同種業務の契約金額の合計は、令和6年度の{calc:K0020}から令和7年度の{calc:K0021}に変わった。",
 "calc_ids": ["K0020", "K0021"],
 "evidence": [{"source_id": "S004", "position": {"row_ref": "S004:000001", "column": "amount_raw"}, "quote": "7,793,940", "role": "support"}]}
```

## claim（record。原資料の記載。fiscal_year が必須）
```json
{"kind": "claim", "lead_id": "L0003", "type": "record", "fiscal_year": 2025,
 "text": "令和7年度の公表資料では、この契約は一般競争入札として記載されている。",
 "calc_ids": [],
 "evidence": [{"source_id": "S006", "position": {"row_ref": "S006:000003", "column": "method_raw"}, "quote": "一般競争", "role": "support"}]}
```
CSVの `column` は `kizashi inspect <row_ref> --run <run_id>` に出るキー（`amount_raw` など）で書く。見出しの文字列（`契約金額`）では書かない。
根拠（`evidence`）の `role` は `support`（支える）・`oppose`（反する）・`background`（背景）の3つ。判定の `judgment`（`support`・`refute`・`unknown`・`na`）とは別の値で、`refute` は書けない。

## explanation（T1〜T5・T6c。T6a・T6bは kizashi が登録するので書かない）
未確認は `"judgment": "unknown"`、`"evidence": []` とし、`searched` に探した範囲を書く（T6c は未確認でよい）。支持の T1・T3・T4 には `scope` と、`recalc_calc_id` か `no_quant_reason` が要る。説明の状況（結論の `explanation_status`）を「全部」「一部」にするときは、支持の判定に根拠と、説明を反映した値（`recalc_calc_id`）と残る差（`residual_calc_id`）を書く。

金額の候補（L0003）：数量の比（K0026）で令和6年度の合計を補正した値（K0027）を説明を反映した値にし、候補の `calc_to`（K0021）との差（K0028）を残る差にする。
```json
{"kind": "derive", "lead_id": "L0003", "op": "ratio", "args": ["K0024", "K0025"], "label": "令和6年度に対する令和7年度の配送数量の比", "unit": "比率"}
```
```json
{"kind": "derive", "lead_id": "L0003", "op": "product", "args": ["K0020", "K0026"], "label": "配送数量の比で補正した令和6年度の契約金額の合計", "unit": "円"}
```
```json
{"kind": "derive", "lead_id": "L0003", "op": "diff", "args": ["K0021", "K0027"], "label": "数量の変化を反映した後に残る差", "unit": "円"}
```
```json
{"kind": "explanation", "lead_id": "L0003", "type": "T3", "judgment": "support", "scope": "part",
 "recalc_calc_id": "K0027", "residual_calc_id": "K0028", "searched": "令和6・7年度の仕様書、入札公告",
 "evidence": [{"source_id": "S012", "position": {"page": 4}, "quote": "配送予定数量は年間1,200件とする。", "role": "support"}]}
```
説明を反映した値は、比べる年度・系列の計算と同じ集計（表・演算・列・対象行、派生計算なら演算と引数が同じもの）にしない。残る差は、説明を反映した値と比較対象（候補の `calc_from` か `calc_to`。この例では K0021）の `diff` だけで、向きはどちらでもよく、単位は3つとも同じにする。値が同じでも、別のIDで作り直した合計を比較対象にすると通らない。

金額以外の候補（L0005。方式・理由の変化）：比べる年度の1つ（ここでは令和7年度）の候補の行から、根拠で説明できる行を除いた残りを数えた件数（K0040）を、説明を反映した値と残る差の両方にする。この例の根拠（企画競争への変更）が説明するのは企画競争の行なので、残りは企画競争によらない行。
```json
{"kind": "calc", "lead_id": "L0005", "op": "count", "table": "procurement_contracts",
 "filter": [{"col": "fiscal_year", "op": "=", "value": 2025}, {"col": "group_key", "op": "=", "value": "調査研究委託"},
            {"col": "reason_category", "op": "!=", "value": "planning_competition"}],
 "label": "令和7年度の候補の行のうち、企画競争によらない行の件数（説明できる行を除いた残り）", "unit": "件"}
```
```json
{"kind": "explanation", "lead_id": "L0005", "type": "T1", "judgment": "support", "scope": "full",
 "recalc_calc_id": "K0040", "residual_calc_id": "K0040",
 "reflected_conditions": [{"col": "reason_category", "op": "!=", "value": "planning_competition"}],
 "searched": "公表元の告知、令和6・7年度の公募と企画競争の公告",
 "evidence": [{"source_id": "S013", "position": {"page": 1}, "quote": "令和7年度から、調査研究の委託は企画競争により相手方を選ぶこととした。", "role": "support"}]}
```
`reflected_conditions` には、その残りを選ぶ条件を書く（説明できる行を選ぶ条件ではない）。条件は、その件数の条件にあり、候補の計算の条件になく、`fiscal_year`・`record_id`・`group_key`・`method_family` でないもの。その年度の候補の行に `reflected_conditions` だけを当てた行が、件数の行と一致しなければならない。件数の行は、その年度の候補の行の一部でなければならない。0件、件数が変わらない、一部だけ残る、のどれでも check は通るが、件数から説明の状況は決まらない（0件でも「全部」と決めつけず、残りを選ぶ条件が根拠の説明する範囲と合うかを記者に確かめてもらう）。条件の列が空の行（競争入札の行の `reason_category` など）は、`is_null` 以外の条件（`!=` を含む）に当たらず残りから外れ、説明できる行として扱われてしまう。そうなる条件（`=`・`!=`・`in`・`not_null`）は、check が判定ごとに `V8_EXPLANATION_STATUS` で拒否する（別の説明が合格していても）。拒否されないのは、その行が `reflected_conditions` に宣言した説明用の条件（`fiscal_year`・`record_id`・`group_key`・`method_family` でないもの）で、空でない値によって外れる場合だけ。集計にだけ足して宣言しない条件では外せない。空の行を残りに残すなら `is_null` を使う。定量化できなければ残りの件数を使わず、`scope` を `unknown` にして `no_quant_reason` と、目的が `reason_breakdown` の質問で扱う。

## question
```json
{"kind": "question", "lead_id": "L0003", "to": "所管部署", "question": "令和7年度に配送の数量や範囲を変えた資料はあるか",
 "documents": "令和6・7年度の仕様書と変更契約書", "decision_impact": "数量の変更で差の大半が説明できれば、説明の状況を「全部」に改める",
 "purposes": ["reason_breakdown", "missing_documents"], "foia": "変更契約書"}
```
`purposes` は質問の目的（1つ以上）：`verify_record`（元の記載の確認。比較成立に必須）、`reason_breakdown`（理由・内訳の確認。説明の状況が「範囲不明」なら必須）、`missing_documents`（不足資料の照会。保留に必須）、`other`。比較成立では、元の記載を確かめる質問（T6c）を登録する。
```json
{"kind": "question", "lead_id": "L0003", "to": "所管部署", "question": "公表資料にある令和7年度のこの契約の契約金額と契約方式は、契約書の記載と同じか",
 "documents": "令和7年度の契約書", "decision_impact": "記載が違えば、比較成立を取り消し、元の記載の誤り（T6c）を検討する",
 "purposes": ["verify_record"]}
```

## coefficient（係数の提案。計算には使われない。値は value にだけ書く）
```json
{"kind": "coefficient", "lead_id": "L0003", "key": "tax_basis", "value": "1.1",
 "meaning": "税抜きの額を税込みに換算する係数",
 "why_needed": "令和6年度の公表は税抜き、令和7年度は税込みと注記されており、そのままでは比べられない",
 "apply_to": "令和6年度の合計（K0020）に掛けて、令和7年度の合計と比べる",
 "basis": [{"file": "playbook", "locator": "C024", "quote": "④契約金額を税込か確認"},
           {"file": "playbook", "locator": "M10", "quote": "報告基準額、税込・税抜、訂正報告、立替、返金を確認する。"}],
 "status": "proposed"}
```
記者の判断を聞いたら、同じ key で status を accepted（使うと判断。P0では計算に使わない）か declined（不要）にし、reporter_note に記者の判断の要約を書く。proposed・accepted のまま残る候補は比較成立にできない（`V8_COEFFICIENT_PENDING`）。

## conclusion
```json
{"kind": "conclusion", "lead_id": "L0003", "outcome": "change", "comparison_state": "comparable",
 "explanation_status": "part"}
```
`outcome` は `change`（比較成立）・`artifact`（見かけの変化）・`hold`（保留）、`comparison_state` は `comparable`・`adjusted`・`unresolved`・`not_applicable`。保留は `"hold_reason"`（`comparison`・`source_check`・`limit`）が要り、任意で `"reopen_condition"`（再開の条件）を書く。補正した（`adjusted`）なら `"adjustment": {"method": "補正の方法", "before": "<補正前の計算ID>", "after": "<補正後の計算ID>"}` を付ける。比較の条件を既定から変えるときだけ `comparison` に変える項目を書く。

## withdraw
```json
{"kind": "withdraw", "target": "C0007", "reason": "引用した資料が別の年度のものだった"}
```
`target` は主張ID・対応ID・計算ID（record で作った計算だけ。run が作った計算は取り下げられない）。仮説の取り下げは withdraw ではなく、hypothesis の `result`。
