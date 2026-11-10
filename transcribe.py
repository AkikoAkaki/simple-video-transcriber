#!/usr/bin/env python3
"""Transcribe a video or audio file with speaker labels.

Usage:
  python transcribe.py <video>                    # full pipeline
  python transcribe.py <video> --language en      # force language
  python transcribe.py <video> --transcribe-only  # skip diarization
  python transcribe.py <video> --diarize-only     # reuse matching stage caches
  python transcribe.py --server                   # worker process the tray talks to over stdin/stdout

Output: transcripts/<filename>_<source-fingerprint>.md
"""

import argparse
import logging
import sys
import traceback
import warnings

# A GBK console must not crash on the checkmarks and arrows in progress lines.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("pyannote").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")
warnings.filterwarnings("ignore", message=r".*std\(\).*degrees of freedom.*")
warnings.filterwarnings("ignore", message=".*resume_download.*")

import config
from appdata import TokenStore
from pipeline.errors import TranscriptionError
from pipeline.job import run_job
from pipeline.models import release_models
from pipeline.reporting import ConsoleReporter, EventReporter
from protocol import SERVER_READY, JobOptions, JobRequest


def run_server() -> None:
    print(SERVER_READY, flush=True)
    for line in iter(sys.stdin.readline, ""):
        try:
            request = JobRequest.from_line(line)
        except Exception as e:
            print(f"ERROR: Server loop error: {e}", file=sys.stderr, flush=True)
            continue
        report = EventReporter(request.job_id)
        try:
            run_job(request, report)
        except Exception as e:
            if not isinstance(e, TranscriptionError):
                traceback.print_exc(file=sys.stdout)
            release_models()
            report.failed(str(e))


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe a video or audio file with speaker labels")
    parser.add_argument("input", nargs="?", help="Path to video/audio file")
    parser.add_argument("--server", action="store_true", help="Run in persistent server mode")
    parser.add_argument("--language", default=None, help="Language code (en/zh/ja/...). Default: auto-detect")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--transcribe-only", action="store_true",
                      help="Run Whisper only, skip diarization (no HF token needed)")
    mode.add_argument("--diarize-only", action="store_true",
                      help="Require matching Whisper cache; reuse matching diarization cache")
    parser.add_argument("--model", default=None, choices=list(config.SUPPORTED_MODELS),
                        help="Whisper model size (large-v3-turbo/large-v3). Overrides config.py")
    parser.add_argument("--device", default=None, help="Compute device (auto/cuda/cpu). Overrides config.py")
    parser.add_argument("--max-speakers", default=None, type=int, help="Maximum number of speakers")
    parser.add_argument("--num-speakers", default=None, type=int, help="Exact number of speakers, when known")
    parser.add_argument("--hotwords", default=None, help="Comma-separated names or technical terms to bias Whisper")
    parser.add_argument("--output-dir", default=None,
                        help="Directory for output .md file. Overrides config.py TRANSCRIPT_DIR")
    parser.add_argument("--title", default="", help="Optional transcript title and filename")
    return parser


def _run_cli() -> None:
    parser = _build_cli_parser()
    args = parser.parse_args()

    if args.server:
        run_server()
        return

    if not args.input:
        parser.error("the following arguments are required: input (unless running with --server)")

    overrides = {"model": args.model, "device": args.device, "language": args.language,
                 "max_speakers": args.max_speakers, "num_speakers": args.num_speakers,
                 "hotwords": args.hotwords}
    options = JobOptions.parse({
        "title": args.title,
        "mode": "transcribe" if args.transcribe_only else "diarize" if args.diarize_only else "full",
        **{name: value for name, value in overrides.items() if value is not None},
    })
    run_job(JobRequest(job_id="", input_path=args.input, output_dir=args.output_dir or str(config.TRANSCRIPT_DIR),
                       options=options, token="" if args.transcribe_only else TokenStore().get()),
            ConsoleReporter())


def main() -> None:
    try:
        _run_cli()
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"\nFATAL: {e}", flush=True)
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)


if __name__ == "__main__":
    main()
