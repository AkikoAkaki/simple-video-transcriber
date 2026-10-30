"""The local API must queue every posted recording exactly once, and nothing without the secret."""
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import api
import appdata
import service

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def test_api_queues_each_recording_once_and_only_with_the_secret(tmp_path):
    svc = service.BackgroundService(appdata.AppSettings(
        watch_dir=str(tmp_path / "watch"), transcript_dir=str(tmp_path / "out"), watcher_enabled=False))
    server = api.ApiServer(svc, port=0)
    server.start()

    def call(route, body=None, secret=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}{route}", method="GET" if body is None else "POST",
            data=None if body is None else json.dumps(body).encode(), headers={"Authorization": f"Bearer {secret}"})
        with _OPENER.open(request, timeout=5) as response:
            return json.loads(response.read())

    paths = [tmp_path / "first.m4a", tmp_path / "second.mp4", tmp_path / "notes.txt"]
    for path in paths:
        path.write_bytes(b"recording")
    posted = {"paths": [str(path) for path in paths + [paths[0]]]}
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            call("/api/jobs", posted, secret="guess")
        assert denied.value.code == 401 and svc.store.queued() == []
        secret = api.load_secret()
        assert len(call("/api/jobs", posted, secret)["queued"]) == 2
        assert sorted(Path(row["source_path"]).name for row in svc.store.queued()) == ["first.m4a", "second.mp4"]
        state = call("/api/state", secret=secret)
        assert sorted(Path(job["source_path"]).name for job in state["recent"]) == ["first.m4a", "second.mp4"]
    finally:
        server.stop()
        svc.stop()
