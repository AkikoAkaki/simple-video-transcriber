# Simple Video Transcriber

Transcribe meeting recordings with speaker labels — fully local, nothing leaves your computer.

[中文说明](README.zh.md)

---

## Install

### Windows

1. [Download the zip](https://github.com/AkikoAkaki/simple-video-transcriber/releases) and extract it
2. Double-click `install.bat`
3. Double-click `start.bat`

### macOS

1. [Download the zip](https://github.com/AkikoAkaki/simple-video-transcriber/releases) and extract it
2. Double-click `install.command`
3. Double-click `start.command`

---

## Use

1. The first time, a setup panel will guide you through getting a free HuggingFace token (2 minutes, one-time)
2. Drag a video onto the drop zone (or click to browse)
3. Click **Transcribe**
4. When done, click **Open transcript →** to view the result

Output formats: Markdown (`.md`), SRT subtitles (`.srt`), or plain text (`.txt`).

---

## Example output

```markdown
### [00:00:00 – 00:00:16] SPEAKER_00
Let's first look at what you've been working on.

### [00:00:16 – 00:01:10] SPEAKER_01
Can you see my screen? So I ran the layout experiment...
```

---

## Performance

Tested on RTX 4060 (8 GB VRAM), 25-minute meeting:

| Step | Time |
|------|------|
| Audio conversion | ~10s |
| Whisper transcription | ~8 min |
| Speaker diarization | ~12 min |

On CPU, expect 5–10× longer. Use the `medium` model for faster results on weaker hardware.

---

## FAQ

**Do I need a GPU?**
No — CPU works, just slower. The GUI will show a note if no GPU is detected.

**Which model size should I use?**
Start with `large-v3` for best quality. Choose `medium` or `small` in Settings if your hardware is limited.

**The detected language is wrong.**
Set your language in the GUI (Auto-detect / English / 中文 / 日本語 …).

**No speaker labels in the output?**
Make sure you have a HuggingFace token set. Without one, transcription still works but without speaker names.

---

## Requirements

- Windows 10+ or macOS 12+
- Python 3.10+ (installed by `install.bat` / `install.command`)
- ffmpeg (installed by the install script)

---

<details>
<summary>Advanced usage (CLI)</summary>

```bash
# Transcribe a file
python transcribe.py path/to/meeting.mp4

# Force language
python transcribe.py meeting.mp4 --language zh

# Skip diarization (no token needed)
python transcribe.py meeting.mp4 --transcribe-only

# Re-run diarization on cached Whisper output
python transcribe.py meeting.mp4 --diarize-only

# Output formats
python transcribe.py meeting.mp4 --output-format srt
python transcribe.py meeting.mp4 --output-format txt

# Override settings one-off
python transcribe.py meeting.mp4 --model medium --device cpu --max-speakers 3 --output-dir ./my-transcripts
```

### Watcher (auto-transcribe)

```bash
python watch.py            # watches inbox/ for new videos
python watch.py --dry-run  # detect files but don't transcribe
```

Files dropped into `inbox/` are automatically transcribed when they stop changing for 10 seconds. Adjust in `config.py`.

</details>

<details>
<summary>Configuration reference</summary>

Edit `config.py` to change defaults:

| Setting | Default | Description |
|---------|---------|-------------|
| `WATCH_DIR` | `inbox/` | Folder the watcher monitors |
| `TRANSCRIPT_DIR` | `transcripts/` | Output folder |
| `CACHE_DIR` | `cache/` | Intermediate files (safe to delete) |
| `WHISPER_MODEL` | `large-v3` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `LANGUAGE` | `None` | `"en"` / `"zh"` / `"ja"` / … — `None` = auto-detect |
| `DEVICE` | `auto` | `"cuda"` / `"cpu"` / `"auto"` |
| `MAX_SPEAKERS` | `None` | Integer for known speaker count |
| `ORGANIZE_BY_YEAR` | `True` | Move videos into `YYYY/` after transcription |
| `STABLE_SECONDS` | `10` | Wait before auto-transcribing |
| `MIN_FILE_SIZE_KB` | `100` | Ignore files smaller than this |
| `WATCH_EXTENSIONS` | `{".mp4", …}` | File types to watch |

</details>

---

## License

MIT
