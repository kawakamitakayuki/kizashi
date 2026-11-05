-- S2 の年度の系列（アダプタ仕様9.4）。:year の年度の、まとめ（group_key）ごとの行すべて（方式を問わない）を
-- 返す。合計から除く行はない（screens/s2_method_change.sql と同じ）。S2 の候補は、比べる2年度とも1つの方式
-- だけで現れるまとめなので、比べる2年度の行は検出SQLの row_refs と同じになる（CLI が照らす）。
SELECT group_key AS lead_key, fiscal_year,
       json_group_array(record_id) AS row_refs,
       '[]' AS excluded
FROM procurement_contracts
WHERE record_status = 'adopted' AND fiscal_year = :year
GROUP BY group_key, fiscal_year;
