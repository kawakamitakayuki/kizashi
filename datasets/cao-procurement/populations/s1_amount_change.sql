-- S1 の母集団（アダプタ仕様9.1）。両年度にある group_key のうち、両年度とも合計できる行がある組を
-- compared、片方か両方の年度に合計できる行がない組を incomparable に数える。
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
