-- S2 契約方式の変化（アダプタ仕様9.2）。CLI は実行の前に、両年度の競争入札と随意契約の CSV が
-- そろっているか（dataset.yaml の screens.s2_method_change.requires）を確かめる。
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
