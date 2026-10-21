import argparse
import logging
import sys
import time
from dataclasses import dataclass, field

import anthropic
from jsonschema import validate, ValidationError

log = logging.getLogger("agent")

MODEL = "claude-haiku-5-5"
PROMPT = "Is pod api-7f9 in namespace staging healthy? If not, find out why."

# $/token, verify against the pricing page
MODEL_PRICES = {"claude-haiku-5-5": {"in": 0.1 / 1e6, "out": 0.5 / 1e6}}

TOOLS = [
    {
        "name": "get_pod_status",
        "description": "Get the status of a Kubernetes pod.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "get_pod_logs",
        "description": "Get the last lines of logs for a Kubernetes pod.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "lines": {"type": "integer"},
            },
            "required": ["namespace", "pod"],
        },
    },
]


def get_pod_status(namespace, pod):
    if namespace != "prod":
        raise ValueError(f"unknown namespace {namespace}")
    if pod != "api-7f9":
        raise ValueError(f"unknown pod {pod}")
    return "CrashLoopBackOff, restarts=14"


def get_pod_logs(namespace, pod, lines=20):
    if namespace != "prod":
        raise ValueError(f"unknown namespace {namespace}")
    if pod != "api-7f9":
        raise ValueError(f"unknown pod {pod}")
    return "ERROR: cannot connect to postgres:5432 (connection refused)"


TOOL_FUNCS = {"get_pod_status": get_pod_status, "get_pod_logs": get_pod_logs}


@dataclass
class AgentConfig:
    model: str
    tools: list
    tool_funcs: dict
    max_turns: int = 5
    max_tokens: int = 1024
    max_cost_usd: float = 0.05
    max_retries: int = 4
    deadline_s: float = 120.0  # wall-clock budget for the whole run


@dataclass
class AgentResult:
    # ok | max_turns | max_tokens | budget | deadline | api_error | unsupported_stop_reason
    status: str
    answer: str = ""
    error: str = ""
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    messages: list = field(default_factory=list)


def run_tool(cfg, schemas, block):
    log.info("tool=%s args=%s", block.name, block.input)
    result = {"type": "tool_result", "tool_use_id": block.id}
    t0 = time.perf_counter()
    try:
        if block.name not in cfg.tool_funcs:
            raise ValueError(f"unknown tool {block.name}")
        validate(instance=block.input, schema=schemas[block.name])
        result["content"] = str(cfg.tool_funcs[block.name](**block.input))
    except ValidationError as e:
        result.update(is_error=True, content=f"invalid input: {e.message} (at {list(e.absolute_path)})")
    except Exception as e:
        result.update(is_error=True, content=str(e))
    log.info("  -> %s %.0fms%s", block.name, (time.perf_counter() - t0) * 1000,
             " (error)" if result.get("is_error") else "")
    return result


def run_agent(client, cfg, prompt):
    prices = MODEL_PRICES[cfg.model]
    schemas = {t["name"]: t["input_schema"] for t in cfg.tools}
    messages = [{"role": "user", "content": prompt}]
    res = AgentResult(status="max_turns", messages=messages)  # stays max_turns if the loop runs out
    start = time.monotonic()

    for i in range(cfg.max_turns):
        # Guards run before the next call, so a final answer is never dropped.
        if res.cost > cfg.max_cost_usd:
            res.status, res.error = "budget", f"cost ${res.cost:.6f} > cap ${cfg.max_cost_usd} before turn {i}"
            break
        remaining = cfg.deadline_s - (time.monotonic() - start)
        if remaining <= 0:
            res.status, res.error = "deadline", f"{cfg.deadline_s}s deadline hit before turn {i}"
            break
        # Timeout is per attempt, so split what is left across the attempts.
        per_attempt = max(1.0, remaining / (cfg.max_retries + 1))

        try:
            msg = client.with_options(timeout=per_attempt, max_retries=cfg.max_retries).messages.create(
                model=cfg.model, max_tokens=cfg.max_tokens, tools=cfg.tools, messages=messages)
        except anthropic.APIStatusError as e:
            res.status = "api_error"
            res.error = f"turn={i} status={e.status_code} request_id={e.request_id} body={e.body}"
            break
        except anthropic.APIConnectionError as e:
            res.status, res.error = "api_error", f"turn={i} connection error: {e.__cause__!r}"
            break

        res.turns += 1
        res.tokens_in += msg.usage.input_tokens
        res.tokens_out += msg.usage.output_tokens
        res.cost = res.tokens_in * prices["in"] + res.tokens_out * prices["out"]
        log.info("turn=%d stop=%s in=%d out=%d total_cost=$%.6f", i, msg.stop_reason,
                 msg.usage.input_tokens, msg.usage.output_tokens, res.cost)
        messages.append({"role": "assistant", "content": msg.content})

        if msg.stop_reason == "end_turn":
            res.status = "ok"
            res.answer = "".join(b.text for b in msg.content if b.type == "text")
            break
        if msg.stop_reason == "max_tokens":
            has_tool = any(b.type == "tool_use" for b in msg.content)
            res.status = "max_tokens"
            res.error = f"turn={i} out={msg.usage.output_tokens}" + (
                " (cut mid tool call, no tool run)" if has_tool else "")
            break
        if msg.stop_reason == "tool_use":
            tool_results = [run_tool(cfg, schemas, b) for b in msg.content if b.type == "tool_use"]
            messages.append({"role": "user", "content": tool_results})
            continue
        res.status, res.error = "unsupported_stop_reason", f"turn={i} stop_reason={msg.stop_reason}"
        break

    return res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-tokens", type=int, default=1024)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log.setLevel(logging.INFO)
    cfg = AgentConfig(model=MODEL, tools=TOOLS, tool_funcs=TOOL_FUNCS, max_tokens=args.max_tokens)
    res = run_agent(anthropic.Anthropic(), cfg, PROMPT)

    if res.answer:
        print(res.answer)
    if res.error:
        print(f"error: {res.error}", file=sys.stderr)
    print(f"status={res.status} turns={res.turns} in={res.tokens_in} out={res.tokens_out} cost=${res.cost:.6f}")
    sys.exit(0 if res.status == "ok" else 1)


if __name__ == "__main__":
    main()
