"""Unit tests for tools.py. subprocess.run is always mocked: no kubectl or cluster needed."""
import re
import subprocess
from unittest.mock import patch

import pytest

import tools

PREFIX = ["kubectl", "--context", "kind-lab", "-n", "sre-lab"]
CAP = 4000


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def run_mock(**kw):
    return patch("tools.subprocess.run", return_value=completed(**kw))


# --- pod name validation: rejected before any kubectl call ---

@pytest.mark.parametrize("bad", ["x; rm -rf /", "--all-namespaces", "", "a" * 254, "-x", "Bad", "ok\n", None])
def test_bad_pod_rejected_without_running_kubectl(bad):
    with patch("tools.subprocess.run") as run:
        assert tools.describe_pod(bad).startswith("Error: invalid pod name")
        assert tools.get_logs(bad).startswith("Error: invalid pod name")
        run.assert_not_called()


def test_longest_valid_name_reaches_kubectl():
    with run_mock(stdout="ok") as run:
        assert tools.describe_pod("a" * 253) == "ok"
    assert run.call_args.args[0] == PREFIX + ["describe", "pod", "a" * 253]


# --- argv shape and verbs ---

def test_argv_and_kwargs():
    with run_mock(stdout="ok") as run:
        tools.get_pods()
        tools.get_events()
        tools.describe_pod("web-1")
        tools.get_logs("web-1", tail=20, previous=True)
    assert [c.args[0] for c in run.call_args_list] == [
        PREFIX + ["get", "pods", "-o", "wide"],
        PREFIX + ["get", "events", "--sort-by=.lastTimestamp"],
        PREFIX + ["describe", "pod", "web-1"],
        PREFIX + ["logs", "web-1", "--tail=20", "--previous"],
    ]
    for c in run.call_args_list:  # list argv, never shell=True
        assert c.kwargs == {"capture_output": True, "text": True, "timeout": 10}


def test_only_get_describe_logs_reachable():
    # Hostile and verb-like inputs through every public function: the verb slot never changes.
    inputs = ["delete", "exec", "apply", "x; rm -rf /", "--all-namespaces", "ok"]
    with run_mock(stdout="x") as run:
        tools.get_pods()
        tools.get_events()
        for pod in inputs:
            tools.describe_pod(pod)
            tools.get_logs(pod, tail=999, previous=True)
    assert run.call_count > 4
    for c in run.call_args_list:
        argv = c.args[0]
        assert argv[: len(PREFIX)] == PREFIX
        assert argv[len(PREFIX)] in {"get", "describe", "logs"}


# --- tail clamp ---

@pytest.mark.parametrize("given,expected", [(0, 1), (-5, 1), (1, 1), (200, 200), (9999, 200), ("abc", 50)])
def test_tail_clamped_to_1_200(given, expected):
    with run_mock(stdout="x") as run:
        tools.get_logs("web-1", tail=given)
    assert f"--tail={expected}" in run.call_args.args[0]


# --- truncation: the cap includes the marker ---

def test_no_truncation_at_or_under_cap():
    for n in (CAP - 1, CAP):
        with run_mock(stdout="x" * n):
            assert tools.get_pods() == "x" * n


@pytest.mark.parametrize("n", list(range(CAP + 1, CAP + 300)) + [10_000, 100_000, 10_000_000])
def test_truncated_to_exactly_cap_with_correct_marker(n):
    raw = "x" * n
    with run_mock(stdout=raw):
        out = tools.get_pods()
    body, marker = out.rsplit("\n", 1)
    assert len(out) == CAP
    assert raw.startswith(body)
    assert marker == f"[truncated: {n - len(body)} chars omitted]"


# --- failures return short actionable strings ---

def test_generic_nonzero_exit():
    with run_mock(stderr="boom", returncode=1):
        out = tools.get_pods()
    assert "failed (exit 1): boom" in out and "get_pods()" in out


def test_timeout_and_missing_kubectl():
    with patch("tools.subprocess.run", side_effect=subprocess.TimeoutExpired("kubectl", 10)):
        out = tools.get_pods()
    assert "timed out after 10s" in out and "Traceback" not in out
    with patch("tools.subprocess.run", side_effect=FileNotFoundError):
        assert "kubectl not found" in tools.get_pods()


@pytest.mark.parametrize("fn", [tools.describe_pod, tools.get_logs])
def test_pod_not_found_says_to_call_get_pods(fn):
    with run_mock(stderr='Error from server (NotFound): pods "gone" not found', returncode=1):
        out = fn("gone")
    assert "'gone' not found" in out and "get_pods" in out


def test_waiting_to_start_says_no_logs_exist():
    stderr = 'Error from server (BadRequest): container "nginx" in pod "bad" is waiting to start: image can\'t be pulled'
    with run_mock(stderr=stderr, returncode=1):
        out = tools.get_logs("bad")
    assert "never ran" in out and "no logs exist" in out
    assert "describe_pod" in out and "get_events" in out


def test_previous_unavailable_message():
    with run_mock(stdout="unable to retrieve container logs for containerd://abc\n"):
        out = tools.get_logs("crashy", previous=True)
    assert out == tools.PREVIOUS_UNAVAILABLE
    assert "previous=False" in out and "describe_pod" in out


def test_normal_logs_pass_through():
    with run_mock(stdout="boom\n"):
        assert tools.get_logs("crashy") == "boom\n"
