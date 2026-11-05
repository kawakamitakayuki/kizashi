"""置いた合成データが、生成スクリプト（tests/fixtures/make_cao_fixtures.py・make_pdf_cells.py）の出力と
同じであることを確かめる。

テストの期待値は生成スクリプトの表から手で数えている。表とファイルが食い違うと期待値の根拠がずれるため。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from conftest import FIXTURES, PDF_CELLS

SCRIPT = Path(__file__).resolve().parent / "fixtures" / "make_cao_fixtures.py"
PDF_SCRIPT = Path(__file__).resolve().parent / "fixtures" / "make_pdf_cells.py"


def _outputs(script: Path = SCRIPT) -> dict[str, bytes]:
    spec = importlib.util.spec_from_file_location(script.stem, script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.outputs()


@pytest.mark.parametrize(
    "name",
    [
        "kyoso_2024_mini.json",
        "kyoso_2025_mini.json",
        "zuikei_2024_mini.json",
        "zuikei_2025_mini.json",
        "traps_zuikei.json",
        "traps_kyoso.json",
    ],
)
def test_pdf_cells_match_generator(name: str) -> None:
    assert (PDF_CELLS / name).read_bytes() == _outputs(PDF_SCRIPT)[name]


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
