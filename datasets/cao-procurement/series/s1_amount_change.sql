-- S1 の年度の系列（アダプタ仕様9.4）。:year の年度の、まとめ（group_key）ごとの行すべてと、合計から除く行と
-- 理由を返す。行の選び方と除き方は screens/s1_amount_change.sql と同じにする（CLI は比べる2年度で、
-- この出力と検出SQLの row_refs・excluded が同じかを照らす。実装仕様4.8）。
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
