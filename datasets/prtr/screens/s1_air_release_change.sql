-- S1 大気への排出量の変化（PRTR。骨組み。docs/adapter_prtr.md 11章）。CLI の検出にはまだ組み込んでいない。
-- :year_from・:year_to は比べる2年度、:pref・:substance_code は dataset.yaml の first_scope（記者が先に決める）。
-- 事業所の対応のキーは、法人番号・都道府県・市区町村・主たる業種コード（整理番号では結ばない。6章）。
-- キーに当たる事業所が両年度ともちょうど1つのものだけを比べる。大小の比較は候補を選ぶためだけで、
-- メモの値は CLI が row_refs から excluded を除いた行で Decimal で計算し直す（実装仕様4.8）。
WITH f AS (
  SELECT record_id, fiscal_year,
         corp_number || '|' || pref || '|' || city || '|' || industry_code AS fkey
  FROM prtr_facilities
  WHERE record_status = 'adopted' AND fiscal_year IN (:year_from, :year_to)
    AND corp_number IS NOT NULL AND pref = :pref
),
uniq AS (
  SELECT fkey FROM f GROUP BY fkey
  HAVING SUM(fiscal_year = :year_from) = 1 AND SUM(fiscal_year = :year_to) = 1
),
r AS (
  SELECT x.record_id, x.fiscal_year, x.air,
         f.fkey || '|' || x.substance_code || '|' || x.unit AS lead_key
  FROM prtr_releases x JOIN f ON f.record_id = x.facility_record_id
  WHERE x.record_status = 'adopted' AND x.substance_code = :substance_code
    AND f.fkey IN (SELECT fkey FROM uniq)
),
p AS (
  SELECT a.lead_key, a.record_id AS rf, b.record_id AS rt,
         NULLIF(a.air, '') AS af, NULLIF(b.air, '') AS at
  FROM r a JOIN r b ON b.lead_key = a.lead_key
  WHERE a.fiscal_year = :year_from AND b.fiscal_year = :year_to
)
SELECT lead_key,
       CASE WHEN af IS NULL OR at IS NULL THEN 'air_release_incomparable'
            WHEN CAST(at AS REAL) > CAST(af AS REAL) THEN 'air_release_up'
            ELSE 'air_release_down' END AS change_category,
       'air_sum' AS metric, :year_from AS period_from, :year_to AS period_to,
       json_array(rf) AS row_refs_from, json_array(rt) AS row_refs_to,
       CASE WHEN af IS NULL THEN json_array(json_object('ref', rf, 'reason', 'air_missing'))
            ELSE json_array() END AS excluded_from,
       CASE WHEN at IS NULL THEN json_array(json_object('ref', rt, 'reason', 'air_missing'))
            ELSE json_array() END AS excluded_to,
       json_array(
         CASE WHEN af IS NULL OR at IS NULL THEN 'air_missing_one_side' END,
         -- 対象物質の見直し（令和5年度の把握から。docs/adapter_prtr.md 5章）をまたぐ比較
         CASE WHEN :year_from < 2023 AND :year_to >= 2023 THEN 'substance_list_revision' END
       ) AS incomparable_reasons
FROM p
WHERE (af IS NULL) <> (at IS NULL)
   OR (af IS NOT NULL AND at IS NOT NULL AND CAST(af AS REAL) <> CAST(at AS REAL));
