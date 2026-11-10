"""Protect transcript content, caches, and publication across failures."""
import gc
import io
import sys
import weakref
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import config
import paths
import transcribe
from pipeline import transcript
from pipeline.audio import convert_to_wav
from pipeline.diarization import diarization_cache_params
from pipeline.errors import TranscriptionError
from pipeline.job import run_job
from pipeline.recognition import Recognition
from pipeline.reporting import ConsoleReporter, Reporter
from pipeline.stage_cache import StageCache
from protocol import JobOptions, JobRequest, parse_event


def _job_paths(source, output_dir=None, title=""):
    return paths.job_paths(source, output_dir or config.TRANSCRIPT_DIR, paths.source_fingerprint(source), title)


def _request(source, output_dir=None, token="test-token", job_id="job", **options):
    return JobRequest(job_id=job_id, input_path=str(source), output_dir=str(output_dir or config.TRANSCRIPT_DIR),
                      options=JobOptions(device="cpu", **options), token=token)


def _fake_ffmpeg(command, **kwargs):
    Path(command[-1]).write_bytes(b"w" * 2048)
    return SimpleNamespace(returncode=0, stderr="")


def _segment(text, *words):
    return {"start": words[0][1], "end": words[-1][2], "text": text,
            "words": [{"word": word, "start": start, "end": end} for word, start, end in words]}


def test_failed_conversion_cannot_publish_partial_audio(tmp_path, monkeypatch):
    source = tmp_path / "source.m4a"
    source.write_bytes(b"original audio")
    output = tmp_path / "audio.wav"

    def convert(command, **kwargs):
        Path(command[-1]).write_bytes(b"w" * 2048)
        assert not output.exists()
        return SimpleNamespace(returncode=1, stderr="conversion interrupted")

    monkeypatch.setattr("subprocess.run", convert)
    with pytest.raises(TranscriptionError):
        convert_to_wav(source, output, Reporter())
    assert not output.exists()
    assert source.read_bytes() == b"original audio"


def test_merge_results_splits_words_when_speaker_changes_mid_segment():
    whisper_segments = [_segment("Hello world yes", ("Hello", 0.0, 1.0), (" world", 1.1, 2.0), (" yes", 2.2, 3.0))]
    speaker_turns = [
        {"start": 0.0, "end": 2.1, "speaker": "SPEAKER_A"},
        {"start": 2.1, "end": 4.0, "speaker": "SPEAKER_B"},
    ]

    result = transcript.merge_results(whisper_segments, speaker_turns)

    assert [(item["speaker"], item["text"]) for item in result] == [
        ("SPEAKER_A", "Hello world"),
        ("SPEAKER_B", "yes"),
    ]


