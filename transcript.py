"""Turn recognized segments and speaker turns into a Markdown transcript."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from typography import join_display_text


UNKNOWN_SPEAKER = "[unknown]"
# Consecutive text from one speaker separated by at most this many seconds is one block.
MAX_GAP_SECONDS = 2.0


def format_time(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _speaker_for_interval(start: float, end: float, speaker_turns: list[dict], turn_idx: int):
    """Choose the speaker with the greatest overlap for one interval.

    ``turn_idx`` only moves forward, so callers pass intervals in time order.
    """
    num_turns = len(speaker_turns)
    while turn_idx < num_turns and speaker_turns[turn_idx]["end"] <= start:
        turn_idx += 1

    best_speaker = UNKNOWN_SPEAKER
    best_overlap = 0.0
    i = turn_idx
    while i < num_turns and speaker_turns[i]["start"] < end:
        turn = speaker_turns[i]
        overlap = max(0.0, min(end, turn["end"]) - max(start, turn["start"]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn["speaker"]
        i += 1
    return best_speaker, turn_idx


def _append_word_text(current: str, word: str) -> str:
    """Append the token exactly as faster-whisper emitted it.

    Word strings carry meaningful leading whitespace for English and usually
    omit it for CJK text. Inventing a separator corrupts both cases and can
    even split one English word into two pieces (for example, ``integr`` +
    ``ation``).
    """
    if not current:
        return word.strip()
    return current + word


def _merge_runs(items: list[dict], join: Callable[[dict, dict], str]) -> list[dict]:
    """Merge consecutive items of one speaker; ``join(run, item)`` returns the run's new text."""
    runs = []
    for item in items:
        run = runs[-1] if runs else None
        if run and run["speaker"] == item["speaker"] and item["start"] - run["end"] <= MAX_GAP_SECONDS:
            run["text"] = join(run, item)
            run.update({key: value for key, value in item.items() if key not in ("start", "text")})
        else:
            runs.append({**item, "text": item["text"].strip()})
    return runs


def _join_blocks(run: dict, item: dict) -> str:
    return join_display_text(run["text"], item["text"])


def _coalesce_short_speaker_runs(segments: list[dict], max_duration: float = 1.0) -> list[dict]:
    """Suppress only high-confidence, short diarization flicker.

    A short unknown block is reassigned only when its known neighbours agree.
    A known speaker block is reassigned under the same condition only when its
    displayed text is at most four non-space characters. Boundary unknown
    blocks may borrow their single known neighbour. All other changes remain
    untouched so real speaker switches are preserved.
    """
    work = [dict(segment) for segment in segments]
    for index, segment in enumerate(work):
        if segment["end"] - segment["start"] > max_duration:
            continue
        previous = work[index - 1] if index > 0 else None
        following = work[index + 1] if index + 1 < len(work) else None
        previous_speaker = previous["speaker"] if previous else None
        following_speaker = following["speaker"] if following else None
        compact_chars = len("".join(segment["text"].split()))

        if segment["speaker"] == UNKNOWN_SPEAKER:
            if previous_speaker and previous_speaker == following_speaker and previous_speaker != UNKNOWN_SPEAKER:
                segment["speaker"] = previous_speaker
            elif previous_speaker and not following and previous_speaker != UNKNOWN_SPEAKER:
                segment["speaker"] = previous_speaker
            elif following_speaker and not previous and following_speaker != UNKNOWN_SPEAKER:
                segment["speaker"] = following_speaker
        elif (compact_chars <= 4 and previous_speaker and
              previous_speaker == following_speaker and
              previous_speaker != segment["speaker"] and
              previous_speaker != UNKNOWN_SPEAKER):
            segment["speaker"] = previous_speaker
    return _merge_runs(work, _join_blocks)


def _merge_word_timestamps(whisper_segments: list[dict], speaker_turns: list[dict]) -> list[dict]:
    words = []
    turn_idx = 0
    for segment_index, segment in enumerate(whisper_segments):
        for word in segment["words"]:
            start, end = float(word["start"]), float(word["end"])
            speaker, turn_idx = _speaker_for_interval(start, end, speaker_turns, turn_idx)
            words.append({"start": start, "end": end, "text": word["word"],
                          "speaker": speaker, "segment": segment_index})

    def join_word(run: dict, word: dict) -> str:
        # Within one Whisper segment the word keeps its own spacing; a new
        # segment starts a new display block.
        if run["segment"] == word["segment"]:
            return _append_word_text(run["text"], word["text"])
        return join_display_text(run["text"], word["text"])

    merged = _merge_runs(words, join_word)
    for run in merged:
        del run["segment"]
    return merged


def merge_results(whisper_segments: list[dict], speaker_turns: list[dict]) -> list[dict]:
    """Label recognized text with speakers and merge it into display blocks."""
    # Word-level timestamps let a single Whisper segment contain multiple
    # speakers without assigning the entire sentence to one person.
    # A mixed cache (some segments with word timestamps, some without) cannot
    # be safely reconstructed word-by-word without dropping the latter. Use
    # the segment-level path for that case; it preserves all source text.
    if speaker_turns and whisper_segments and all(segment.get("words") for segment in whisper_segments):
        return _coalesce_short_speaker_runs(_merge_word_timestamps(whisper_segments, speaker_turns))

    labeled = []
    turn_idx = 0
    for seg in whisper_segments:
        speaker, turn_idx = _speaker_for_interval(seg["start"], seg["end"], speaker_turns, turn_idx)
        labeled.append({"start": seg["start"], "end": seg["end"], "text": seg["text"], "speaker": speaker})
    return _merge_runs(labeled, _join_blocks)


def generate_markdown(segments: list[dict], source_file: str, total_sec: float,
                      diarization: str | None, language: str | None,
                      *, title: str = "", model_name: str) -> str:
    """Render the transcript; ``diarization`` names the pipeline, or None when it was skipped."""
    lines = [
        f"# {title or 'Transcript'}", "",
        f"**Source**: {source_file}",
        f"**Duration**: {format_time(total_sec)} ({int(total_sec)}s)",
        f"**Model**: Whisper {model_name}  |  Language: {language or 'auto-detect'}",
        f"**Diarization**: {diarization or 'disabled'}",
        "", "---", "",
    ]

    if diarization:
        spk_stats = defaultdict(lambda: {"dur": 0.0, "count": 0})
        for s in segments:
            if s["speaker"] != UNKNOWN_SPEAKER:
                spk_stats[s["speaker"]]["dur"] += s["end"] - s["start"]
                spk_stats[s["speaker"]]["count"] += 1
        if spk_stats:
            lines += ["## Speakers", ""]
            for spk in sorted(spk_stats):
                lines.append(f"- **{spk}**: {format_time(spk_stats[spk]['dur'])} ({spk_stats[spk]['count']} segments)")
            lines += ["", "---", ""]

    lines += ["## Transcript", ""]
    for seg in segments:
        lines += [f"### [{format_time(seg['start'])} – {format_time(seg['end'])}] {seg['speaker']}", "", seg["text"], ""]
    return "\n".join(lines)
