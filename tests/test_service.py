"""Protect recordings, durable job state, and expensive transcription results."""
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import service
from paths import source_fingerprint


@pytest.fixture
def svc(tmp_path):
    app = service.BackgroundService(service.AppSettings(
        watch_dir=str(tmp_path / "watch"), transcript_dir=str(tmp_path / "transcripts"),
        watcher_enabled=False,
    ))
    app.worker.stop()  # Exercise the real controller synchronously, with fake process I/O.
    yield app
    app.stop()


def test_retry_keeps_settings_and_rejects_replaced_recording(svc, tmp_path):
    source = tmp_path / "New Recording 4.m4a"
    source.write_bytes(b"original recording")
    original = svc.add_file(source, {"language": "en", "device": "cuda"})
    assert svc.add_file(source) is None  # An active recording must not be queued twice.
    svc.store.update(original["job_id"], status="failed")
    svc.settings.language = "zh"
    retried = svc.retry_job(original["job_id"])
    assert retried["job_id"] == original["job_id"]
    assert json.loads(retried["options_json"]) == json.loads(original["options_json"])
    svc.store.update(original["job_id"], status="failed")
    source.write_bytes(b"different recording at the same path")
    with pytest.raises(ValueError, match="changed"):
        svc.retry_job(original["job_id"])
    assert svc.add_file(source)["job_id"] != original["job_id"]
    source.unlink()
    with pytest.raises(FileNotFoundError):
        svc.retry_job(original["job_id"])


def test_watcher_skips_archive_and_waits_for_finished_recording(svc, tmp_path, monkeypatch):
    settings = svc.settings
    settings.watch_dir = str(tmp_path)
    settings.stable_seconds = 1
    settings.min_file_size_kb = 0
    (tmp_path / "archive.m4a").write_bytes(b"old")
    ready_events = []

    def on_event(event, payload):
        if event == "ready":
            ready_events.append(payload)

    svc.on_event = on_event
    observer = SimpleNamespace(schedule=lambda *a, **k: None, start=lambda: None,
                               stop=lambda: None, join=lambda **k: None)
    monkeypatch.setattr(service, "Observer", lambda: observer)
    monkeypatch.setattr(service.FileWatcher, "_tick_loop", lambda self: None)
    watcher = svc.watcher
    watcher.start()
    try:
        now = time.time() + 2
        watcher.rescan()
        watcher.tick_once(now)
        assert ready_events == []
        assert svc.store.queued() == []
        source = tmp_path / "recording.m4a"
        source.write_bytes(b"start")
        watcher.rescan()
        source.write_bytes(b"still recording")
        watcher.tick_once(now)
        assert ready_events == []
        assert svc.store.queued() == []
        watcher.tick_once(now + 0.5)
        assert ready_events == []
        assert svc.store.queued() == []
        original_open = open
        locked = True

        def open_recording(path, mode="r", *args, **kwargs):
            if Path(path) == source and mode == "r+b" and locked:
                raise PermissionError("sharing violation")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", open_recording)
        watcher.tick_once(now + 2)
        assert ready_events == []
        assert svc.store.queued() == []
        locked = False
        watcher.tick_once(now + 4)
        assert [event["path"] for event in ready_events] == [str(source.resolve())]
        assert [row["source_path"] for row in svc.store.queued()] == [str(source.resolve())]
        watcher.rescan()
        watcher.tick_once(now + 6)
        assert [event["path"] for event in ready_events] == [str(source.resolve())]
        assert len(svc.store.queued()) == 1
    finally:
        watcher.stop()


def test_restart_recovers_work_but_not_cancelled_or_completed_jobs(svc, tmp_path):
    jobs = {}
    for status in ("queued", "running", "cancel_requested", "completed"):
        source = tmp_path / f"{status}.m4a"
        source.write_bytes(b"recording")
        row = svc.store.create_if_new(source)
        svc.store.update(row["job_id"], status=status, retry_count=4)
        jobs[status] = row["job_id"]
    svc.start()
    assert {row["job_id"] for row in svc.store.queued()} == {jobs["queued"], jobs["running"]}
    assert svc.store.get(jobs["cancel_requested"])["status"] == "cancelled"
    assert svc.store.get(jobs["completed"])["status"] == "completed"
    svc.store.update(jobs["running"], status="running")
    svc.store.recover_interrupted()
    assert svc.store.get(jobs["running"])["status"] == "failed"


