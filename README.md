# Meeting Transcriber

A personal Windows tool that turns OBS meeting recordings, iPhone Voice Memos, and other audio/video into Markdown with timestamps and speaker labels. Transcripts are kept for reference and agent context. Recognition runs locally; initial model downloads require internet access. The tool does not summarize, rewrite, or infer speaker names.

[中文说明](README.zh.md)

## Use

- **Right-click:** in Explorer, right-click an audio or video file and choose 转录 (on Windows 11 it is under "Show more options"). The file is queued, and the tray starts if it is not running. Run `setup_context_menu.ps1` once to add the entry.
- **Tray menu:** right-click the tray icon to pick files, cancel the current task, toggle the watcher, change the watch folder, save the HuggingFace token, or clear the audio cache. Left-click opens the task panel in Obsidian.
- **OBS recordings:** with the watcher on, a finished new recording in the watch folder is not transcribed right away. A notification asks first; click it, or choose 转录 in the tray's 新录屏 menu or the Obsidian panel, to queue it. Recordings already present at startup are ignored.
- **Obsidian panel:** the plugin in `obsidian-plugin/` shows the current task, new recordings, and recent tasks. It cancels and retries tasks. For completed tasks, open the raw transcript, copy a `/transcript` command containing its full path, or associate an existing organized note. Run the copied command in your vault's OpenCode session with your existing `transcript` custom command; the plugin does not run an agent in the background. Raw transcripts stay in `transcripts/`. Associated note paths follow vault renames and moves. Audio and video files inside the vault also get a 转录 entry in their context menu.
- **Agents / CLI:** call `transcribe.py`. It uses the same pipeline as the tray. Success exits with code 0; failure exits with a nonzero code.

A Windows notification appears when a task ends; clicking it opens the Obsidian panel. Output names retain source fingerprints to distinguish replaced files such as repeated `New Recording 4` recordings. A retry reuses the task's original settings and the current token; the source recording must still exist and match the original file version, so import a replaced recording as a new task.

The default pipeline includes speaker diarization. Save a HuggingFace token from the tray menu or the plugin settings and accept the terms for [speaker-diarization-3.1](https://hf.co/pyannote/speaker-diarization-3.1) and [segmentation-3.0](https://hf.co/pyannote/segmentation-3.0). Without a token or valid diarization cache, or if loading or diarization fails or returns no speaker turns, the task fails and does not publish a new Markdown transcript.

Per-task options such as title, language, speaker counts, and terms are CLI-only (`--title`, `--language`, `--num-speakers`, `--max-speakers`, `--hotwords`, `--transcribe-only`); the tray and panel use the defaults in `config.py`. Keep the full pipeline for lectures with student interaction, seminars, and meetings. Speaker labels are numbers, not verified identities.

On an NVIDIA GPU, English speech is recognized with Parakeet TDT 0.6B v3, about 2.5x faster than Whisper; on a 69-minute lecture it kept every student question. With auto-detect, a small Whisper model first identifies the language from the opening minutes, and anything below 80% English confidence uses Whisper. Chinese and other languages, CPU runs, and jobs with terms (`--hotwords`) use Whisper, because Parakeet takes no prompt. The first Parakeet job downloads about 2.5 GB to `%LOCALAPPDATA%/SimpleVideoTranscriber/models/`. Set `PARAKEET_FOR_ENGLISH = False` in `config.py` to always use Whisper.

## Install and start

Requires Windows, Python 3.10+, and ffmpeg. `install.bat` checks Python, attempts to install missing ffmpeg through winget, and installs `requirements.txt` into `.venv`. Then run `start.bat`.

To start the tray at login:

```powershell
powershell -ExecutionPolicy Bypass -File setup_autostart.ps1
```

Append `-Uninstall` to remove autostart. This script manages autostart; `config.py` does not.

To add the Explorer right-click entry (current-user registry only, no admin rights; append `-Uninstall` to remove it):

```powershell
powershell -ExecutionPolicy Bypass -File setup_context_menu.ps1
```

Obsidian plugin: in `obsidian-plugin/`, run `npm install` and `npm run deploy`, which copies the build into `E:/obsidian-vault/.obsidian/plugins/meeting-transcriber` (set `VAULT_PLUGIN_DIR` for another vault), then enable Meeting Transcriber under Community plugins. The tray publishes its port and secret as `api_port` and `api_token` in `%LOCALAPPDATA%/SimpleVideoTranscriber/`, and the plugin reads both; change the port with `API_PORT` in `config.py`. Web pages cannot read those files, so they cannot queue files.

The panel polls every 2 seconds while work is queued or running and every 15 seconds when idle or unavailable. Polling pauses while the panel or document is hidden and resumes when shown. Requests have a 10-second deadline that closes the connection. Timed-out actions are not automatically retried: check task state before submitting again. Authentication and response errors are shown separately from connection failures.

## Command line

Use the Python environment containing the dependencies, such as `.venv\Scripts\python.exe` after running the installer:

```powershell
python transcribe.py "lecture.m4a" --language en
python transcribe.py "meeting.mp4" --num-speakers 3 --hotwords "vLLM,KV Cache"
python transcribe.py "solo-lecture.m4a" --transcribe-only
python transcribe.py "meeting.mp4" --diarize-only
```

`--diarize-only` reuses recognition results matching the current recognition settings; matching diarization results are also reused. Keep recognition caches when retrying failures. Overrides include `--model large-v3`, `--device cpu`, `--max-speakers 3`, and `--output-dir ./transcripts`. The HuggingFace token is the one saved from the tray or plugin, otherwise the `HF_TOKEN` environment variable.

## Files and settings

- `transcripts/`: raw results used by the separate organization workflow. The plugin does not move, rewrite, or delete these files. Markdown is fully written before replacing the destination; write failures preserve the existing file.
- `cache/`: intermediate audio and recognition, diarization, and merged results. The tray's 清理音频缓存 removes only audio associated with terminal jobs, preserving JSON and Markdown.
- `%LOCALAPPDATA%/SimpleVideoTranscriber/`: tray settings, the job database, text logs, the local API port and secret (`api_port`, `api_token`), and the Parakeet model.

CLI and tray tasks cannot process the same recording version concurrently. If another task is using it, transcription fails with an audio-cache-in-use error; retry after that task finishes. Clearing the audio cache skips WAVs in use.

`config.py` supplies the defaults for every task, and CLI arguments override them. `settings.json` holds only the tray's watch folder and whether it is watching. The default Whisper model is `large-v3-turbo`, with `large-v3` also available, for jobs that use Whisper; the default device is `auto`. The watch folder defaults to the user's Videos directory and can be changed from the tray menu or defaulted through `MEETING_TRANSCRIBER_WATCH_DIR`.

Transcription settings are captured when a task is queued. Missing tokens fail early unless a valid diarization cache exists. Matching recognition and diarization caches bypass audio extraction and model execution.

The watcher waits for roughly 15 seconds of stable file size and checks whether the file can be opened. This is a recording-completion heuristic; paused recordings or transfers can trigger early. Source audio/video is neither moved nor deleted, and media links are not added automatically.