def test_merge_results_preserves_original_cjk_and_subword_spacing():
    whisper_segments = [_segment("你又有 cost integration", ("你", 0.0, 0.3), ("又", 0.3, 0.6), ("有", 0.6, 0.9),
                                 (" cost", 0.9, 1.3), (" integr", 1.3, 1.8), ("ation", 1.8, 2.2))]
    speaker_turns = [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_A"}]

    result = transcript.merge_results(whisper_segments, speaker_turns)

    assert len(result) == 1
    assert result[0]["text"] == whisper_segments[0]["text"]


def test_merge_results_falls_back_without_dropping_segments_missing_words():
    whisper_segments = [
        _segment("有时间戳", ("有时间戳", 0.0, 1.0)),
        {"start": 1.2, "end": 2.0, "text": "没有时间戳"},
    ]
    speaker_turns = [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_A"}]

    result = transcript.merge_results(whisper_segments, speaker_turns)

    assert len(result) == 1
    assert "有时间戳" in result[0]["text"]
    assert "没有时间戳" in result[0]["text"]


class FakeWhisper:
    def transcribe(self, *args, **kwargs):
        segment = SimpleNamespace(start=0.0, end=2.0, text="Lecture content.", words=[])
        return iter([segment]), SimpleNamespace(duration=2.0, language="en", language_probability=1.0)


class FakePipeline:
    def to(self, device):
        return self

    def __call__(self, *args, **kwargs):
        return SimpleNamespace(itertracks=lambda **_: iter([
            (SimpleNamespace(start=0.0, end=2.0), None, "SPEAKER_00")]))


def _stub_engines(monkeypatch, whisper=FakeWhisper, pipeline=FakePipeline):
    monkeypatch.setattr("faster_whisper.WhisperModel", lambda *a, **k: whisper())
    monkeypatch.setattr("pyannote.audio.Pipeline.from_pretrained", lambda *a, **k: pipeline())


def _stub_job_audio(tmp_path, monkeypatch):
    source = tmp_path / "lecture.m4a"
    source.write_bytes(b"audio")
    monkeypatch.setattr("subprocess.run", _fake_ffmpeg)
    monkeypatch.setattr("faster_whisper.audio.decode_audio",
                        lambda *a, **k: np.zeros(32000, dtype=np.float32))
    _stub_engines(monkeypatch)
    return source


def test_cli_and_server_produce_identical_transcripts(tmp_path, monkeypatch):
    source = _stub_job_audio(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["transcribe.py", str(source), "--device", "cpu", "--language", "en"])
    transcribe.main()
    output = _job_paths(source).output_md
    cli_text = output.read_text(encoding="utf-8")
    output.unlink()
    monkeypatch.setattr(sys, "stdin", io.StringIO(_request(source, language="en").to_line() + "\n"))
    transcribe.run_server()
    assert output.read_text(encoding="utf-8") == cli_text
    assert "SPEAKER_00" in cli_text
    assert "Lecture content." in cli_text


def test_cached_job_skips_audio_and_models(tmp_path, monkeypatch):
    source = tmp_path / "New Recording 4.m4a"
    source.write_bytes(b"source")
    out = tmp_path / "out"
    job_paths = _job_paths(source, out)
    StageCache(job_paths.whisper_json, "whisper", **Recognition("large-v3", "cpu", "en", "").cache_params()).write(
        [{"start": 0.0, "end": 2.0, "text": "Lecture content."}], total_sec=5.0)
    StageCache(job_paths.diarize_json, "diarization", **diarization_cache_params(None, None)).write(
        [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}])

    def unnecessary(*args, **kwargs):
        raise AssertionError("Cached job must not convert audio or touch models")

    monkeypatch.setattr("subprocess.run", unnecessary)
    monkeypatch.setattr("faster_whisper.WhisperModel", unnecessary)
    monkeypatch.setattr("pyannote.audio.Pipeline.from_pretrained", unnecessary)
    run_job(_request(source, out, token="", model="large-v3", language="en", title="CSC290: Lecture 1"), ConsoleReporter())
    output = _job_paths(source, out, "CSC290: Lecture 1").output_md
    text = output.read_text(encoding="utf-8")
    assert text.startswith("# CSC290: Lecture 1\n")
    assert "00:00:05 (5s)" in text
    assert "Whisper large-v3 " in text
    assert ":" not in output.name

    def require_new_recognition(*args, **kwargs):
        raise RuntimeError("New language requires recognition")

    monkeypatch.setattr("subprocess.run", _fake_ffmpeg)
    monkeypatch.setattr("faster_whisper.WhisperModel", require_new_recognition)
    with pytest.raises(TranscriptionError, match="New language requires recognition"):
        run_job(_request(source, out, model="large-v3", language="zh"), ConsoleReporter())
    source.write_bytes(b"replacement recording")
    assert _job_paths(source, out, "CSC290: Lecture 1").output_md != output


def test_markdown_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    output = tmp_path / "lecture.md"
    output.write_text("archived", encoding="utf-8")

    def fail_replace(path, target):
        assert Path(target).read_text(encoding="utf-8") == "archived"
        assert path.read_text(encoding="utf-8") == "new transcript"
        raise PermissionError("file is open")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(PermissionError):
        paths.write_atomic(output, "new transcript")
    assert output.read_text(encoding="utf-8") == "archived"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("failure", ["missing_token", "model_load", "inference", "empty"])
def test_diarization_failure_keeps_transcript_and_recognition_cache(tmp_path, monkeypatch, capsys, failure):
    source = _stub_job_audio(tmp_path, monkeypatch)
    token = "" if failure == "missing_token" else "test-token"
    if not token:
        def unexpected_conversion(*args, **kwargs):
            raise AssertionError("Missing credentials must fail before expensive work")
        monkeypatch.setattr("subprocess.run", unexpected_conversion)

    class Pipeline(FakePipeline):
        def __call__(self, *args, **kwargs):
            if failure == "inference":
                raise RuntimeError("diarization failed")
            return SimpleNamespace(itertracks=lambda **kwargs: iter([]))

    def load(*args, **kwargs):
        if failure == "model_load":
            raise RuntimeError("model load failed")
        return Pipeline()

    monkeypatch.setattr("pyannote.audio.Pipeline.from_pretrained", load)
    job_paths = _job_paths(source)
    job_paths.output_md.parent.mkdir(parents=True)
    job_paths.output_md.write_text("Archived transcript", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(_request(source, token=token, job_id="failure-job").to_line() + "\n"))
    transcribe.run_server()
    events = [event for line in capsys.readouterr().out.splitlines() if (event := parse_event(line))]
    assert events[-1]["event"] == "failed"
    assert events[-1]["job_id"] == "failure-job"
    assert token or "HuggingFace token" in events[-1]["error"]
    assert not any(e["event"] == "completed" for e in events)
    assert job_paths.output_md.read_text(encoding="utf-8") == "Archived transcript"
    assert job_paths.whisper_json.is_file() == bool(token)
    assert not job_paths.diarize_json.exists()


def test_out_of_memory_stage_releases_resident_models_then_retries(tmp_path, monkeypatch):
    source = _stub_job_audio(tmp_path, monkeypatch)
    resident = weakref.WeakSet()
    whisper_loads = []

    class Whisper(FakeWhisper):
        def transcribe(self, *args, **kwargs):
            if len(whisper_loads) == 1:
                raise RuntimeError("CUDA failed with error out of memory")
            return super().transcribe(*args, **kwargs)

    def load_whisper(*args, **kwargs):
        gc.collect()
        assert not whisper_loads or not resident, "Retry started while earlier models still occupy memory"
        whisper_loads.append(None)
        model = Whisper()
        resident.add(model)
        return model

    def load_pipeline(*args, **kwargs):
        pipeline = FakePipeline()
        resident.add(pipeline)
        return pipeline

    monkeypatch.setattr("faster_whisper.WhisperModel", load_whisper)
    monkeypatch.setattr("pyannote.audio.Pipeline.from_pretrained", load_pipeline)
    run_job(_request(source), ConsoleReporter())
    text = _job_paths(source).output_md.read_text(encoding="utf-8")
    assert "Lecture content." in text and "SPEAKER_00" in text
    assert len(whisper_loads) == 2
