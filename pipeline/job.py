"""Run one transcription: reuse what is cached, compute the rest, publish the Markdown."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Callable

from paths import JobPaths, SourceChangedError, audio_cache_lock, job_paths, source_fingerprint, write_atomic
from pipeline.audio import SharedAudio, convert_to_wav, get_wav_duration
from pipeline.diarization import PIPELINE_ID, diarization_cache_params, preload as preload_speakers, run_diarization
from pipeline.errors import TranscriptionError, is_out_of_memory
from pipeline.models import release_models, resolve_device
from pipeline.recognition import Recognition
from pipeline.reporting import BackgroundReporter, Reporter, log
from pipeline.stage_cache import StageCache
from pipeline.transcript import generate_markdown, merge_results
from protocol import JobOptions, JobRequest


def run_job(request: JobRequest, report: Reporter) -> None:
    input_path = Path(request.input_path).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    fingerprint = source_fingerprint(input_path)
    if request.source_fingerprint and fingerprint != request.source_fingerprint:
        raise SourceChangedError()
    paths = job_paths(input_path, Path(request.output_dir).resolve(), fingerprint, request.options.title)
    with audio_cache_lock(paths.wav):
        if source_fingerprint(input_path) != fingerprint:
            raise SourceChangedError()
        _transcribe(input_path, paths, request.options, request.token, report)


def _transcribe(input_path: Path, paths: JobPaths, options: JobOptions, token: str, report: Reporter) -> None:
    device = resolve_device(options.device)
    diarize = options.mode != "transcribe"
    recognition = Recognition(options.model, device, options.language, options.hotwords)
    recognition_cache = StageCache(paths.whisper_json, "whisper", **recognition.cache_params())
    speaker_cache = StageCache(paths.diarize_json, "diarization",
                               **diarization_cache_params(options.max_speakers, options.num_speakers))

    recognized = recognition_cache.read()
    if options.mode == "diarize" and recognized is None:
        raise FileNotFoundError(f"No cached Whisper result for {input_path.name}")
    diarized = speaker_cache.read() if diarize else None
    need_recognition = recognized is None
    need_speakers = diarize and diarized is None
    if need_speakers and not token:
        raise TranscriptionError("Speaker diarization requires a HuggingFace token. Save one from the tray or "
                                 f"plugin and accept the terms at https://hf.co/{PIPELINE_ID}.")
    if need_recognition or need_speakers:
        loaders = ([recognition.preload] if need_recognition else []) + (
            [lambda: preload_speakers(token, device)] if need_speakers else [])
        _preload(loaders)
        convert_to_wav(input_path, paths.wav, report)
    audio = SharedAudio(paths.wav)
    parallel = device == "cuda" and need_recognition and need_speakers

    def recognize():
        result = recognition.run(audio, report)
        return recognition_cache.write(result.segments, total_sec=result.total_sec, model_label=result.model_label)

    def label_speakers():
        turns = run_diarization(audio, token, num_speakers=options.num_speakers, max_speakers=options.max_speakers,
                                device=device, report=BackgroundReporter("diarization") if parallel else report)
        return speaker_cache.write(turns)

    stages = {}
    if need_recognition:
        stages["recognition"] = recognize
    else:
        report.stage("transcribing", 1.0, f"Loaded {len(recognized['result'])} recognized segments from cache")
    if need_speakers:
        stages["speakers"] = label_speakers
    elif diarize:
        report.stage("diarizing", 1.0, f"Loaded {len(diarized['result'])} speaker turns from cache")
    results = _run_stages(stages, parallel,
                          on_waiting=lambda: report.stage("diarizing", 0.0, "Finishing speaker diarization"))
    recognized = results.get("recognition", recognized)
    diarized = results.get("speakers", diarized)
    turns = diarized["result"] if diarized else []

    report.stage("merging", 0.0, "Merging transcript and speaker labels")
    segments = merge_results(recognized["result"], turns)

    # Caches written before duration was recorded carry none.
    total_sec = recognized.get("total_sec") or 0
    if total_sec <= 0 and paths.wav.is_file():
        total_sec = get_wav_duration(paths.wav)
    if total_sec <= 0 and segments:
        total_sec = segments[-1]["end"]

    report.stage("writing", 0.0, f"Writing {len(segments)} transcript blocks")
    content = generate_markdown(segments, input_path.name, total_sec, PIPELINE_ID if turns else None,
                                options.language, title=options.title,
                                model_label=recognized.get("model_label") or recognition.whisper_label)
    write_atomic(paths.output_md, content)
    report.completed(str(paths.output_md), f"✓ Done → {paths.output_md}")


def _preload(loaders: list[Callable[[], None]]) -> None:
    """Load the models while ffmpeg converts the audio, so the first job after a worker start does not
    wait for each in turn. A failed preload is ignored; the stage loads again and reports the error."""

    def load():
        for loader in loaders:
            with contextlib.suppress(Exception):
                loader()

    threading.Thread(target=load, name="model-preload", daemon=True).start()


def _run_stages(stages: dict[str, Callable[[], dict]], parallel: bool, on_waiting: Callable[[], None]) -> dict:
    """Run the stages, the first two at the same time when ``parallel``.

    Models stay loaded between jobs, so a stage that runs out of memory is retried once alone
    after every model is released. Any other failure is raised once the other stage has finished
    and cached its result. ``on_waiting`` runs when the first stage finishes before the second.
    """
    names = list(stages)
    results, errors = {}, {}

    def run(name):
        try:
            results[name] = stages[name]()
        except Exception as exc:
            errors[name] = exc

    if parallel:
        helper = threading.Thread(target=run, args=(names[1],), name=f"{names[1]}-stage", daemon=True)
        helper.start()
        run(names[0])
        if helper.is_alive() and names[0] not in errors:
            on_waiting()
        helper.join()
    else:
        for name in names:
            run(name)
            if name in errors and not is_out_of_memory(errors[name]):
                break
    for name in [name for name in names if is_out_of_memory(errors.get(name))]:
        log(f"[WARN] {name} ran out of memory ({errors.pop(name)}); retrying it alone")
        release_models()
        run(name)
    for name in names:
        if name in errors:
            raise errors[name]
    return results
