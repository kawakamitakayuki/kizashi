"""正規化したJSONとハッシュ（implementation_spec.md 11章の2）。

検証記録のハッシュ（check_hash）は P0-6 で足す。P0-2 では、records.cells_sha256 などに使う
正規化したJSONと SHA-256 だけを置く。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_json(obj: object) -> str:
    """キーを並べ替え、区切りの空白をなくしたJSON（11章の2）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
