-- S1 金額の変化（アダプタ仕様9.1）。:year_from・:year_to は run_config の compare_years。
-- SQL の中の合計は候補を選ぶためだけに使う。メモの値は CLI が row_refs から excluded を除いた行で
-- Decimal で計算し直す（実装仕様4.8）。
WITH base AS (
  SELECT * FROM procurement_contracts
  WHERE record_status = 'adopted' AND fiscal_year IN (:year_from, :year_to)
),
g AS (
  SELECT group_key, fiscal_year,
         json_group_array(record_id) AS row_refs,
         json_group_array(json_object('ref', record_id,
             'reason', CASE WHEN unit_price = 1 THEN 'unit_price' ELSE 'amount_not_numeric' END))
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
