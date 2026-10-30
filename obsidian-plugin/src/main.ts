import { spawn } from "child_process";
import { FileSystemAdapter, Notice, Plugin, PluginSettingTab, Setting, TAbstractFile, TFile } from "obsidian";
import { TranscriberApi } from "./api";
import { TaskView, VIEW_TYPE } from "./view";

interface Settings {
	port: number;
	/** Started when the panel finds no running tray. */
	startScript: string;
	lastFolder: string;
	/** job id -> vault path of the archived transcript */
	archived: Record<string, string>;
}

const DEFAULTS: Settings = {
	port: 47321,
	startScript: "I:\\Projects\\meeting-transcriber\\start.bat",
	lastFolder: "",
	archived: {},
};
const MEDIA = new Set(["mp4", "m4a", "mov", "mkv", "webm", "mp3", "wav", "flac", "aac", "opus"]);

export default class MeetingTranscriberPlugin extends Plugin {
	settings: Settings = { ...DEFAULTS, archived: {} };
	api = new TranscriberApi(() => this.settings.port);

	async onload() {
		this.settings = { ...DEFAULTS, ...(await this.loadData()) };
		this.registerView(VIEW_TYPE, leaf => new TaskView(leaf, this));
		this.addRibbonIcon("mic", "转录任务", () => this.openPanel());
		this.addCommand({ id: "open-panel", name: "打开转录任务面板", callback: () => this.openPanel() });
		this.addCommand({ id: "start-service", name: "启动转录服务", callback: () => this.startService() });
		// The tray's left click opens obsidian://meeting-transcriber.
		this.registerObsidianProtocolHandler("meeting-transcriber", () => this.openPanel());
		this.registerEvent(this.app.workspace.on("file-menu", (menu, file) => {
			if (!this.isMedia(file)) return;
			menu.addItem(item => item.setTitle("转录").setIcon("mic").onClick(() => this.queue([file])));
		}));
		this.registerEvent(this.app.workspace.on("files-menu", (menu, files) => {
			const media = files.filter(file => this.isMedia(file));
			if (!media.length) return;
			menu.addItem(item => item.setTitle(`转录 ${media.length} 个文件`).setIcon("mic").onClick(() => this.queue(media)));
		}));
		this.addSettingTab(new TranscriberSettingTab(this));
	}

	async saveSettings() {
		await this.saveData(this.settings);
	}

	async openPanel() {
		const existing = this.app.workspace.getLeavesOfType(VIEW_TYPE)[0];
		const leaf = existing ?? this.app.workspace.getRightLeaf(false);
		if (!leaf) return;
		if (!existing) await leaf.setViewState({ type: VIEW_TYPE, active: true });
		await this.app.workspace.revealLeaf(leaf);
	}

	startService() {
		const child = spawn("cmd.exe", ["/c", this.settings.startScript], { detached: true, stdio: "ignore", windowsHide: true });
		child.on("error", error => new Notice(`无法启动转录服务：${error.message}`));
		child.unref();
		new Notice("正在启动转录服务…");
	}

	private isMedia(file: TAbstractFile): file is TFile {
		return file instanceof TFile && MEDIA.has(file.extension.toLowerCase());
	}

	private async queue(files: TFile[]) {
		const adapter = this.app.vault.adapter;
		if (!(adapter instanceof FileSystemAdapter)) return;
		try {
			const { queued } = await this.api.queue(files.map(file => adapter.getFullPath(file.path)));
			new Notice(queued.length ? `已加入转录队列：${queued.length} 个文件` : "这些文件已经在队列里或转录过了");
			await this.openPanel();
		} catch (error) {
			new Notice(`转录服务没有响应：${(error as Error).message}`);
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
			.setName("服务端口")
			.setDesc("和转录程序 config.py 里的 API_PORT 一致。")
			.addText(text => text.setValue(String(this.plugin.settings.port)).onChange(async value => {
				const port = Number(value);
				if (Number.isInteger(port) && port > 0) {
					this.plugin.settings.port = port;
					await this.plugin.saveSettings();
				}
			}));
		new Setting(containerEl)
			.setName("启动脚本")
			.setDesc("面板发现转录服务没有运行时，用它启动托盘。")
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
					await this.plugin.api.setToken(token);
					new Notice("Token 已保存");
				} catch (error) {
					new Notice(`保存失败：${(error as Error).message}`);
				}
			}));
	}
}
