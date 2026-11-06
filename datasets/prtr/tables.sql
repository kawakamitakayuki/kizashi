-- PRTR の型付きの表と対応表（骨組み。P1-c。docs/adapter_prtr.md 10章）。
-- 共通の8表（src/kizashi/schema.sql の records・leads など）の後に作る。量は原単位のままの十進数の文字列で持ち、
-- 合計は Decimal で行う（浮動小数点にしない）。

-- 本紙：1行が1事業所・1年度
CREATE TABLE prtr_facilities (
  record_id TEXT PRIMARY KEY REFERENCES records(record_id),
  record_status TEXT NOT NULL CHECK (record_status IN ('adopted','held','excluded')),
  fiscal_year INTEGER NOT NULL,          -- 排出年度（本紙2列）
  seiri_no TEXT NOT NULL,                -- 整理番号（年度の中だけで一意。年度をまたいで結ばない）
  corp_number TEXT,                      -- 事業者の法人番号（13桁。空欄は NULL）
  pref TEXT NOT NULL,                    -- 事業所の所在地：都道府県
  city TEXT NOT NULL,                    -- 事業所の所在地：市区町村
  industry_code TEXT NOT NULL,           -- 主たる業種コード
  industry_name TEXT,
  substance_count INTEGER NOT NULL,      -- 届出物質数
  UNIQUE (fiscal_year, seiri_no)
);

-- 別紙：1行が1事業所・1物質・1年度
CREATE TABLE prtr_releases (
  record_id TEXT PRIMARY KEY REFERENCES records(record_id),
  record_status TEXT NOT NULL CHECK (record_status IN ('adopted','held','excluded')),
  fiscal_year INTEGER NOT NULL,
  seiri_no TEXT NOT NULL,
  facility_record_id TEXT REFERENCES prtr_facilities(record_id),   -- 年度＋整理番号で結んだ本紙の行
  sheet_no INTEGER NOT NULL,
  substance_code TEXT NOT NULL,          -- 管理番号（対応化学物質のコード番号）
  substance_name TEXT NOT NULL,
  unit TEXT NOT NULL CHECK (unit IN ('kg','mg-TEQ')),
  air TEXT,                              -- 排出量：大気
  water TEXT,                            -- 排出量：公共用水域
  soil TEXT,                             -- 排出量：土壌
  landfill TEXT,                         -- 排出量：埋立
  sewer TEXT,                            -- 移動量：下水道（排出と合算しない）
  offsite TEXT,                          -- 移動量：事業所の外（排出と合算しない）
  incomparable_reasons_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE (fiscal_year, seiri_no, substance_code)
);

-- 年度間の事業所の対応表（内閣府の procurement_matches と同じ列）。キーで結べない事業所を、記者と Claude が
-- 資料で確かめて結ぶ。left_json・right_json は本紙の record_id の配列
CREATE TABLE prtr_facility_matches (
  match_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL REFERENCES leads(lead_id),
  left_json TEXT NOT NULL,
  right_json TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('1:1','1:n','n:1','n:m')),
  reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK (status IN ('confirmed','pending','withdrawn')),
  created_at TEXT NOT NULL
);
