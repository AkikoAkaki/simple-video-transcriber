"""Protect transcript content, caches, and publication across failures."""
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import transcribe


def test_failed_conversion_cannot_publish_partial_audio(tmp_path, monkeypatch):
    source = tmp_path / "source.m4a"
    source.write_bytes(b"original audio")
    output = tmp_path / "audio.wav"
    fail = True

    def convert(command, **kwargs):
        Path(command[-1]).write_bytes(b"w" * 2048)
        assert not output.exists()
        return SimpleNamespace(returncode=1 if fail else 0, stderr="conversion interrupted")

    monkeypatch.setattr(transcribe.subprocess, "run", convert)
    with pytest.raises(transcribe.TranscriptionError):
        transcribe.convert_to_wav(source, output)
    assert not output.exists()
    assert source.read_bytes() == b"original audio"
    fail = False
    transcribe.convert_to_wav(source, output)
    assert output.read_bytes() == b"w" * 2048


def test_merge_results_splits_words_when_speaker_changes_mid_segment():

    whisper_segments = [{
        "start": 0.0,
        "end": 4.0,
        "text": "Hello world yes",
        "words": [
            {"start": 0.0, "end": 1.0, "word": "Hello"},
            {"start": 1.1, "end": 2.0, "word": " world"},
            {"start": 2.2, "end": 3.0, "word": " yes"},
        ],
    }]
    speaker_turns = [
        {"start": 0.0, "end": 2.1, "speaker": "SPEAKER_A"},
        {"start": 2.1, "end": 4.0, "speaker": "SPEAKER_B"},
    ]

    result = transcribe.merge_results(whisper_segments, speaker_turns)

    assert [(item["speaker"], item["text"]) for item in result] == [
        ("SPEAKER_A", "Hello world"),
        ("SPEAKER_B", "yes"),
    ]


