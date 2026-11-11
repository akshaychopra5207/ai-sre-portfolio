import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from agent import TOOL_FUNCS, TOOLS, AgentConfig, run_agent
from prompts import SYSTEM_PROMPT, TASK_PROMPT

RUNS_DIR = Path(__file__).parent / "runs"


def tool_calls(messages):
    # Assistant turns hold SDK blocks; tool_use blocks carry the name and args the model chose.
    return [(b.name, b.input) for m in messages if m["role"] == "assistant"
            for b in m["content"] if b.type == "tool_use"]


def save_run(messages):
    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    # SDK content blocks are pydantic models; dump them, leave plain dicts alone.
    path.write_text(json.dumps(messages, indent=2, default=lambda o: o.model_dump(mode="json")))
    return path


def main():
    # Refuse before touching the key or the API while the prompts are placeholders.
    if "TODO" in (SYSTEM_PROMPT, TASK_PROMPT):
        sys.exit("prompts.py still contains TODO: write SYSTEM_PROMPT and TASK_PROMPT first.")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY is not set.")

    cfg = AgentConfig(model="claude-haiku-5-5", tools=TOOLS, tool_funcs=TOOL_FUNCS,
                      max_turns=8, max_tokens=1024, max_cost_usd=0.10)
    res = run_agent(anthropic.Anthropic(api_key=api_key), cfg, TASK_PROMPT, system=SYSTEM_PROMPT)

    print(f"status: {res.status}")
    if res.error:
        print(f"error: {res.error}")
    print(f"turns: {res.turns}  tokens in/out: {res.tokens_in}/{res.tokens_out}  cost: ${res.cost:.6f}")
    print("tool calls:")
    for n, (name, args) in enumerate(tool_calls(res.messages), 1):
        print(f"  {n}. {name}({json.dumps(args)})")
    print(f"answer:\n{res.answer}")
    print(f"saved: {save_run(res.messages)}")
    sys.exit(0 if res.status == "ok" else 1)


if __name__ == "__main__":
    main()
