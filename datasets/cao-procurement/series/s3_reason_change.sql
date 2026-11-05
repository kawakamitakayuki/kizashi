-- S3 の年度の系列（アダプタ仕様9.4）。:year の年度の、まとめ（group_key）ごとの随意契約の行すべてを返す。
-- 合計から除く行はない（screens/s3_reason_change.sql と同じ。CLI が比べる2年度で照らす）。
SELECT group_key AS lead_key, fiscal_year,
       json_group_array(record_id) AS row_refs,
       '[]' AS excluded
FROM procurement_contracts
WHERE method_family = 'zuikei' AND record_status = 'adopted' AND fiscal_year = :year
GROUP BY group_key, fiscal_year;
