"""置いた合成データが、生成スクリプト（tests/fixtures/make_cao_fixtures.py）の出力と同じであることを確かめる。

テストの期待値は生成スクリプトの表から手で数えている。表とファイルが食い違うと期待値の根拠がずれるため。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from conftest import FIXTURES

SCRIPT = Path(__file__).resolve().parent / "fixtures" / "make_cao_fixtures.py"


def _outputs() -> dict[str, bytes]:
    spec = importlib.util.spec_from_file_location("make_cao_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.outputs()


@pytest.mark.parametrize(
    "name",
    [
        "kyoso_2024_mini.csv",
        "kyoso_2025_mini.csv",
        "zuikei_2024_mini.csv",
        "zuikei_2025_mini.csv",
        "kyoso_bad_header.csv",
    ],
)
def test_fixture_files_match_generator(name: str) -> None:
    assert (FIXTURES / name).read_bytes() == _outputs()[name]
