-- S3 随意契約の理由の変化（アダプタ仕様9.3）。集約の中の ORDER BY のため SQLite 3.44 以上が要る。
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
