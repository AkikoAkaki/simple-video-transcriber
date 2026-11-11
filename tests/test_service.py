"""Protect recordings, durable job state, and expensive transcription results."""
import io
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from background.service import BackgroundService
from paths import job_paths, partial_path, source_fingerprint, wav_cache_path
from protocol import SERVER_READY, JobOptions, JobRequest


@pytest.fixture
def svc(tmp_path):
    app = BackgroundService(lambda _event, _payload: None)
    yield app
    app.stop()


def set_job(svc, job_id, **columns):
    """Put a job in a state a test starts from, as an earlier run would have left it."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with sqlite3.connect(svc.store.path) as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id = ?", (*columns.values(), job_id))


def run_with_worker(svc, monkeypatch, row, proc):
    """Drive one job through the real queue against a stand-in for the worker process."""
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: proc)
    svc.worker.start()
    svc.worker.enqueue(row["job_id"])
    deadline = time.monotonic() + 10
    while svc.store.get(row["job_id"])["status"] in ("queued", "running", "cancel_requested"):
        assert time.monotonic() < deadline, "job never settled"
        time.sleep(0.02)
    return svc.store.get(row["job_id"])


def test_retry_keeps_settings_and_rejects_replaced_recording(svc, tmp_path):
    source = tmp_path / "New Recording 4.m4a"
    source.write_bytes(b"original recording")
    original = svc.store.create_if_new(source, JobOptions(language="en", device="cuda"))
    assert svc.add_files([source]) == []  # An active recording must not be queued twice.
    set_job(svc, original["job_id"], status="failed")
    retried = svc.retry_job(original["job_id"])
    assert retried["job_id"] == original["job_id"]
    assert json.loads(retried["options_json"]) == json.loads(original["options_json"])
    set_job(svc, original["job_id"], status="failed")
    source.write_bytes(b"different recording at the same path")
    with pytest.raises(ValueError, match="changed"):
        svc.retry_job(original["job_id"])
    assert svc.add_files([source])[0]["job_id"] != original["job_id"]
    source.unlink()
    with pytest.raises(FileNotFoundError):
        svc.retry_job(original["job_id"])


def test_restart_recovers_work_but_not_cancelled_or_completed_jobs(svc, tmp_path, monkeypatch):
    jobs = {}
    for status in ("queued", "running", "cancel_requested", "completed"):
        source = tmp_path / f"{status}.m4a"
        source.write_bytes(b"recording")
        row = svc.store.create_if_new(source)
        set_job(svc, row["job_id"], status=status, retry_count=4)
        jobs[status] = row["job_id"]
    monkeypatch.setattr(svc.worker, "start", lambda: None)  # Keep the real worker process out of the test.
    svc.start()
    assert {row["job_id"] for row in svc.store.queued()} == {jobs["queued"], jobs["running"]}
    assert svc.store.get(jobs["cancel_requested"])["status"] == "cancelled"
    assert svc.store.get(jobs["completed"])["status"] == "completed"
    set_job(svc, jobs["running"], status="running")
    svc.store.recover_interrupted()
    assert svc.store.get(jobs["running"])["status"] == "failed"


def test_cancellation_stops_worker_and_ignores_late_results(svc, tmp_path, monkeypatch):
    source = tmp_path / "cancel.m4a"
    source.write_bytes(b"audio")
    row = svc.store.create_if_new(source)
    proc = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=0)
    killed = []
    monkeypatch.setattr("background.worker.terminate_process_tree", lambda p: killed.append(p))

    def events():
        yield SERVER_READY
        svc.worker.cancel_active()
        yield '@@EVENT {"event":"completed","output_path":"stale.md"}'

    proc.stdout = events()
    result = run_with_worker(svc, monkeypatch, row, proc)
    assert killed == [proc]
    assert result["status"] == "cancelled"
    assert result["output_path"] == ""


@pytest.mark.parametrize("outcome", ["missing-output", "crash-with-old-output", "completed"])
def test_worker_never_claims_false_success(svc, tmp_path, monkeypatch, outcome):
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"audio")
    row = svc.store.create_if_new(source, JobOptions(language="en", device="cuda"))
    output = tmp_path / "transcripts" / "result.md"
    output.parent.mkdir(exist_ok=True)
    if outcome != "missing-output":
        output.write_text("saved transcript", encoding="utf-8")
    messages = [SERVER_READY, '@@EVENT {"event":"failed","job_id":"previous-job","error":"stale"}']
    if outcome != "crash-with-old-output":
        messages.append("@@EVENT " + json.dumps({
            "event": "completed", "job_id": row["job_id"], "output_path": str(output),
        }))
    proc = SimpleNamespace(stdin=io.StringIO(), stdout=iter(messages), poll=lambda: 1, pid=0)
    result = run_with_worker(svc, monkeypatch, row, proc)
    assert result["status"] == ("completed" if outcome == "completed" else "failed")
    if outcome != "missing-output":
        assert output.read_text(encoding="utf-8") == "saved transcript"
    sent = JobRequest.from_line(proc.stdin.getvalue())
    assert (sent.job_id, sent.options.language, sent.options.device) == (row["job_id"], "en", "cuda")


def test_audio_cleanup_preserves_all_unowned_and_active_files(svc, tmp_path):
    cache = config.CACHE_DIR
    cache.mkdir()
    removable = set()
    for status in ("completed", "failed", "cancelled", "queued", "running", "cancel_requested"):
        folder = tmp_path / status
        folder.mkdir()
        source = folder / "same.m4a"
        source.write_bytes(b"recording")
        row = svc.store.create_if_new(source)
        set_job(svc, row["job_id"], status=status)
        wav = wav_cache_path("same", source_fingerprint(source))
        job_paths(source, tmp_path, source_fingerprint(source)).whisper_json.write_bytes(b"recognition cache")
        for audio in (wav, partial_path(wav)):
            audio.write_bytes(b"audio")
            if status in ("completed", "failed", "cancelled"):
                removable.add(audio.name)
    for name in ("same_16k.wav", "same_16k.part.wav", "untracked_16k.wav",
                 "untracked_16k.part.wav", "saved.md"):
        (cache / name).write_bytes(b"must survive")
    before = {p.name: p.read_bytes() for p in cache.iterdir()}
    result = svc.clear_audio_cache()
    after = {name: (cache / name).read_bytes() for name in before if (cache / name).exists()}
    assert after == {name: data for name, data in before.items() if name not in removable}
    assert result.deleted_count == len(removable)
