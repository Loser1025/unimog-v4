"""
mcp_server.py  —  Unimog V4 MCP Server (Full-Agent Edition)
=============================================================
Claude Code (推論) ← MCP → このサーバー → v4_modules フルエージェント

Claude の役割: ユーザー意図を理解して agent_run を呼ぶだけ
v4_modules の役割: Gemini ReAct ループ + AutoGit + LongTermMemory + 全ツール実行

起動: python mcp_server.py  (Claude Code が settings.json から自動起動)
"""

import asyncio
import logging
import re
import subprocess
import sys
from pathlib import Path

# ── v4_modules インポート（ツール登録のみ・main()は実行しない）────────
import sys
V4_DIR = Path(__file__).parent
if str(V4_DIR) not in sys.path:
    sys.path.insert(0, str(V4_DIR))
logging.disable(logging.CRITICAL)
from v4_modules.tools import tools as v4_tools   # ToolRegistry — 直接ツール呼び出し用
logging.disable(logging.NOTSET)

# ── MCP ─────────────────────────────────────────────────────────
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("unimog-v4")

# ── ログ監視ウィンドウ管理 ───────────────────────────────────────
_monitor_launched: bool = False
_LOG_PATH = str(V4_DIR / "v4_modules" / "gemini_agent.log")

def _ensure_log_monitor():
    """
    ログ監視 PowerShell ウィンドウを1回だけ起動する。
    すでに起動済みの場合は何もしない。
    CREATE_NEW_CONSOLE で独立した新しいコンソールウィンドウを開く。
    """
    global _monitor_launched
    if _monitor_launched:
        return
    try:
        # クォート問題を避けるため EncodedCommand を使用
        import base64
        inner = f"Get-Content -Path '{_LOG_PATH}' -Wait -Tail 20 -Encoding UTF8"
        encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(
            ["powershell", "-NoExit", "-NoProfile", "-EncodedCommand", encoded],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
        _monitor_launched = True
    except Exception:
        pass  # 起動失敗は無視（監視ウィンドウはオプション機能）

# MCP から除外するツール（stdin 必須 / MCP 文脈で不要）
_EXCLUDE = {"ask_user"}

# ANSI エスケープコード除去
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ════════════════════════════════════════════════════════════════
# ツール一覧
# ════════════════════════════════════════════════════════════════
@server.list_tools()
async def list_tools() -> list[Tool]:
    result: list[Tool] = []

    # ── ① メインツール: フルエージェント実行（最優先で使う）────────
    result.append(Tool(
        name="agent_run",
        description=(
            "【最優先】タスクを V4.py の Gemini フルエージェントに委譲して実行する。\n"
            "\n"
            "内部で実行されること:\n"
            "  • AutoGit バックアップ（タスク前に自動コミット）\n"
            "  • LongTermMemory から過去の教訓を RAG 注入\n"
            "  • Gemini ReAct ループ（Thought→Action→Observation 最大60ステップ）\n"
            "  • ファイル編集・PowerShell・Web検索・Wikipedia など全ツール自動実行\n"
            "  • 並列ツール実行対応\n"
            "  • AutoGit チェックポイント（書き込み後に自動コミット）\n"
            "  • 教訓の自動保存（次回以降に活用）\n"
            "\n"
            "使い分け:\n"
            "  agent_run   → コーディング・ファイル編集・調査・マルチステップ作業\n"
            "  agent_plan  → 複雑なプロジェクト（計画→並列実行→レビュー）\n"
            "  個別ツール  → 単純な読み取り確認のみ"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "V4.py エージェントに実行させるタスクの詳細な説明（日本語OK）"
                },
                "working_dir": {
                    "type": "string",
                    "description": "作業ディレクトリのフルパス。省略時は V4.py と同じフォルダ"
                },
                "timeout": {
                    "type": "integer",
                    "description": "タイムアウト秒数（デフォルト 600 = 10分）",
                    "default": 600
                }
            },
            "required": ["task"]
        }
    ))

    # ── ② Plan-and-Execute モード ────────────────────────────────
    result.append(Tool(
        name="agent_plan",
        description=(
            "大規模タスクを V4.py の Plan-and-Execute モードで実行する。\n"
            "Planner がステップに分解 → Executor が並列実行 → Reviewer が検証。\n"
            "リファクタリング・大規模追加・複数ファイル変更などに最適。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "実行させるタスクの詳細な説明"
                },
                "working_dir": {
                    "type": "string",
                    "description": "作業ディレクトリのフルパス"
                },
                "timeout": {
                    "type": "integer",
                    "description": "タイムアウト秒数（デフォルト 900 = 15分）",
                    "default": 900
                }
            },
            "required": ["task"]
        }
    ))

    # ── ③ ターミナル起動（長時間・ユーザー監視が必要な作業）────────
    result.append(Tool(
        name="agent_terminal",
        description=(
            "V4.py を新しいターミナルウィンドウで起動する（インタラクティブモード）。\n"
            "30分以上かかる作業・ユーザーが途中で確認したい場合に使う。\n"
            "このツールは起動だけして即返す（結果待ちなし）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "ターミナルに表示する初期タスクメモ（参照用）"
                },
                "working_dir": {
                    "type": "string",
                    "description": "作業ディレクトリのフルパス"
                }
            },
            "required": ["task"]
        }
    ))

    # ── ④ 軽量読み取りツール（Claude が直接使う・推論負荷ゼロ）────
    _read_only = {"read_file", "list_directory", "glob", "grep", "get_repo_map", "read_tool_cache"}
    for spec in v4_tools.get_specs():
        if spec["name"] not in _read_only:
            continue
        result.append(Tool(
            name=spec["name"],
            description=f"[軽量読み取り] {spec['description']}",
            inputSchema=spec["parameters"],
        ))

    return result


