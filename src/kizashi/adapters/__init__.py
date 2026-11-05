"""分野アダプタの読み込み（implementation_spec.md 2章の adapters/__init__.py）。

`datasets/<分野>/dataset.yaml` を読み、分野ごとの正規化のモジュールと組にして返す。
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import yaml

from kizashi import paths
from kizashi.errors import KizashiError
from kizashi.hashing import sha256_bytes

# 分野の名前 → 正規化のモジュール。
MODULES: dict[str, str] = {
    "cao-procurement": "kizashi.adapters.cao_procurement",
}

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FAMILIES = ("kyoso", "zuikei")


@dataclass(frozen=True)
class FileSpec:
    """dataset.yaml の files の1件と、その列の対応（column_sets）を合わせたもの。"""

    fiscal_year: int
    family: str
    encoding: str
    columns: str
    expected_columns: int
    pdf_columns: int
    take: dict[str, dict]
    drop: tuple[int, ...]
    partial_year: bool = False

    def index_of(self, key: str) -> int | None:
        item = self.take.get(key)
        return None if item is None else int(item["index"])


@dataclass(frozen=True)
class Adapter:
    domain: str
    directory: Path
    config: dict
    yaml_sha256: str
    module: ModuleType
    files: dict[tuple[int, str], FileSpec] = field(default_factory=dict)

    @property
    def parser_version(self) -> str:
        return str(self.config["parser_version"])

    def file_spec(self, fiscal_year: int, family: str) -> FileSpec | None:
        return self.files.get((fiscal_year, family))

    def logical_key(self, fiscal_year: int, family: str, fmt: str) -> str:
        """論理資料のキー（アダプタ仕様1章：`cao-procurement|<年度>|<kyoso|zuikei>|<csv|pdf>`）。"""
        return f"{self.domain}|{fiscal_year}|{family}|{fmt}"


def _bad_yaml(domain: str, why: str) -> KizashiError:
    return KizashiError(
        "E_INTERNAL",
        "cli",
        f"datasets/{domain}/dataset.yaml の形が誤っている：{why}",
        next_action="開発側で dataset.yaml を直す。",
    )


def _file_specs(domain: str, config: dict) -> dict[tuple[int, str], FileSpec]:
    column_sets = config.get("column_sets") or {}
    specs: dict[tuple[int, str], FileSpec] = {}
    for entry in config.get("files") or []:
        cs = column_sets.get(entry.get("columns"))
        if cs is None:
            raise _bad_yaml(domain, f"column_sets に {entry.get('columns')!r} がない")
        take = cs.get("take") or {}
        if any(not isinstance(k, str) for k in take):
            # 引用符のない no は YAML で真偽値になる（アダプタ仕様10章）。
            raise _bad_yaml(domain, 'take のキーが文字列でない（"no" を引用符で囲む）')
        if entry.get("family") not in FAMILIES:
            raise _bad_yaml(domain, f"family が {FAMILIES} のどれでもない：{entry.get('family')!r}")
        key = (int(entry["fiscal_year"]), str(entry["family"]))
        if key in specs:
            raise _bad_yaml(domain, f"files に同じ年度・方式が2つある：{key}")
        specs[key] = FileSpec(
            fiscal_year=key[0],
            family=key[1],
            encoding=str(entry["encoding"]),
            columns=str(entry["columns"]),
            expected_columns=int(cs["expected_columns"]),
            pdf_columns=int(cs["pdf_columns"]),
            take={str(k): dict(v) for k, v in take.items()},
            drop=tuple(int(i) for i in cs.get("drop") or ()),
            partial_year=bool(entry.get("partial_year", False)),
        )
    return specs


def load_adapter(root: Path, domain: str) -> Adapter:
    """分野アダプタを読み込む。知らない分野は E_USAGE。"""
    if not _DOMAIN_RE.fullmatch(domain or "") or domain not in MODULES:
        raise KizashiError(
            "E_USAGE",
            "run",
            f"知らない分野：{domain!r}",
            next_action=f"分野は {', '.join(sorted(MODULES))} のどれかを指定する。",
        )
    directory = paths.dataset_dir(root, domain)
    yaml_path = directory / "dataset.yaml"
    if not yaml_path.is_file():
        raise KizashiError(
            "E_USAGE",
            "run",
            f"分野の設定がない：datasets/{domain}/dataset.yaml",
            next_action="リポジトリの直下で実行しているか、分野の名前を確かめる。",
        )
    raw = yaml_path.read_bytes()
    config = yaml.safe_load(raw)
    if not isinstance(config, dict) or config.get("domain") != domain:
        raise _bad_yaml(domain, "domain が分野の名前と違う")
    module = importlib.import_module(MODULES[domain])
    return Adapter(
        domain=domain,
        directory=directory,
        config=config,
        yaml_sha256=sha256_bytes(raw),
        module=module,
        files=_file_specs(domain, config),
    )
