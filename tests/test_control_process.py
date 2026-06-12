import sys
import time

import pytest

from livetranslate.control.process import PipelineProcess

# A stub child that prints, handles SIGINT gracefully, then idles.
STUB = """
import signal, sys, time
def bye(_s, _f):
    print("drained", flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, bye)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, bye)
print("pipeline up", flush=True)
while True:
    time.sleep(0.1)
"""


def make_proc(tmp_path, stub=STUB):
    return PipelineProcess(tmp_path, cmd=[sys.executable, "-u", "-c", stub])


def wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_start_runs_and_captures_logs(tmp_path):
    proc = make_proc(tmp_path)
    proc.start(extra_env={"FAKE_KEY": "x"})
    assert proc.running()
    assert wait_until(lambda: any("pipeline up" in l for l in proc.logs_since(0)[0]))
    proc.stop()


def test_start_twice_raises(tmp_path):
    proc = make_proc(tmp_path)
    proc.start(extra_env={})
    with pytest.raises(RuntimeError):
        proc.start(extra_env={})
    proc.stop()


def test_stop_is_graceful_then_records_exit(tmp_path):
    proc = make_proc(tmp_path)
    proc.start(extra_env={})
    wait_until(lambda: any("pipeline up" in l for l in proc.logs_since(0)[0]))
    proc.stop()
    assert not proc.running()
    assert wait_until(lambda: any("drained" in l for l in proc.logs_since(0)[0]))
    assert proc.last_exit == 0


def test_logs_since_cursor(tmp_path):
    proc = make_proc(tmp_path)
    proc.start(extra_env={})
    wait_until(lambda: proc.logs_since(0)[1] >= 1)
    lines, seq = proc.logs_since(0)
    again, seq2 = proc.logs_since(seq)
    assert again == [] and seq2 == seq
    proc.stop()


def test_child_crash_sets_last_exit(tmp_path):
    proc = make_proc(tmp_path, stub="import sys; print('boom', flush=True); sys.exit(3)")
    proc.start(extra_env={})
    assert wait_until(lambda: proc.last_exit is not None)
    assert proc.last_exit == 3
    assert not proc.running()


def test_restart_after_crash_does_not_corrupt_new_run(tmp_path):
    """An old pump thread must not write its exit code/marker into a new run."""
    proc = make_proc(tmp_path, stub="import sys; print('boom', flush=True); sys.exit(3)")
    proc.start(extra_env={})
    assert wait_until(lambda: proc.last_exit == 3)
    # restart with a long-lived child; old run's state must not leak in
    proc.cmd = [sys.executable, "-u", "-c", STUB]
    proc.start(extra_env={})
    assert wait_until(lambda: any("pipeline up" in l for l in proc.logs_since(0)[0]))
    time.sleep(0.3)   # give any stale pump thread a chance to misbehave
    assert proc.last_exit is None          # new run is alive; old code must not reappear
    proc.stop()


def test_concurrent_start_only_launches_one_child(tmp_path):
    import threading
    proc = make_proc(tmp_path)
    errors = []

    def try_start():
        try:
            proc.start(extra_env={})
        except RuntimeError as e:
            errors.append(e)

    threads = [threading.Thread(target=try_start) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(errors) == 4               # exactly one start wins
    assert proc.running()
    proc.stop()
