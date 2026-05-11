"""v4_modules エントリポイント — python -m v4_modules で起動。"""
from __future__ import annotations
import os
import sys
import io
import logging
from pathlib import Path

def main():
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    if hasattr(sys.stdin, "buffer"):
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

    from v4_modules.utils import safe_print, C
    import v4_modules.config as _cfg
    from v4_modules.config import load_config
    from v4_modules.agent import GeminiAgent, AccountRotator
    from v4_modules.tools import tools
    from v4_modules.rag import LongTermMemory
    from v4_modules.autogit import AutoGit
    from v4_modules.orchestrator import (InteractiveOrchestrator, AgentOrchestrator,
                                          POWERSHELL_EXECUTOR_GUIDANCE, REACT_SYSTEM_PROMPT)
    from v4_modules.main import interactive_loop, pipe_mode, auto_mode

    logging.getLogger("gemini_agent").handlers = [
        h for h in logging.getLogger("gemini_agent").handlers
        if isinstance(h, logging.FileHandler)]  # FileHandler のみ残す（コンソール出力を削除）

    base_dir = str(Path(__file__).parent)
    accounts, system_prompt = load_config(base_dir)
    rotator = AccountRotator(accounts)

    long_term_memory = LongTermMemory(str(Path(base_dir) / "lessons_db.json"))
    agent        = GeminiAgent(rotator, tools)
    orchestrator = AgentOrchestrator(rotator, tools, executor=agent, long_term_memory=long_term_memory)
    auto_git     = AutoGit()

    # UNIMOG_CWD (mcp_server.py からの指定) を優先し、次に .env の DEFAULT_CWD を使う
    unimog_cwd = os.environ.get("UNIMOG_CWD")
    if unimog_cwd and Path(unimog_cwd).exists():
        agent.cwd = str(Path(unimog_cwd).resolve())
    elif _cfg._DEFAULT_CWD and Path(_cfg._DEFAULT_CWD).exists():
        agent.cwd = str(Path(_cfg._DEFAULT_CWD).resolve())

    plan_prompt  = (system_prompt or "") + POWERSHELL_EXECUTOR_GUIDANCE
    react_prompt = plan_prompt + REACT_SYSTEM_PROMPT
    agent.set_system_prompt(react_prompt)
    orchestrator.set_executor_system_prompt(plan_prompt)
    interactive_orch = InteractiveOrchestrator(agent, auto_git, long_term_memory=long_term_memory)

    args = sys.argv[1:]
    if "--prompt" in args:
        idx = args.index("--prompt")
        pipe_mode(agent, args[idx + 1]) if idx + 1 < len(args) else sys.exit(1)
    elif "--auto-prompt" in args:
        idx = args.index("--auto-prompt")
        auto_mode(interactive_orch, args[idx + 1], orchestrator) if idx + 1 < len(args) else sys.exit(1)
    elif "--status" in args:
        agent.print_status(); sys.exit(0)
    else:
        interactive_loop(agent, orchestrator, interactive_orch, auto_git, react_prompt, plan_prompt)

if __name__ == "__main__":
    main()
