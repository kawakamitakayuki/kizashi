# 見逃しの点検（設計書7.7、test_plan.md 4章）

- 状態：未実施（ひな形。点検は利用者が行う。時間が足りなければ「未実施」のまま残す。設計書7.5の削る順①）
- 目的：検出が候補に選ばなかったグループを、人が原本で確かめる。選ばれた候補だけを確かめても、見逃しは測れない。
- 件数：5件程度。件数が少ないので、見逃しの割合を数字で宣伝しない。

## 選び方

1. 選ぶ方法（無作為の方法、乱数の種、何の一覧から選ぶか）を、一覧を見る前にここに書く：未記入
2. 選ばれなかったグループの一覧は、読み取り専用の SQL を `inbox/` に書き、`kizashi query <run_id> inbox/<名前>.sql` で出す（50行まで。`LIMIT` と `OFFSET` で続きを読む）。S1（契約金額の変化）の例（令和6・7年度。年度は実行の比べる2年度に合わせる）：

```sql
-- 両年度に採用の行がある group_key のうち、S1 の候補になっていないもの
SELECT p.group_key
FROM (SELECT group_key FROM procurement_contracts
      WHERE record_status = 'adopted' AND fiscal_year IN (2024, 2025)
      GROUP BY group_key HAVING COUNT(DISTINCT fiscal_year) = 2) AS p
WHERE p.group_key NOT IN (SELECT lead_key FROM leads WHERE screen_id = 's1_amount_change')
ORDER BY p.group_key
LIMIT 50 OFFSET 0
```

この例は、両年度に採用の行があるグループを数える。S1 が比べた組（`populations/s1_amount_change.sql`）は、さらに両年度とも合計できる行（単価契約でなく、金額が数値になる行）がある組に限るので、この一覧と比べた組の数は一致しない。S2（方式の変化）・S3（理由の変化）は、`screen_id` と、`datasets/cao-procurement/populations/` の同じ名前の SQL の条件に合わせて書き直す。

3. 選んだグループの行は `kizashi inspect <row_ref> --run <run_id>` で読み、原本（`data/raw/<sha256>.csv` と PDF）で確かめる。

## 記録

| # | 実行ID | 検出SQL | グループ（`group_key`） | 原本で確かめた結果（変化の有無と中身） | 候補にすべきだったか | 理由（検出の条件、比較できない理由など） | 確かめた人・日 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 |
| 2 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 |
| 3 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 |
| 4 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 |
| 5 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 | 未記入 |
