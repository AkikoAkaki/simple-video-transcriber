# Simple Video Transcriber

自动转录会议录像并识别说话人 — 完全本地运行，无需上传任何内容。

[English](README.md)

---

## 安装

### Windows

1. [下载 zip 包](https://github.com/AkikoAkaki/simple-video-transcriber/releases) 并解压
2. 双击 `install.bat`
3. 双击 `start.bat`

### macOS

1. [下载 zip 包](https://github.com/AkikoAkaki/simple-video-transcriber/releases) 并解压
2. 双击 `install.command`
3. 双击 `start.command`

---

## 使用

1. 首次启动时会弹出引导面板，按步骤注册免费 HuggingFace Token（2 分钟，仅需一次）
2. 将视频拖入上方区域（或点击选择文件）
3. 点击 **Transcribe**（开始转录）
4. 完成后点击 **Open transcript →** 查看结果

输出格式可选：Markdown（`.md`）、SRT 字幕（`.srt`）、纯文本（`.txt`）。

---

## 输出示例

```markdown
### [00:00:00 – 00:00:16] SPEAKER_00
我们先来看看你最近在做什么。

### [00:00:16 – 00:01:10] SPEAKER_01
能看到我的屏幕吗？我跑了一下布局实验……
```

---

## 性能参考

RTX 4060 (8 GB 显存) 测试，25 分钟会议：

| 步骤 | 时间 |
|------|------|
| 音频转换 | ~10 秒 |
| Whisper 转录 | ~8 分钟 |
| 说话人分离 | ~12 分钟 |

无 GPU 时预计慢 5–10 倍。硬件较弱可在设置中选 `medium` 模型。

---

## 常见问题

**没有 GPU 能用吗？**
能，CPU 可以运行，只是比较慢。启动时会提示。

**选哪个模型？**
推荐默认的 `large-v3` 以获得最佳质量。硬件有限可选 `medium` 或 `small`。

**识别语言不对？**
在 GUI 中手动选择语言（自动检测 / English / 中文 / 日本語 …）。

**输出没有说话人标签？**
需要设置 HuggingFace Token。没有 Token 仍可转录，只是不标注说话人。

---

## 系统要求

- Windows 10+ 或 macOS 12+
- Python 3.10+（安装脚本会自动安装）
- ffmpeg（安装脚本会自动安装）

