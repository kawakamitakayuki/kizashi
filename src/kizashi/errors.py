"""型付きのエラーと終了コード（implementation_spec.md 3.2・3.7・9章）。"""

from __future__ import annotations

from collections.abc import Iterable

# 3.7 の表（検証のコード以外）。
_EXIT_CODES_3_7: dict[str, int] = {
    "E_CWD": 3,
    "E_BAD_ID": 2,
    "E_USAGE": 2,
    "E_INBOX_PATH": 1,
    "E_SCHEMA": 1,
    "E_UNKNOWN_KIND": 1,
    "E_SELF_DECLARED": 1,
    "E_REF_NOT_FOUND": 1,
    "E_QUOTE_NOT_FOUND": 1,
    "E_QUOTE_TRUNCATED": 1,
    "E_QUOTE_YEAR": 1,
    "E_BAD_EVIDENCE_SOURCE": 1,
    "E_DOC_VALUE_MISMATCH": 1,
    "E_BAD_EXPR": 1,
    "E_DIV_ZERO": 1,
    "E_NOT_ALLOWED_COLUMN": 1,
    "E_V7": 1,
    "E_LIMIT_LEADS": 1,
    "E_LIMIT_EVIDENCE": 1,
    "E_LIMIT_HTTP": 1,
    "E_LIMIT_BYTES": 1,
    "E_ROUNDS_EXCEEDED": 1,
    "E_HOST_NOT_ALLOWED": 1,
    "E_REDIRECT_NOT_ALLOWED": 1,
    "E_FETCH_FAILED": 4,
    "E_FETCH_INVALID": 1,
    "E_FORMAT_CHANGED": 3,
    "E_QUERY_REJECTED": 1,
    "E_QUERY_TIMEOUT": 1,
    "E_NOT_RESUMABLE": 3,
    "E_INPUT_CHANGED": 3,
    "E_RULE_VERSION": 3,
    "E_DENYLIST_MISSING": 3,
    "E_INTERNAL": 5,
}

# 9章の検証のコード。検査の不合格は終了コード1（3.2）。
# 9章の V2 の行にある E_RULE_VERSION は、3.7 のとおり終了コード3（上の表）。
# W7_STRONG_WORD は警告であり、エラーのコードではない。
_CHECK_CODES_9: tuple[str, ...] = (
    "V1_INPUT_EMPTY",
    "V1_QUALITY_NOT_RUN",
    "V1_RUN_FAILED",
    "V2_REF_MISSING",
    "V2_HASH_MISMATCH",
    "V2_STALE_OR_WITHDRAWN",
    "V2_BAD_EVIDENCE_SOURCE",
    "V3A_RECALC_MISMATCH",
    "V3B_REAGG_MISMATCH",
    "V3B_HEADER_UNRESOLVED",
    "V3C_DERIVE_MISMATCH",
    "V3_UNCITED_NUMBER",
    "V3_NO_MEANING",
    "V4_QUOTE_NOT_FOUND",
    "V4_QUOTE_TRUNCATED",
    "V4_QUOTE_YEAR",
    "V5_CONDITION_MISSING",
    "V5_UNRESOLVED_COMPARISON",
    "V5_ADJUSTED_NO_VALUES",
    "V5_BAD_RATE",
    "V5_PARTIAL_YEAR",
    "V6_TYPE_MISSING",
    "V6_NO_EVIDENCE",
    "V6_SELF_EVIDENCE",
    "V6_NO_SEARCH_SCOPE",
    "V7_RAW_NUMBER",
    "V7_PERSONAL_NAME",
    "V8_REQUIRED_MISSING",
    "V8_MATCH_UNCONFIRMED",
    "V8_COEFFICIENT_PENDING",
    "V8_CHANGE_NOT_NEGATED",
    "V8_ARTIFACT_NO_FACTOR",
    "V8_EXPLANATION_STATUS",
    "V8_NO_WITHDRAW_RESULT",
    "V8_HOLD_REASON",
    "V8_T6C_QUESTION",
)

# 12.2 の check --publish の P0 の4項目の不合格（終了コード1）。仕様はコードの名前を決めていない（D-053）。
_PUBLISH_CODES_12: tuple[str, ...] = (
    "P_MANIFEST_MISMATCH",
    "P_DENYLIST_HASH",
    "P_DENYLIST_NAME",
    "P_PERSONAL_NAME",
    "P_EXCLUDED_VALUE",
    "P_COUNTERPARTY",
    "P_CORP_NUMBER",
)

EXIT_CODES: dict[str, int] = {
    **_EXIT_CODES_3_7,
    **{c: 1 for c in _CHECK_CODES_9},
    **{c: 1 for c in _PUBLISH_CODES_12},
}

EXIT_OK = 0


def exit_code_for(code: str) -> int:
    """エラーコードの終了コード。表にないコードは書き損じとして ValueError にする。"""
    try:
        return EXIT_CODES[code]
    except KeyError:
        raise ValueError(f"未知のエラーコード: {code}") from None


class KizashiError(Exception):
    """CLI が JSON で返す型付きのエラー（3.2）。"""

    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        affected_ids: Iterable[str] = (),
        retryable: bool = False,
        next_action: str = "",
        failures: Iterable[dict] | None = None,
    ) -> None:
        self.exit_code = exit_code_for(code)
        super().__init__(f"{code}: {message}")
        self.code = code
        self.stage = stage
        self.message = message
        self.affected_ids = tuple(affected_ids)
        self.retryable = bool(retryable)
        self.next_action = next_action
        # check の不合格は、すべてを集めて返す（4.6）。code は最初の不合格のコード
        self.failures = None if failures is None else [dict(f) for f in failures]

    def to_dict(self) -> dict:
        data = {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "affected_ids": list(self.affected_ids),
            "retryable": self.retryable,
            "next_action": self.next_action,
        }
        if self.failures is not None:
            data["failures"] = self.failures
        return data