def test_merge_results_preserves_original_cjk_and_subword_spacing():

    whisper_segments = [{
        "start": 0.0,
        "end": 3.0,
        "text": "你又有 cost integration",
        "words": [
            {"start": 0.0, "end": 0.3, "word": "你"},
            {"start": 0.3, "end": 0.6, "word": "又"},
            {"start": 0.6, "end": 0.9, "word": "有"},
            {"start": 0.9, "end": 1.3, "word": " cost"},
            {"start": 1.3, "end": 1.8, "word": " integr"},
            {"start": 1.8, "end": 2.2, "word": "ation"},
        ],
    }]
    speaker_turns = [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_A"}]

    result = transcribe.merge_results(whisper_segments, speaker_turns)

    assert len(result) == 1
    assert result[0]["text"] == whisper_segments[0]["text"]


def test_merge_results_falls_back_without_dropping_segments_missing_words():

    whisper_segments = [
        {
            "start": 0.0,
            "end": 1.0,
            "text": "有时间戳",
            "words": [{"start": 0.0, "end": 1.0, "word": "有时间戳"}],
        },
        {"start": 1.2, "end": 2.0, "text": "没有时间戳"},
    ]
    speaker_turns = [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_A"}]

    result = transcribe.merge_results(whisper_segments, speaker_turns)

    assert len(result) == 1
    assert "有时间戳" in result[0]["text"]
    assert "没有时间戳" in result[0]["text"]


def _stub_job_audio(tmp_path, monkeypatch):

    source = tmp_path / "lecture.m4a"
    source.write_bytes(b"audio")
    monkeypatch.setattr(transcribe.config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(transcribe.config, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(transcribe, "convert_to_wav", lambda *_: None)
    monkeypatch.setattr(transcribe, "get_wav_duration", lambda _: 2.0)
    monkeypatch.setattr(transcribe, "_resolve_device", lambda *args: "cpu")
    monkeypatch.setattr(transcribe, "clear_diarize_pipeline", lambda: None)
    monkeypatch.setattr(transcribe, "_release_whisper_before_diarization", lambda: None)

    def recognize(_wav, cache, _language, _hotwords, key, **kwargs):
        segments = [{"start": 0.0, "end": 2.0, "text": "Lecture content."}]
        transcribe._write_stage_cache(cache, "whisper", key, segments)
        return segments

    monkeypatch.setattr(transcribe, "run_whisper", recognize)
    return source


def test_cli_and_server_produce_identical_transcripts(tmp_path, monkeypatch):

    source = _stub_job_audio(tmp_path, monkeypatch)
    class Pipeline:
        def __call__(self, *_args, **_kwargs):
            return SimpleNamespace(itertracks=lambda **_: iter([
                (SimpleNamespace(start=0.0, end=2.0), None, "SPEAKER_00")]))

    monkeypatch.setattr(transcribe, "get_diarize_pipeline", lambda *_: Pipeline())
    monkeypatch.setattr(transcribe, "get_hf_token", lambda: "test-token")
    monkeypatch.setattr(sys, "argv", ["transcribe.py", str(source), "--device", "cpu", "--language", "en"])
    transcribe._main()
    output = transcribe.derive_paths(source)["output_md"]
    cli_text = output.read_text(encoding="utf-8")
    output.unlink()
    transcribe.run_job_from_json({
        "input_path": str(source), "output_dir": str(output.parent),
        "device": "cpu", "language": "en", "token": "test-token",
    })
    assert output.read_text(encoding="utf-8") == cli_text
    assert "SPEAKER_00" in cli_text
    assert "Lecture content." in cli_text


def test_cached_job_skips_audio_and_models_and_keeps_globals(tmp_path, monkeypatch):
    source = tmp_path / "New Recording 4.m4a"
    source.write_bytes(b"source")
    monkeypatch.setattr(transcribe.config, "CACHE_DIR", tmp_path / "cache")
    paths = transcribe.derive_paths(source, tmp_path / "out")
    key = transcribe._whisper_cache_key("large-v3", "cpu", "en", "", True)
    transcribe._write_stage_cache(paths["whisper_json"], "whisper", key,
        [{"start": 0.0, "end": 2.0, "text": "Lecture content."}], metadata={"total_sec": 5.0})
    transcribe._write_stage_cache(paths["diarize_json"], "diarization",
        transcribe._diarization_cache_key(None, None),
        [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}])

    def unnecessary(*args, **kwargs):
        raise AssertionError("Cached job must not convert audio or touch models")

    for name in ("convert_to_wav", "run_whisper", "run_diarization",
                 "clear_diarize_pipeline", "_release_whisper_before_diarization"):
        monkeypatch.setattr(transcribe, name, unnecessary)
    before = (transcribe.config.WHISPER_MODEL, transcribe.config.DEVICE, transcribe.config.TRANSCRIPT_DIR)
    transcribe.run_job_from_json({"input_path": str(source), "output_dir": str(tmp_path / "out"),
                                 "model": "large-v3", "device": "cpu", "language": "en",
                                 "title": "CSC290: Lecture 1"})
    output = transcribe.derive_paths(source, tmp_path / "out", "CSC290: Lecture 1")["output_md"]
    text = output.read_text(encoding="utf-8")
    assert text.startswith("# CSC290: Lecture 1\n")
    assert "00:00:05 (5s)" in text
    assert "Whisper large-v3 " in text
    assert ":" not in output.name
    assert before == (transcribe.config.WHISPER_MODEL, transcribe.config.DEVICE, transcribe.config.TRANSCRIPT_DIR)
    def require_new_recognition(*args, **kwargs):
        raise RuntimeError("New language requires recognition")
    monkeypatch.setattr(transcribe, "convert_to_wav", lambda *args: None)
    monkeypatch.setattr(transcribe, "clear_diarize_pipeline", lambda: None)
    monkeypatch.setattr(transcribe, "_release_whisper_before_diarization", lambda: None)
    monkeypatch.setattr(transcribe, "run_whisper", require_new_recognition)
    with pytest.raises(RuntimeError, match="New language requires recognition"):
        transcribe.run_job_from_json({"input_path": str(source), "output_dir": str(output.parent),
                                     "model": "large-v3", "device": "cpu", "language": "zh"})
    source.write_bytes(b"replacement recording")
    assert transcribe.derive_paths(source, output.parent, "CSC290: Lecture 1")["output_md"] != output


def test_markdown_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    output = tmp_path / "lecture.md"
    output.write_text("archived", encoding="utf-8")
    original_replace = Path.replace

    def fail_replace(path, target):
        assert Path(target).read_text(encoding="utf-8") == "archived"
        assert path.read_text(encoding="utf-8") == "new transcript"
        raise PermissionError("file is open")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(PermissionError):
        transcribe.write_markdown(output, "new transcript")
    assert output.read_text(encoding="utf-8") == "archived"
    assert list(tmp_path.iterdir()) == [output]
    monkeypatch.setattr(Path, "replace", original_replace)
    transcribe.write_markdown(output, "new transcript")
    assert output.read_text(encoding="utf-8") == "new transcript"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("failure", ["missing_token", "model_load", "inference", "empty"])
def test_diarization_failure_keeps_transcript_and_recognition_cache(tmp_path, monkeypatch, capsys, failure):
    source = _stub_job_audio(tmp_path, monkeypatch)
    token = "" if failure == "missing_token" else "test-token"
    if not token:
        def unexpected_conversion(*args):
            raise AssertionError("Missing credentials must fail before expensive work")
        monkeypatch.setattr(transcribe, "convert_to_wav", unexpected_conversion)

    class Pipeline:
        def __call__(self, *args, **kwargs):
            if failure == "inference":
                raise RuntimeError("diarization failed")
            return SimpleNamespace(itertracks=lambda **kwargs: iter([]))

    def load(*args):
        if failure == "model_load":
            raise RuntimeError("model load failed")
        return Pipeline()

    monkeypatch.setattr(transcribe, "get_diarize_pipeline", load)
    paths = transcribe.derive_paths(source)
    paths["output_md"].write_text("Archived transcript", encoding="utf-8")
    task = {"command": "transcribe", "job_id": "failure-job",
            "input_path": str(source), "output_dir": str(paths["output_md"].parent),
            "device": "cpu", "token": token}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(task) + "\n"))
    transcribe.run_server()
    events = [json.loads(line.removeprefix("@@EVENT "))
              for line in capsys.readouterr().out.splitlines() if line.startswith("@@EVENT ")]
    assert events[-1]["event"] == "failed"
    assert events[-1]["job_id"] == "failure-job"
    assert not any(e["event"] in {"completed", "completed_with_warning"} for e in events)
    assert paths["output_md"].read_text(encoding="utf-8") == "Archived transcript"
    assert paths["whisper_json"].is_file() == bool(token)
    assert not paths["diarize_json"].exists()


def test_batch_releases_models_before_loading_next_stage(tmp_path, monkeypatch):
    _stub_job_audio(tmp_path, monkeypatch)
    loaded = set()

    def recognize(*args, **kwargs):
        assert not loaded, "Previous model still occupies memory before Whisper"
        loaded.add("whisper")
        return [{"start": 0.0, "end": 2.0, "text": "Lecture content."}]

    def diarize(*args, **kwargs):
        assert not loaded, "Whisper still occupies memory before diarization"
        loaded.add("diarization")
        return [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}]

    monkeypatch.setattr(transcribe, "run_whisper", recognize)
    monkeypatch.setattr(transcribe, "run_diarization", diarize)
    monkeypatch.setattr(transcribe, "clear_diarize_pipeline", lambda: loaded.discard("diarization"))
    monkeypatch.setattr(transcribe, "_release_whisper_before_diarization", lambda: loaded.discard("whisper"))
    for name in ("first.m4a", "second.m4a"):
        source = tmp_path / name
        source.write_bytes(b"audio")
        transcribe.run_job_from_json({"input_path": str(source), "device": "cpu",
                                     "output_dir": str(tmp_path / "transcripts"), "token": "test-token"})
        assert "Lecture content." in transcribe.derive_paths(source)["output_md"].read_text(encoding="utf-8")
