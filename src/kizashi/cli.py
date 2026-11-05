"""kizashi の入口（implementation_spec.md 2章の cli.py、3.1・3.2）。

P0-0 では、起動時の検査（作業フォルダーと SQLite の版）と、エラーの JSON 化だけを本実装にしている。
7つの操作は、4.1〜4.7 の書式の引数を受け付けて {"stub": "<操作名>"} を返す仮の実装である。
ID の書式と --run の欠けの検査（E_BAD_ID。test_plan.md の CLI-2）は P0-2 で加える。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

from kizashi.errors import EXIT_OK, KizashiError

# 検出SQLが集約の中の ORDER BY（group_concat(x, '|' ORDER BY x)）を使うため（1章）。
MIN_SQLITE_VERSION: tuple[int, ...] = (3, 44)

# 操作を決める前（起動時の検査と引数の解釈）のエラーの stage。
STAGE_CLI = "cli"

OPERATIONS = ("run", "fetch", "inspect", "query", "record", "check", "render")


# ---------------------------------------------------------------------------
# 起動時の検査（3.1）
# ---------------------------------------------------------------------------


def check_working_dir() -> Path:
    """現在のフォルダーがリポジトリの直下（pyproject.toml と datasets/ がある）でなければ E_CWD。"""
    here = Path.cwd()
    if (here / "pyproject.toml").is_file() and (here / "datasets").is_dir():
        return here
    raise KizashiError(
        "E_CWD",
        STAGE_CLI,
        "リポジトリの直下で実行していない（pyproject.toml と datasets/ が現在のフォルダーにない）。",
        next_action="リポジトリの直下に移ってから kizashi を実行する。",
    )


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def check_sqlite_version() -> None:
    """Python に組み込まれた SQLite が3.44未満なら E_USAGE（1章）。"""
    version = sqlite3.sqlite_version
    required = ".".join(str(p) for p in MIN_SQLITE_VERSION)
    if _version_tuple(version) < MIN_SQLITE_VERSION:
        raise KizashiError(
            "E_USAGE",
            STAGE_CLI,
            f"SQLite の版が {version} で、{required} 以上が要る"
            "（検出SQLが集約の中の ORDER BY を使うため）。",
            next_action=(
                f"SQLite {required} 以上を組み込んだ Python（uv が入れる Python など）で"
                " .venv を作り直す（uv sync）。"
            ),
        )


# ---------------------------------------------------------------------------
# 引数の解釈（4.1〜4.7 の書式）
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """引数の誤りを E_USAGE の例外にする。標準出力に1つのJSONだけを書くため（3.2）。"""

    def error(self, message: str) -> NoReturn:
        raise KizashiError(
            "E_USAGE",
            STAGE_CLI,
            f"引数の誤り：{message}",
            next_action=f"書式：{self.format_usage().strip()}",
        )


def _page_number(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or int(text) < 1:
        raise argparse.ArgumentTypeError(f"ページは1以上の整数で指定する：{text!r}")
    return int(text)


def build_parser() -> tuple[_Parser, dict[str, _Parser]]:
    """全体の解釈器と、操作ごとの解釈器を返す。

    --help は付けない（説明の文を出力すると、標準出力が1つのJSONでなくなるため）。
    省略形（--res など）は受け付けない。
    """
    parser = _Parser(prog="kizashi", add_help=False, allow_abbrev=False)
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="<操作>", parser_class=_Parser
    )
    ops: dict[str, _Parser] = {
        name: sub.add_parser(name, add_help=False, allow_abbrev=False) for name in OPERATIONS
    }

    # kizashi run <分野> --config inbox/<名前>.json
    # kizashi run --resume <run_id>
    p = ops["run"]
    p.add_argument("domain", nargs="?", metavar="<分野>")
    p.add_argument("--config", metavar="<ファイル>")
    p.add_argument("--resume", metavar="<run_id>")

    # kizashi fetch <run_id> <URL> [--index | --definition]
    # kizashi fetch <run_id> --recheck <source_id>
    p = ops["fetch"]
    p.add_argument("run_id", metavar="<run_id>")
    p.add_argument("url", nargs="?", metavar="<URL>")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--index", action="store_true")
    group.add_argument("--definition", action="store_true")
    p.add_argument("--recheck", metavar="<source_id>")

    # kizashi inspect <run_id> [--page <n>]
    # kizashi inspect <ID> --run <run_id> [--page <n>]
    p = ops["inspect"]
    p.add_argument("target_id", metavar="<ID>")
    p.add_argument("--run", dest="run_id", metavar="<run_id>")
    p.add_argument("--page", type=_page_number, default=1, metavar="<n>")

    # kizashi query <run_id> inbox/<名前>.sql
    p = ops["query"]
    p.add_argument("run_id", metavar="<run_id>")
    p.add_argument("sql_file", metavar="<SQLファイル>")

    # kizashi record <run_id> inbox/<名前>.json
    p = ops["record"]
    p.add_argument("run_id", metavar="<run_id>")
    p.add_argument("json_file", metavar="<JSONファイル>")

    # kizashi check <lead_id> --run <run_id>
    # kizashi check --publish examples/publish.manifest --run <run_id> [--run <run_id> ...]
    p = ops["check"]
    p.add_argument("lead_id", nargs="?", metavar="<lead_id>")
    p.add_argument("--publish", metavar="<manifest>")
    p.add_argument("--run", dest="run_ids", action="append", metavar="<run_id>")

    # kizashi render <lead_id> --run <run_id> [--public]
    # kizashi render <run_id> [--public]
    p = ops["render"]
    p.add_argument("target_id", metavar="<ID>")
    p.add_argument("--run", dest="run_id", metavar="<run_id>")
    p.add_argument("--public", action="store_true")

    return parser, ops


def _check_form(ops: dict[str, _Parser], args: argparse.Namespace) -> None:
    """引数の組み合わせが、各操作の書式のどれかに当たるかを確かめる。"""
    if args.command == "run":
        if args.resume is not None:
            if args.domain is not None or args.config is not None:
                ops["run"].error("--resume は <分野> と --config と一緒に使えない")
        elif args.domain is None or args.config is None:
            ops["run"].error("<分野> と --config <ファイル>、または --resume <run_id> が要る")
    elif args.command == "fetch":
        if args.recheck is not None:
            if args.url is not None or args.index or args.definition:
                ops["fetch"].error("--recheck は <URL>・--index・--definition と一緒に使えない")
        elif args.url is None:
            ops["fetch"].error("<URL> か --recheck <source_id> が要る")
    elif args.command == "check":
        if args.publish is not None:
            if args.lead_id is not None:
                ops["check"].error("--publish は <lead_id> と一緒に使えない")
        elif args.lead_id is None:
            ops["check"].error("<lead_id> か --publish <manifest> が要る")
        elif args.run_ids is not None and len(args.run_ids) > 1:
            ops["check"].error("候補の検査では --run を1つだけ指定する")


# ---------------------------------------------------------------------------
# 操作（P0-0 では仮の実装）
# ---------------------------------------------------------------------------


def _stub(name: str) -> Callable[[argparse.Namespace], dict]:
    def handler(args: argparse.Namespace) -> dict:
        return {"stub": name}

    return handler


HANDLERS: dict[str, Callable[[argparse.Namespace], dict]] = {op: _stub(op) for op in OPERATIONS}


# ---------------------------------------------------------------------------
# 出力（3.2）
# ---------------------------------------------------------------------------


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    """標準出力に1つのJSONを書き、終了コードを返す。"""
    arg_list = list(sys.argv[1:] if argv is None else argv)
    stage = STAGE_CLI
    try:
        check_working_dir()
        check_sqlite_version()
        parser, ops = build_parser()
        args = parser.parse_args(arg_list)
        _check_form(ops, args)
        stage = args.command
        data = HANDLERS[args.command](args)
    except KizashiError as exc:
        _emit({"ok": False, "error": exc.to_dict()})
        return exc.exit_code
    # 想定外のエラー（バグ）も1つのJSONにして返す（3.2）。
    # スタックトレースは標準エラー出力だけに出し、JSONには要約だけを入れる。
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        internal = KizashiError(
            "E_INTERNAL",
            stage,
            f"想定外のエラー（{type(exc).__name__}）。詳細は標準エラー出力にある。",
            next_action="調査のセッションでは直さず、保留にして記者に伝える。開発側で原因を調べる。",
        )
        _emit({"ok": False, "error": internal.to_dict()})
        return internal.exit_code
    _emit({"ok": True, "data": data})
    return EXIT_OK
