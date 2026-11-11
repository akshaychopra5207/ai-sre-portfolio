import logging
import time
from dataclasses import dataclass, field

import anthropic
from jsonschema import validate, ValidationError

import tools

log = logging.getLogger("agent")

# $/token, verify against the pricing page
MODEL_PRICES = {"claude-haiku-5-5": {"in": 0.1 / 1e6, "out": 0.5 / 1e6}}

# Same pattern as tools.py; the real validation stays there, this only helps the model get it right.
POD_SCHEMA = {
    "type": "string",
    "pattern": f"^{tools.POD_PATTERN.pattern}$",
    "maxLength": tools.MAX_POD_LEN,
    "description": "Pod name, e.g. one listed by get_pods.",
}

TOOLS = [
    {
        "name": "get_pods",
        "description": (
            "List pods in the sre-lab namespace with status, restarts, age and node. "
            "Use first to find pod names and spot unhealthy ones. "
            "Output may be truncated at 4000 characters."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_events",
        "description": (
            "List recent events in the sre-lab namespace, oldest first (warnings like BackOff, "
            "Failed, FailedScheduling). Use to see why pods are failing across the namespace. "
            "Output may be truncated at 4000 characters, so the newest events can be cut off."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "describe_pod",
        "description": (
            "Describe one pod: container state, last exit code, restart count, probes, and its events. "
            "Use after get_pods to explain why a specific pod is unhealthy. "
            "Output may be truncated at 4000 characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"pod": POD_SCHEMA},
            "required": ["pod"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_logs",
        "description": (
            "Get the last lines of a pod's container logs. Use for application errors. "
            "tail is the number of lines (default 50, max 200; larger values are clamped). "
            "previous=true returns the last crashed container's logs, which can be unavailable "
            "after many restarts. Output may be truncated at 4000 characters. "
            "A container that never started has no logs; use describe_pod instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod": POD_SCHEMA,
                "tail": {"type": "integer", "description": "Lines from the end, max 200."},
                "previous": {"type": "boolean", "description": "Logs of the previous (crashed) container."},
            },
            "required": ["pod"],
            "additionalProperties": False,
        },
    },
]

TOOL_FUNCS = {
    "get_pods": tools.get_pods,
    "get_events": tools.get_events,
    "describe_pod": tools.describe_pod,
    "get_logs": tools.get_logs,
}


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


def run_agent(client, cfg, prompt, system=None):
    prices = MODEL_PRICES[cfg.model]
    schemas = {t["name"]: t["input_schema"] for t in cfg.tools}
    messages = [{"role": "user", "content": prompt}]
    res = AgentResult(status="max_turns", messages=messages)  # stays max_turns if the loop runs out
    start = time.monotonic()
    # Only send `system` when set: None would be sent as null and rejected by the API.
    extra = {"system": system} if system else {}

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
                model=cfg.model, max_tokens=cfg.max_tokens, tools=cfg.tools, messages=messages, **extra)
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