def test_cancellation_stops_worker_and_ignores_late_results(svc, tmp_path, monkeypatch):
    source = tmp_path / "cancel.m4a"
    source.write_bytes(b"audio")
    row = svc.store.create_if_new(source)
    proc = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
    killed = []
    monkeypatch.setattr(service, "_terminate_process_tree", lambda p: killed.append(p))

    def events():
        svc.worker.cancel_active()
        yield '@@EVENT {"event":"completed","output_path":"stale.md"}'
        yield '@@EVENT {"event":"failed","error":"late error"}'

    proc.stdout = events()
    svc.worker._proc = proc
    svc.worker._active = row
    monkeypatch.setattr(svc.worker, "_ensure_server_running", lambda: proc)
    svc.worker._run_one_server(row)
    result = svc.store.get(row["job_id"])
    assert killed == [proc]
    assert result["status"] == "cancelled"
    assert result["output_path"] == ""
    assert result["error"] == ""


@pytest.mark.parametrize("outcome", ["missing-output", "crash-with-old-output", "completed"])
def test_worker_never_claims_false_success(svc, tmp_path, monkeypatch, outcome):
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"audio")
    row = svc.store.create_if_new(source, {"language": "en", "device": "cuda"})
    output = tmp_path / "transcripts" / "result.md"
    output.parent.mkdir(exist_ok=True)
    if outcome != "missing-output":
        output.write_text("saved transcript", encoding="utf-8")
    messages = [
        '@@EVENT {"event":"failed","job_id":"previous-job","error":"stale"}',
    ]
    if outcome != "crash-with-old-output":
        messages.append("@@EVENT " + json.dumps({
            "event": "completed", "job_id": row["job_id"], "output_path": str(output),
        }))
    proc = SimpleNamespace(stdin=io.StringIO(), stdout=iter(messages), poll=lambda: 1)
    monkeypatch.setattr(svc.worker, "_ensure_server_running", lambda: proc)
    svc.worker._run_one_server(row)
    result = svc.store.get(row["job_id"])
    assert result["status"] == ("completed" if outcome == "completed" else "failed")
    if outcome != "missing-output":
        assert output.read_text(encoding="utf-8") == "saved transcript"
    sent = json.loads(proc.stdin.getvalue())
    assert (sent["job_id"], sent["language"], sent["device"]) == (row["job_id"], "en", "cuda")


def test_service_saves_to_current_settings_path(svc, tmp_path):
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["transcript_dir"] == str(tmp_path / "transcripts")
    svc.settings.language = "ja"
    svc.settings.save()
    assert service.AppSettings.load().language == "ja"


def test_audio_cleanup_preserves_all_unowned_and_active_files(svc, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    removable = set()
    for status in ("completed", "completed_with_warning", "failed", "cancelled",
                   "queued", "running", "converting", "transcribing", "diarizing", "cancel_requested"):
        folder = tmp_path / status
        folder.mkdir()
        source = folder / "same.m4a"
        source.write_bytes(b"recording")
        row = svc.store.create_if_new(source)
        svc.store.update(row["job_id"], status=status)
        stem = f"same_{source_fingerprint(source)}_16k"
        for suffix in (".wav", ".wav.part", ".part.wav"):
            name = stem + suffix
            (cache / name).write_bytes(b"audio")
            if status in ("completed", "completed_with_warning", "failed", "cancelled"):
                removable.add(name)
        if status == "completed":
            (cache / f"{stem}.json").write_bytes(b"recognition cache")
    for name in ("same_16k.wav", "same_16k.part.wav", "untracked_16k.wav",
                 "untracked_16k.wav.part", "saved.md"):
        (cache / name).write_bytes(b"must survive")
    before = {p.name: p.read_bytes() for p in cache.iterdir()}
    result = svc.clear_audio_cache()
    after = {p.name: p.read_bytes() for p in cache.iterdir()}
    assert after == {name: data for name, data in before.items() if name not in removable}
    assert result["deleted_count"] == len(removable)


def test_watcher_redetection_never_resurrects_finished_recording(svc, tmp_path):
    watched = []
    for status in ("completed", "failed", "cancelled"):
        source = tmp_path / f"finished-{status}.m4a"
        source.write_bytes(b"finished recording")
        row = svc.add_file(source)
        svc.store.update(row["job_id"], status=status, output_path="result.md")
        watched.append((source, status, row["job_id"]))
    queued_before = len(svc.worker._queue)
    for source, status, job_id in watched:
        svc._event("ready", {"path": str(source), "size": source.stat().st_size})
        kept = svc.store.get(job_id)
        assert kept["status"] == status
        assert kept["output_path"] == "result.md"
    assert svc.store.queued() == []
    assert len(svc.worker._queue) == queued_before
