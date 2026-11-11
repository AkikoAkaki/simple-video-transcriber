import { spawn } from "child_process";
import { access } from "fs/promises";
import { dirname, join } from "path";
import { FileSystemAdapter, Notice, Plugin, PluginSettingTab, Setting, TAbstractFile, TFile } from "obsidian";
import media from "../../media_extensions.json";
import { APP_DATA_DIR, ApiError, TranscriberApi } from "./api";
import { TaskView, VIEW_TYPE } from "./view";

interface Settings {
	/** Starts the tray when an action needs it and it is not running. */
	startScript: string;
	/** Finished jobs whose organizing command was copied; the panel no longer lists them. */
	copied: string[];
}

const DEFAULTS: Settings = {
	startScript: "I:\\Projects\\meeting-transcriber\\start.bat",
	copied: [],
};
const COPIED_LIMIT = 200;
const START_TIMEOUT_MS = 60000;
const EXTENSIONS = [...media.audio, ...media.video];
const MEDIA = new Set(EXTENSIONS.map(extension => extension.slice(1)));

/** The disk path of a file chosen in a file dialog or dropped onto the panel. */
function diskPath(file: File): string {
	const legacy = (file as File & { path?: string }).path;
	if (legacy) return legacy;
	try {
		const electron = require("electron") as { webUtils?: { getPathForFile(file: File): string } };
		return electron.webUtils?.getPathForFile(file) ?? "";
	} catch {
		return "";
	}
}

function formatSize(bytes: number): string {
	const units = ["B", "KB", "MB", "GB"];
	let size = bytes;
	let unit = 0;
	while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
	return unit ? `${size.toFixed(1)} ${units[unit]}` : `${size} B`;
}

export default class MeetingTranscriberPlugin extends Plugin {
	settings: Settings = { ...DEFAULTS, copied: [] };
	api = new TranscriberApi();
	private starting: Promise<boolean> | null = null;

	async onload() {
		const saved = await this.loadData();
		this.settings = {
			startScript: typeof saved?.startScript === "string" ? saved.startScript : DEFAULTS.startScript,
			copied: Array.isArray(saved?.copied) ? saved.copied.filter((id: unknown) => typeof id === "string") : [],
		};
		this.registerView(VIEW_TYPE, leaf => new TaskView(leaf, this));
		this.addRibbonIcon("mic", "转录任务", () => this.openPanel());
		this.addCommand({ id: "open-panel", name: "打开转录任务面板", callback: () => this.openPanel() });
		this.addCommand({ id: "choose-files", name: "转录文件…", callback: () => this.chooseFiles() });
		// The tray's left click opens obsidian://meeting-transcriber.
		this.registerObsidianProtocolHandler("meeting-transcriber", () => this.openPanel());
		this.registerEvent(this.app.workspace.on("file-menu", (menu, file) => {
			if (!this.isMedia(file)) return;
			menu.addItem(item => item.setTitle("转录").setIcon("mic").onClick(() => this.queueVaultFiles([file])));
		}));
		this.registerEvent(this.app.workspace.on("files-menu", (menu, files) => {
			const media = files.filter(file => this.isMedia(file));
			if (!media.length) return;
			menu.addItem(item => item.setTitle(`转录 ${media.length} 个文件`).setIcon("mic").onClick(() => this.queueVaultFiles(media)));
		}));
		this.addSettingTab(new TranscriberSettingTab(this));
	}

	async saveSettings() {
		await this.saveData(this.settings);
	}

	get transcriptDir(): string {
		return join(dirname(this.settings.startScript), "transcripts");
	}

	isCopied(jobId: string): boolean {
		return this.settings.copied.includes(jobId);
	}

	openPath(path: string) {
		const child = spawn("explorer.exe", [path], { stdio: "ignore", windowsHide: true });
		child.on("error", error => new Notice(`无法打开 ${path}：${error.message}`));
		child.unref();
	}

	async openTranscript(path: string) {
		try {
			await access(path);
			this.openPath(path);
		} catch (error) {
			new Notice(`原稿不存在或无法读取：${String(error)}`);
		}
	}

	/** Copies the organizing command; the job then leaves the panel. */
	async copyOrganizeCommand(jobId: string, path: string): Promise<boolean> {
		try {
			await access(path);
			await navigator.clipboard.writeText(`/transcript ${JSON.stringify(path)}`);
		} catch (error) {
			new Notice(`无法复制整理命令：${String(error)}`);
			return false;
		}
		new Notice("已复制整理命令");
		this.settings.copied = [...this.settings.copied.filter(id => id !== jobId), jobId].slice(-COPIED_LIMIT);
		await this.saveSettings();
		return true;
	}

	async openPanel() {
		const existing = this.app.workspace.getLeavesOfType(VIEW_TYPE)[0];
		const leaf = existing ?? this.app.workspace.getRightLeaf(false);
		if (!leaf) return;
		if (!existing) await leaf.setViewState({ type: VIEW_TYPE, active: true });
		await this.app.workspace.revealLeaf(leaf);
	}

