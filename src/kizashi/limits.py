"""上限の定数（implementation_spec.md 3.6。設計書4.3の値）。"""

from __future__ import annotations

MAX_DEEP_LEADS = 3
# --index・--definition・--recheck と初期データは数えない。
MAX_EVIDENCE_FETCH = 12
# すべての通信を数える（リダイレクトの各段と再試行を含む）。
MAX_HTTP_REQUESTS = 40
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_RUN_BYTES = 100 * 1024 * 1024
# 一時的な通信の失敗だけを再試行する。
MAX_RETRIES = 2
MAX_ROUNDS = 2
QUERY_ROW_LIMIT = 50
QUERY_TIMEOUT_SEC = 10

# 4.2：リダイレクトは自動で追わず、1段ずつ許可リストで検査する（最大5段）。
MAX_REDIRECTS = 5
