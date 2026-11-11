import re
import subprocess

KUBECTL_PREFIX = ["kubectl", "--context", "kind-lab", "-n", "sre-lab"]
POD_PATTERN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
TIMEOUT_SECONDS = 10
MAX_OUTPUT_CHARS = 4000
MIN_TAIL = 1
MAX_TAIL = 200
MAX_STDERR_CHARS = 200
MAX_POD_LEN = 253  # Kubernetes object-name limit
# kubectl exits 0 and prints this on stdout when the old container's logs are gone
UNAVAILABLE_LOGS_PREFIX = "unable to retrieve container logs"
PREVIOUS_UNAVAILABLE = (
    "Previous-container logs unavailable (often garbage-collected after many restarts). "
    "Try previous=False, or describe_pod to see last state and exit code."
)
NAMESPACE="sre-lab"


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    # The cap includes the marker, so: omitted = len(text) - keep and keep = cap - len(marker).
    # The marker's length depends on the digits of `omitted`, so iterate from the smallest
    # possible value; omitted only grows, so this settles in a couple of passes.
    omitted = len(text) - MAX_OUTPUT_CHARS
    while True:
        marker = f"\n[truncated: {omitted} chars omitted]"
        keep = MAX_OUTPUT_CHARS - len(marker)
        if len(text) - keep == omitted:
            return text[:keep] + marker
        omitted = len(text) - keep


def _explain_failure(verb: str, pod: str | None, exit_code: int, stderr: str) -> str:
    lowered = stderr.lower()
    if pod and ("notfound" in lowered or "not found" in lowered):
        return f"Pod '{pod}' not found in namespace sre-lab. Call get_pods to list valid pod names."
    if pod and "waiting to start" in lowered:
        return (
            f"Pod '{pod}' container never ran (waiting to start, e.g. image pull failure), "
            f"so no logs exist. Use describe_pod('{pod}') or get_events() to see why."
        )
    detail = stderr.strip()[:MAX_STDERR_CHARS] or "no error output"
    return (
        f"Error: 'kubectl {verb}' failed (exit {exit_code}): {detail}. "
        "Check the pod name with get_pods() and that the kind-lab context and sre-lab namespace exist."
    )


def _run_kubectl(args: list[str], pod: str | None = None) -> str:
    cmd = KUBECTL_PREFIX + args
    verb = args[0]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return (
            f"Error: 'kubectl {verb}' timed out after {TIMEOUT_SECONDS}s. "
            "Check the kind-lab cluster is reachable (kubectl --context kind-lab get nodes) and retry."
        )
    except FileNotFoundError:
        return "Error: kubectl not found on PATH. Install kubectl or fix PATH, then retry."
    if result.returncode != 0:
        return _explain_failure(verb, pod, result.returncode, result.stderr)
    return _truncate(result.stdout)


def _valid_pod(pod: str) -> bool:
    # fullmatch (not ^$) so a trailing newline can't slip through
    return isinstance(pod, str) and len(pod) <= MAX_POD_LEN and POD_PATTERN.fullmatch(pod) is not None


def _invalid_pod_error(pod) -> str:
    return (
        f"Error: invalid pod name {str(pod)[:60]!r}. "
        "Use lowercase letters, digits and '-' only (start and end alphanumeric); see get_pods() for names."
    )


def get_pods() -> str:
    return _run_kubectl(["get", "pods", "-o", "wide"])


def get_events() -> str:
    return _run_kubectl(["get", "events", "--sort-by=.lastTimestamp"])


def describe_pod(pod: str) -> str:
    if not _valid_pod(pod):
        return _invalid_pod_error(pod)
    return _run_kubectl(["describe", "pod", pod], pod=pod)


def get_logs(pod: str, tail: int = 50, previous: bool = False) -> str:
    if not _valid_pod(pod):
        return _invalid_pod_error(pod)
    try:
        tail = max(MIN_TAIL, min(MAX_TAIL, int(tail)))
    except (TypeError, ValueError):
        tail = 50
    args = ["logs", pod, f"--tail={tail}"]
    if previous:
        args.append("--previous")
    output = _run_kubectl(args, pod=pod)
    if previous and output.startswith(UNAVAILABLE_LOGS_PREFIX):
        return PREVIOUS_UNAVAILABLE
    return output