	async refreshViews() {
		for (const leaf of this.app.workspace.getLeavesOfType(VIEW_TYPE)) {
			if (leaf.view instanceof TaskView) await leaf.view.refresh();
		}
	}

	/** Runs an API call, starting the tray first when it is not running. */
	async withService<T>(action: () => Promise<T>): Promise<T> {
		try {
			return await action();
		} catch (error) {
			if (!(error instanceof ApiError && error.kind === "connection")) throw error;
		}
		if (!await this.startService()) throw new Error("转录服务启动超时");
		return action();
	}

	/** Starts the tray and waits until it answers; concurrent callers share one start. */
	private startService(): Promise<boolean> {
		this.starting ??= (async () => {
			new Notice("正在启动转录服务…");
			const child = spawn("cmd.exe", ["/c", this.settings.startScript], { detached: true, stdio: "ignore", windowsHide: true });
			child.on("error", error => new Notice(`无法启动转录服务：${error.message}`));
			child.unref();
			const deadline = Date.now() + START_TIMEOUT_MS;
			while (Date.now() < deadline) {
				await new Promise(resolve => window.setTimeout(resolve, 1000));
				try {
					await this.api.state();
					return true;
				} catch {
					// Still starting.
				}
			}
			return false;
		})().finally(() => { this.starting = null; });
		return this.starting;
	}

	async queuePaths(paths: string[]) {
		if (!paths.length) return;
		try {
			const { queued } = await this.withService(() => this.api.queue(paths));
			new Notice(queued.length ? `已加入转录队列：${queued.length} 个文件` : "这些文件已经在队列里或转录过了");
		} catch (error) {
			new Notice(`无法加入转录队列：${(error as Error).message}`);
		}
		await this.openPanel();
		await this.refreshViews();
	}

	chooseFiles() {
		const input = document.body.createEl("input", { type: "file", attr: { multiple: "", accept: EXTENSIONS.join(",") } });
		input.hide();
		input.onchange = () => {
			const paths = Array.from(input.files ?? [], diskPath).filter(Boolean);
			input.remove();
			void this.queuePaths(paths);
		};
		input.oncancel = () => input.remove();
		input.click();
	}

	/** Media files among those dropped onto the panel. */
	droppedPaths(files: FileList): string[] {
		return Array.from(files, diskPath).filter(path => MEDIA.has(path.split(".").pop()?.toLowerCase() ?? ""));
	}

	private isMedia(file: TAbstractFile): file is TFile {
		return file instanceof TFile && MEDIA.has(file.extension.toLowerCase());
	}

	private async queueVaultFiles(files: TFile[]) {
		const adapter = this.app.vault.adapter;
		if (!(adapter instanceof FileSystemAdapter)) return;
		await this.queuePaths(files.map(file => adapter.getFullPath(file.path)));
	}

	async clearAudioCache() {
		try {
			const { deleted_count, reclaimed_bytes } = await this.withService(() => this.api.clearCache());
			new Notice(`已删除 ${deleted_count} 个音频缓存，释放 ${formatSize(reclaimed_bytes)}`);
		} catch (error) {
			new Notice(`清理失败：${(error as Error).message}`);
		}
	}
}

class TranscriberSettingTab extends PluginSettingTab {
	constructor(private readonly plugin: MeetingTranscriberPlugin) {
		super(plugin.app, plugin);
	}

	display() {
		const { containerEl } = this;
		containerEl.empty();
		new Setting(containerEl)
			.setName("启动脚本")
			.setDesc("需要转录服务时用它启动托盘；托盘空闲 3 分钟后自动退出。")
			.addText(text => text.setValue(this.plugin.settings.startScript).onChange(async value => {
				this.plugin.settings.startScript = value.trim();
				await this.plugin.saveSettings();
			}));
		let token = "";
		new Setting(containerEl)
			.setName("HuggingFace Token")
			.setDesc("说话人分离需要。保存在转录程序里，不写进 Obsidian。")
			.addText(text => {
				text.inputEl.type = "password";
				text.onChange(value => (token = value.trim()));
			})
			.addButton(button => button.setButtonText("保存").onClick(async () => {
				if (!token) return;
				try {
					await this.plugin.withService(() => this.plugin.api.setToken(token));
					new Notice("Token 已保存");
				} catch (error) {
					new Notice(`保存失败：${(error as Error).message}`);
				}
			}));
		new Setting(containerEl)
			.setName("音频缓存")
			.setDesc("只删除已结束任务的 WAV，识别缓存和转录稿不受影响。")
			.addButton(button => button.setButtonText("清理").onClick(() => this.plugin.clearAudioCache()));
		new Setting(containerEl)
			.setName("日志")
			.addButton(button => button.setButtonText("打开日志文件夹").onClick(() => this.plugin.openPath(join(APP_DATA_DIR, "logs"))));
	}
}
