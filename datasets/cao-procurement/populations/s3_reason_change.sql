-- S3 の母集団（アダプタ仕様9.3）。両方の年度に随意契約で現れる group_key の数を compared、
-- incomparable は0とする。
WITH y AS (
  SELECT DISTINCT group_key, fiscal_year FROM procurement_contracts
  WHERE method_family = 'zuikei' AND record_status = 'adopted' AND fiscal_year IN (:year_from, :year_to)
)
SELECT COUNT(*) AS compared, 0 AS incomparable
FROM y a JOIN y b ON b.group_key = a.group_key
WHERE a.fiscal_year = :year_from AND b.fiscal_year = :year_to;