# ════════════════════════════════════════════════════════════════
# ツール実行
# ════════════════════════════════════════════════════════════════
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    loop = asyncio.get_event_loop()

    if name == "agent_run":
        result = await loop.run_in_executor(
            None, _agent_run,
            arguments.get("task", ""),
            arguments.get("working_dir"),
            arguments.get("timeout", 600),
            False,  # plan_mode=False
        )
        return [TextContent(type="text", text=result)]

    if name == "agent_plan":
        result = await loop.run_in_executor(
            None, _agent_run,
            arguments.get("task", ""),
            arguments.get("working_dir"),
            arguments.get("timeout", 900),
            True,   # plan_mode=True
        )
        return [TextContent(type="text", text=result)]

    if name == "agent_terminal":
        result = _agent_terminal(
            arguments.get("task", ""),
            arguments.get("working_dir"),
        )
        return [TextContent(type="text", text=result)]

    # 軽量読み取りツール
    try:
        result = await loop.run_in_executor(
            None, lambda: v4_tools.execute(name, arguments)
        )
        return [TextContent(type="text", text=str(result))]
    except ValueError as e:
        return [TextContent(type="text", text=f"未知のツール: {name}  ({e})")]
    except Exception as e:
        return [TextContent(type="text", text=f"エラー [{name}]: {type(e).__name__}: {e}")]


# ════════════════════════════════════════════════════════════════
# agent_run / agent_plan 実装
# ════════════════════════════════════════════════════════════════
def _agent_run(task: str, working_dir: str | None,
               timeout: int, plan_mode: bool) -> str:
    """
    V4.py を --auto-prompt（または /plan 相当）で起動して結果を返す。
    --auto-prompt モード:
      InteractiveOrchestrator.run_react() をフルで実行
      AutoGit + LongTermMemory + ReAct ループ + 全ツール
      ユーザー確認なし（自動承認）
    """
    _ensure_log_monitor()   # タスク開始時にログ監視ウィンドウを起動（初回のみ）

    cwd     = working_dir or str(V4_DIR)
    py      = sys.executable

    if plan_mode:
        # /plan モード: タスクの先頭に /plan を付けて --auto-prompt に渡す
        task_arg = f"/plan {task}"
    else:
        task_arg = task

    flag = "--auto-prompt"

    # 作業ディレクトリを v4_modules に伝える
    env_patch = {
        "UNIMOG_CWD": cwd,
    }
    import os
    env = {
        **os.environ,
        **env_patch,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }

    DONE_MARKER = "===UNIMOG_DONE==="
    import threading, time

    try:
        proc = subprocess.Popen(
            [py, "-m", "v4_modules", flag, task_arg],
            stdin=subprocess.DEVNULL,   # MCP stdio を汚染させない
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # stderr も stdout にまとめてキャプチャ
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except Exception as e:
        return f"v4_modules 起動エラー: {type(e).__name__}: {e}"

    result_lines: list[str] = []
    done_event = threading.Event()

    def _read():
        """stdout を行単位で読む。マーカーを検出したら即 done_event をセット。"""
        collecting = True
        try:
            for raw in proc.stdout:
                clean = strip_ansi(raw)
                if DONE_MARKER in clean:
                    done_event.set()
                    collecting = False
                    # V4.py がバックグラウンドで教訓保存中 → 読み捨てて排出
                    continue
                if collecting:
                    result_lines.append(clean)
        finally:
            done_event.set()  # EOF でも必ずセット

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()

    if done_event.wait(timeout=timeout):
        answer = "".join(result_lines).strip()
        return answer or "(V4.py の出力なし)"
    else:
        proc.kill()
        proc.wait()
        partial = "".join(result_lines).strip()
        return (
            f"タイムアウト ({timeout}秒)\n"
            f"途中出力:\n{partial[-2000:] if partial else '(なし)'}\n\n"
            f"長時間タスクは agent_terminal を使ってターミナルで実行してください。"
        )


# ════════════════════════════════════════════════════════════════
# agent_terminal 実装
# ════════════════════════════════════════════════════════════════
def _agent_terminal(task: str, working_dir: str | None) -> str:
    """新しい cmd ウィンドウで v4_modules をインタラクティブ起動"""
    cwd   = working_dir or str(V4_DIR)
    py    = sys.executable
    title = "Unimog V4 — Agent Terminal"

    task_preview = task[:300].replace('"', "'")

    cmd = f'start "{title}" cmd /k "cd /d {V4_DIR} && {py} -m v4_modules"'
    try:
        subprocess.Popen(cmd, shell=True, cwd=cwd)
        return (
            f"v4_modules ターミナルを起動しました。\n"
            f"作業フォルダ: {cwd}\n\n"
            f"以下のタスクをターミナルに貼り付けてください:\n"
            f"{'─'*50}\n"
            f"{task_preview}\n"
            f"{'─'*50}"
        )
    except Exception as e:
        return f"ターミナル起動エラー: {e}"


# ════════════════════════════════════════════════════════════════
# エントリポイント
# ════════════════════════════════════════════════════════════════
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )

if __name__ == "__main__":
    asyncio.run(main())
