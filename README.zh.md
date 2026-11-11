# Meeting Transcriber

个人 Windows 转录工具：把 OBS 会议录屏、iPhone 语音备忘录以及其他音视频转为带时间戳和说话人标注的 Markdown，供长期归档、查阅和 agent 获取上下文。识别在本机运行；首次使用需要联网下载模型，不对原稿自动摘要、改写或推测说话人姓名。

[English](README.md)

## 使用

- **右键转录**：在资源管理器里右键音视频文件，选“转录”（Windows 11 在“显示更多选项”里），文件直接进入队列；托盘没运行时会自动启动。运行一次 `setup_context_menu.ps1` 添加这个菜单。
- **托盘**：只在有任务时运行，队列空闲 3 分钟后自动退出（转录进程同时释放模型）；右键转录和 Obsidian 插件需要时会重新启动它。运行期间右键托盘图标可以选文件转录、取消当前任务、设置 HuggingFace Token、清理音频缓存。
- **Obsidian 面板**：`obsidian-plugin/` 里的插件可以独立完成日常操作：点“转录文件…”或把录音拖进面板加入队列，托盘没运行时自动启动。面板显示当前任务和一列历史（完成、失败、取消），失败的任务显示原因并可重试。点文件名打开原稿；复制 `/transcript` 整理命令后，该任务从面板消失。托盘退出后，面板读取它留下的 `history.json` 继续显示历史。设置页可以保存 Token、清理音频缓存、打开日志文件夹。在 vault 的 OpenCode 会话中运行复制的命令，需要已有的 `transcript` 自定义命令；插件不会后台启动 agent。原稿留在 `transcripts/`。vault 里的音视频文件也可以右键“转录”。
- **agent / 命令行**：调用 `transcribe.py`，与托盘使用同一转录流程。成功退出码为 0，失败为非零。

转完会弹 Windows 通知，点击打开 Obsidian 面板。输出文件名保留源文件指纹，重复出现的 `New Recording 4` 会按不同文件版本区分。重试沿用原任务设置并使用当前 Token，需要原音视频仍在且未被替换；改了内容的同名录音要重新导入。

默认流程包含说话人分离，需要在托盘菜单或插件设置里保存 HuggingFace Token，并接受 [speaker-diarization-3.1](https://hf.co/pyannote/speaker-diarization-3.1) 和 [segmentation-3.0](https://hf.co/pyannote/segmentation-3.0) 的使用条款。缺少 Token 且无可用分离缓存、模型加载失败、分离运行失败或没有分离结果时，任务标记为失败，不发布本次 Markdown。

标题、语言、人数和术语这类单次选项只在命令行提供（`--title`、`--language`、`--num-speakers`、`--max-speakers`、`--hotwords`、`--transcribe-only`）；托盘和面板使用 `config.py` 的默认值。包含学生互动的 lecture、seminar 或多人会议保持默认的完整流程。说话人标签是编号，不代表已核实身份。

在 NVIDIA GPU 上，英文语音用 Parakeet TDT 0.6B v3 识别，比 Whisper 快约 2.5 倍；在一场 69 分钟的 lecture 上，学生提问都保留了。语言为自动检测时，先用一个小 Whisper 模型根据开头几分钟判断语言，英文置信度低于 80% 的仍用 Whisper。中文和其他语言、CPU 运行，以及指定了术语（`--hotwords`）的任务都用 Whisper，因为 Parakeet 不支持提示词。第一次用 Parakeet 会下载约 2.5 GB 到 `%LOCALAPPDATA%/SimpleVideoTranscriber/models/`。在 `config.py` 设 `PARAKEET_FOR_ENGLISH = False` 可始终使用 Whisper。

## 安装与启动

需要 Windows、Python 3.10+ 和 ffmpeg。`install.bat` 检查 Python、尝试通过 winget 安装缺失的 ffmpeg，并在 `.venv` 安装 `requirements.txt`。安装后用 `start.bat` 启动。

要登录后自动运行托盘服务：

```powershell
powershell -ExecutionPolicy Bypass -File setup_autostart.ps1
```

取消自启动时追加 `-Uninstall`。自启动由此脚本管理，不由 `config.py` 控制。

添加资源管理器右键菜单（只写当前用户的注册表，不需要管理员权限，取消时追加 `-Uninstall`）：

```powershell
powershell -ExecutionPolicy Bypass -File setup_context_menu.ps1
```

Obsidian 插件：在 `obsidian-plugin/` 运行 `npm install` 和 `npm run deploy`，产物会复制到 `E:/obsidian-vault/.obsidian/plugins/meeting-transcriber`（用环境变量 `VAULT_PLUGIN_DIR` 指定其他 vault），再在 Obsidian 的第三方插件里启用 Meeting Transcriber。托盘把端口和密钥写到 `%LOCALAPPDATA%/SimpleVideoTranscriber/` 的 `api_port` 和 `api_token`，插件读取这两个文件；端口在 `config.py` 的 `API_PORT` 修改。网页无法读取这些文件，所以不能借此往队列里加文件。

面板有排队或运行任务时每 2 秒查询，空闲时每 15 秒查询，服务退出后停止查询。面板或文档隐藏时暂停，重新显示时立即刷新。请求超过 10 秒会断开连接；超时的操作不会自动重试，请先查看任务状态再重新提交。鉴权和响应错误会与连接失败分开显示。

## 命令行

使用安装依赖的 Python；通过安装脚本配置的环境可调用 `.venv\Scripts\python.exe`：

```powershell
python transcribe.py "lecture.m4a" --language en
python transcribe.py "meeting.mp4" --num-speakers 3 --hotwords "vLLM,KV Cache"
python transcribe.py "solo-lecture.m4a" --transcribe-only
python transcribe.py "meeting.mp4" --diarize-only
```

`--diarize-only` 复用匹配当前识别参数的识别缓存；匹配的说话人分离缓存也会复用。失败后重试不需要删除识别缓存。可用 `--model large-v3`、`--device cpu`、`--max-speakers 3`、`--output-dir ./transcripts` 覆盖默认值。HuggingFace Token 使用托盘或插件保存的值，没有时读 `HF_TOKEN` 环境变量。

## 输出与配置

- `transcripts/`：原始转录稿，供独立的整理流程读取。插件不移动、改写或删除这些文件。Markdown 写入完整后才替换正式文件；写入失败保留已有文件。
- `cache/`：音频中间文件和识别、分离、合并结果。“清理音频缓存”只清理与已结束任务关联的音频缓存，保留 JSON 和 Markdown。
- `%LOCALAPPDATA%/SimpleVideoTranscriber/`：任务数据库、供插件读取的最近任务（`history.json`）、文本日志、本地接口的端口和密钥（`api_port`、`api_token`）和 Parakeet 模型。

命令行和托盘任务不能同时处理同一版本的录音。其他任务正在使用它时，转录会报音频缓存被占用，等该任务结束后再重试。清理音频缓存会跳过占用中的 WAV。

`config.py` 提供所有任务的默认设置，命令行参数覆盖对应默认值。走 Whisper 的任务默认用 `large-v3-turbo`，也可选 `large-v3`；设备默认为 `auto`。

任务入队时保存转录设置。缺少 Token 且没有可用分离缓存时提前失败；识别和分离缓存均匹配时，跳过音频提取和模型运行。

输出不会移动或删除原始音视频，也不会自动添加媒体链接。
