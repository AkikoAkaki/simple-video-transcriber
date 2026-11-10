import { spawn } from "child_process";
import { access } from "fs/promises";
import { FileSystemAdapter, FuzzySuggestModal, Notice, Plugin, PluginSettingTab, Setting, TAbstractFile, TFile } from "obsidian";
import media from "../../media_extensions.json";
import { TranscriberApi } from "./api";
import { TaskView, VIEW_TYPE } from "./view";

interface Settings {
	/** Started when the panel finds no running tray. */
	startScript: string;
	/** job id -> vault path explicitly associated by the user; legacy archive links remain usable. */
	archived: Record<string, string>;
}

const DEFAULTS: Settings = {
	startScript: "I:\\Projects\\meeting-transcriber\\start.bat",
	archived: {},
};
const MEDIA = new Set([...media.audio, ...media.video].map(extension => extension.slice(1)));

export default class MeetingTranscriberPlugin extends Plugin {
	settings: Settings = { ...DEFAULTS, archived: {} };
	api = new TranscriberApi();

	async onload() {
		const saved = await this.loadData();
		const links: unknown = saved?.archived;
		this.settings = {
			startScript: typeof saved?.startScript === "string" ? saved.startScript : DEFAULTS.startScript,
			archived: typeof links === "object" && links !== null && !Array.isArray(links)
				? Object.fromEntries(Object.entries(links).filter(([, path]) => typeof path === "string")) : {},
		};
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
		this.registerEvent(this.app.vault.on("rename", (file, oldPath) => {
			void this.updateNoteLinks(oldPath, file.path).catch(error => new Notice(`保存关联失败：${String(error)}`));
		}));
		this.registerEvent(this.app.vault.on("delete", file => {
			void this.updateNoteLinks(file.path, null).catch(error => new Notice(`保存关联失败：${String(error)}`));
		}));
		this.addSettingTab(new TranscriberSettingTab(this));
	}

	async saveSettings() {
		await this.saveData(this.settings);
	}

	private async updateNoteLinks(oldPath: string, newPath: string | null) {
		let changed = false;
		for (const [id, path] of Object.entries(this.settings.archived)) {
			if (path !== oldPath && !path.startsWith(`${oldPath}/`)) continue;
			if (newPath === null) delete this.settings.archived[id];
			else this.settings.archived[id] = newPath + path.slice(oldPath.length);
			changed = true;
		}
		if (changed) {
			await this.saveSettings();
			void this.refreshViews();
		}
	}

	async refreshViews() {
		for (const leaf of this.app.workspace.getLeavesOfType(VIEW_TYPE)) {
			if (leaf.view instanceof TaskView) await leaf.view.refresh();
		}
	}

	async openTranscript(path: string) {
		try {
			await access(path);
			const child = spawn("explorer.exe", [path], { stdio: "ignore", windowsHide: true });
			child.on("error", error => new Notice(`无法打开原稿：${error.message}`));
			child.unref();
		} catch (error) {
			new Notice(`原稿不存在或无法读取：${String(error)}`);
		}
	}

	async copyOrganizeCommand(path: string): Promise<boolean> {
		try {
			await access(path);
			await navigator.clipboard.writeText(`/transcript ${JSON.stringify(path)}`);
			new Notice("已复制整理命令，请在 vault 的 OpenCode 会话中运行；原稿会保留");
			return true;
		} catch (error) {
			new Notice(`无法复制整理命令：${String(error)}`);
			return false;
		}
	}

	/** The organized note the user associated with this job, while it still exists. */
	linkedNote(jobId: string): TFile | null {
		const path = this.settings.archived[jobId];
		const file = path ? this.app.vault.getAbstractFileByPath(path) : null;
		return file instanceof TFile ? file : null;
	}

	chooseLinkedNote(jobId: string) {
		new NoteModal(this, jobId).open();
	}

	async linkNote(jobId: string, file: TFile) {
		this.settings.archived[jobId] = file.path;
		await this.saveSettings();
		await this.refreshViews();
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

class NoteModal extends FuzzySuggestModal<TFile> {
	constructor(private readonly plugin: MeetingTranscriberPlugin, private readonly jobId: string) {
		super(plugin.app);
		this.setPlaceholder("选择已经整理好的笔记；关联不会修改文件");
	}
	getItems() { return this.app.vault.getMarkdownFiles(); }
	getItemText(file: TFile) { return file.path; }
	async onChooseItem(file: TFile) {
		try {
			await this.plugin.linkNote(this.jobId, file);
			new Notice(`已关联 ${file.path}`);
		} catch (error) {
			new Notice(`关联失败：${String(error)}`);
		}
	}
}
