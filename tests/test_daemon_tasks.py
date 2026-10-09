import threading

from wsp_core.daemon_tasks import DaemonTask


def test_done_callback_registered_before_completion_runs_once():
    release = threading.Event()
    callback_seen = threading.Event()
    calls = []

    task = DaemonTask(lambda: release.wait(1) or "done")
    task.add_done_callback(
        lambda completed: (calls.append(completed), callback_seen.set())
    )

    release.set()
    assert callback_seen.wait(1)
    assert calls == [task]
    assert task.done()


def test_done_callback_registered_after_completion_runs_immediately_once():
    task = DaemonTask(lambda: "done")
    assert task.result(timeout=1) == "done"
    calls = []

    task.add_done_callback(calls.append)

    assert calls == [task]


def test_failing_done_callback_does_not_change_task_result():
    release = threading.Event()

    def complete():
        release.wait(1)
        return "done"

    task = DaemonTask(complete)

    def fail(_task):
        raise RuntimeError("callback failed")

    task.add_done_callback(fail)
    release.set()

    assert task.result(timeout=1) == "done"
