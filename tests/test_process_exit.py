"""Budget overruns must bound process lifetime, not just function return.

``concurrent.futures`` joins its worker threads at interpreter exit, so a
budget-bounded function could return on time while the CLI/subprocess
invocation still hung at exit until the overdue worker finished. These tests
run the real interpreter-exit path in a subprocess and assert the process
terminates promptly while a worker is still sleeping.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The hung worker sleeps far longer than the asserted bounds: if worker
# threads were joined at exit again, these tests would fail loudly.
_RESEARCH_SCRIPT = """
import json, time
from research import run_research_mode

def execute(provider):
    if provider == "slow":
        time.sleep(10.0)
    return {"provider": provider, "results": [
        {"url": "https://" + provider + ".test/a", "title": provider, "description": "x"},
    ]}

def extract(urls):
    return {"provider": None, "results": []}

start = time.monotonic()
result = run_research_mode(
    query="exit promptly",
    research_providers=["fast", "slow"],
    execute_search=execute,
    extract_urls=extract,
    max_results=5,
    max_extract_urls=0,
    time_budget_seconds=0.3,
)
print(json.dumps({
    "function_elapsed": time.monotonic() - start,
    "providers_queried": result["routing"]["providers_queried"],
}))
"""

_LINKUP_SCRIPT = """
import json, time
import providers

def fake_make_request(url, headers, body, timeout=30):
    if body["url"] == "https://slow.test/page":
        time.sleep(10.0)
    return {"markdown": "content for " + body["url"]}

providers.make_request = fake_make_request
providers._BATCH_TIMEOUT_GRACE_SECONDS = 0.3

start = time.monotonic()
result = providers.extract_linkup(
    ["https://fast.test/page", "https://slow.test/page"],
    api_key="linkup-test-key-12345",
    timeout=0,
)
print(json.dumps({
    "function_elapsed": time.monotonic() - start,
    "errors": [bool(r.get("error")) for r in result["results"]],
}))
"""


def _run_in_subprocess(script: str) -> "tuple[float, dict]":
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=20,
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return elapsed, payload


def test_research_budget_overrun_does_not_stall_process_exit():
    elapsed, payload = _run_in_subprocess(_RESEARCH_SCRIPT)

    assert payload["function_elapsed"] < 3.0
    assert payload["providers_queried"] == ["fast"]
    # Includes interpreter startup; the overdue worker sleeps 10s, so a joined
    # worker pool would keep the process alive well past this bound.
    assert elapsed < 6.0


def test_linkup_batch_timeout_does_not_stall_process_exit():
    elapsed, payload = _run_in_subprocess(_LINKUP_SCRIPT)

    assert payload["function_elapsed"] < 3.0
    assert payload["errors"] == [False, True]
    assert elapsed < 6.0
