-- 台帳のDDL（implementation_spec.md 5章の8つの表と、adapter_cao_procurement.md 8章の分野の表）。
-- 外部キーで参照し合うため、表は最初からすべて作る。接続のたびに PRAGMA foreign_keys = ON と
-- PRAGMA journal_mode = WAL を設定する（ledger.py）。

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  config_json TEXT NOT NULL,
  profile TEXT,
  state TEXT NOT NULL CHECK (state IN ('running','completed','partial','failed')),
  stop_reason TEXT,
  stages_json TEXT NOT NULL DEFAULT '{}',
  rule_versions_json TEXT NOT NULL,
  knowledge_hashes_json TEXT,
  counts_json TEXT NOT NULL DEFAULT '{}',
  limits_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE sources (
  source_id TEXT PRIMARY KEY,
  logical_key TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('dataset','evidence','definition','index')),
  url TEXT NOT NULL,
  final_url TEXT,
  referrer_url TEXT,
  fetched_at TEXT,
  sha256 TEXT,
  supersedes TEXT REFERENCES sources(source_id),
  media_type TEXT,
  encoding TEXT,
  size_bytes INTEGER,
  page_count INTEGER,
  status TEXT NOT NULL CHECK (status IN ('planned','fetched','not_published','failed','out_of_scope')),
  failure_reason TEXT,
  published_date TEXT,
  period TEXT,
  terms_note TEXT
);

CREATE TABLE records (
  record_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  logical_no INTEGER NOT NULL,
  cells_json TEXT NOT NULL,
  cells_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('adopted','held','excluded')),
  status_reason TEXT,
  parser_version TEXT NOT NULL,
  UNIQUE (source_id, logical_no)
);

CREATE TABLE leads (
  lead_id TEXT PRIMARY KEY,
  screen_id TEXT NOT NULL,
  query_file TEXT,
  lead_key TEXT NOT NULL,
  change_category TEXT NOT NULL,
  metric TEXT,
  period_from TEXT,
  period_to TEXT,
  calc_from TEXT,
  calc_to TEXT,
  calc_ids_json TEXT NOT NULL DEFAULT '[]',
  peer_count INTEGER,
  comparison_json TEXT NOT NULL DEFAULT '{}',
  note TEXT,
  comparison_state TEXT CHECK (comparison_state IN ('comparable','adjusted','unresolved','not_applicable')),
  adjustment_json TEXT,
  incomparable_reasons_json TEXT NOT NULL DEFAULT '[]',
  hypothesis TEXT,
  withdraw_if TEXT,
  hypothesis_result TEXT CHECK (hypothesis_result IN ('kept','withdrawn')),
  questions_json TEXT NOT NULL DEFAULT '[]',
  coefficients_json TEXT NOT NULL DEFAULT '[]',
  proposed_outcome TEXT CHECK (proposed_outcome IN ('change','artifact','hold')),
  explanation_status TEXT CHECK (explanation_status IN ('none','part','full','unknown')),
  hold_reason TEXT CHECK (hold_reason IN ('comparison','source_check','limit')),
  state TEXT NOT NULL DEFAULT 'candidate' CHECK (state IN ('candidate','investigating','change','artifact','hold')),
  reopen_condition TEXT,
  rounds INTEGER NOT NULL DEFAULT 0,
  memo_no INTEGER NOT NULL DEFAULT 0,
  memo_hash TEXT,
  UNIQUE (screen_id, lead_key, change_category, period_from, period_to)
);

CREATE TABLE computations (
  calc_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('base','doc_value','derived','reconcile')),
  lead_id TEXT REFERENCES leads(lead_id),
  request_json TEXT NOT NULL,
  sql_text TEXT,
  sql_sha256 TEXT,
  formula TEXT,
  ref_calc_ids_json TEXT,
  value TEXT,
  unit TEXT,
  meaning TEXT NOT NULL,
  row_refs_json TEXT,
  row_refs_sha256 TEXT,
  row_count INTEGER,
  result_json TEXT,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','withdrawn')),
  origin TEXT NOT NULL CHECK (origin IN ('run','record')),
  created_at TEXT NOT NULL
);

CREATE TABLE claims (
  claim_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL REFERENCES leads(lead_id),
  type TEXT NOT NULL CHECK (type IN ('record','observation','publisher_explanation','hypothesis')),
  fiscal_year INTEGER,
  text TEXT NOT NULL,
  calc_ids_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','checked','held','withdrawn','stale')),
  replaces TEXT REFERENCES claims(claim_id),
  created_at TEXT NOT NULL,
  CHECK (type <> 'record' OR fiscal_year IS NOT NULL)
);

CREATE TABLE explanations (
  lead_id TEXT NOT NULL REFERENCES leads(lead_id),
  type TEXT NOT NULL CHECK (type IN ('T1','T2','T3','T4','T5','T6a','T6b','T6c')),
  judgment TEXT NOT NULL CHECK (judgment IN ('support','refute','unknown','na')),
  evidence_json TEXT NOT NULL DEFAULT '[]',
  searched TEXT,
  recalc_calc_id TEXT REFERENCES computations(calc_id),
  residual_calc_id TEXT REFERENCES computations(calc_id),
  reflected_conditions_json TEXT,
  scope TEXT CHECK (scope IN ('full','part','unknown')),
  no_quant_reason TEXT,
  context TEXT NOT NULL DEFAULT 'main_session',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (lead_id, type)
);

CREATE TABLE events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('state_change','record','query','check','render','correction','recheck','resume','stage')),
  before_json TEXT,
  after_json TEXT,
  reason TEXT,
  result_json TEXT,
  check_hash TEXT,
  at TEXT NOT NULL
);

-- 分野の表（adapter_cao_procurement.md 8章）

CREATE TABLE procurement_contracts (
  record_id TEXT PRIMARY KEY REFERENCES records(record_id),
  record_status TEXT NOT NULL CHECK (record_status IN ('adopted','held','excluded')),
  fiscal_year INTEGER NOT NULL,
  partial_year INTEGER NOT NULL DEFAULT 0,
  method_family TEXT NOT NULL CHECK (method_family IN ('kyoso','zuikei')),
  no INTEGER NOT NULL,
  title_raw TEXT NOT NULL,
  group_key TEXT NOT NULL,
  title_removed_parts TEXT,
  officer_units_json TEXT NOT NULL DEFAULT '[]',
  officer_count INTEGER NOT NULL DEFAULT 1,
  contract_date TEXT,
  counterparty_raw TEXT,
  corp_numbers_json TEXT NOT NULL DEFAULT '[]',
  corp_number_state TEXT NOT NULL CHECK (corp_number_state IN ('digits13','exponent','missing','multiple','invalid')),
  method_raw TEXT,
  method_detail_json TEXT,
  unit_price INTEGER NOT NULL CHECK (unit_price IN (0,1)),
  reason_raw TEXT,
  reason_category TEXT,
  reason_articles_json TEXT,
  amount_raw TEXT,
  amount_yen INTEGER,
  amount_note TEXT,
  estimate_raw TEXT,
  rate_raw TEXT,
  note_raw TEXT,
  dup_group TEXT,
  incomparable_reasons_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE procurement_matches (
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
