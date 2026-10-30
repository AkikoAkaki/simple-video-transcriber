import { existsSync } from "fs";
import { basename } from "path";
import { ItemView, Notice, TFile, TFolder, WorkspaceLeaf } from "obsidian";
import type { Job, ServiceState } from "./api";
import { moveIntoVault, pickFolder, suggestFolder, transcriptTitle } from "./archive";
import type MeetingTranscriberPlugin from "./main";

export const VIEW_TYPE = "meeting-transcriber-tasks";
const POLL_MS = 2000;

const STATUS: Record<string, string> = {
	queued: "排队中",
	running: "转录中",
	cancel_requested: "正在取消",
	completed: "完成",
	completed_with_warning: "完成（有警告）",
	failed: "失败",
	cancelled: "已取消",
};
const RETRYABLE = new Set(["failed", "cancelled", "completed_with_warning"]);
const DONE = new Set(["completed", "completed_with_warning"]);

function when(iso: string): string {
	if (!iso) return "";
	const date = new Date(iso);
	const pad = (n: number) => String(n).padStart(2, "0");
	return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export class TaskView extends ItemView {
	private sections: Record<string, HTMLElement> = {};
	// Each section re-renders only when its data changes, so a click is never lost to a redraw.
	private shown: Record<string, string> = {};

	constructor(leaf: WorkspaceLeaf, private readonly plugin: MeetingTranscriberPlugin) {
		super(leaf);
	}

	getViewType() {
		return VIEW_TYPE;
	}

	getDisplayText() {
		return "转录任务";
	}

	getIcon() {
		return "mic";
	}

	async onOpen() {
		const root = this.contentEl;
		root.empty();
		root.addClass("mt-view");
		for (const name of ["service", "active", "offered", "recent", "watcher"]) {
			this.sections[name] = root.createDiv({ cls: `mt-section mt-${name}` });
		}
		this.registerInterval(window.setInterval(() => void this.refresh(), POLL_MS));
		await this.refresh();
	}

	async refresh() {
		let state: ServiceState | null = null;
		try {
			state = await this.plugin.api.state();
		} catch {
			// The tray is not running; render the offline state below.
		}
		this.draw("service", state ? "up" : "down", el => this.drawService(el, state !== null));
		if (!state) {
			for (const name of ["active", "offered", "recent", "watcher"]) this.draw(name, "", el => el.empty());
			return;
		}
		const archived = this.plugin.settings.archived;
		this.draw("active", JSON.stringify(state.active), el => this.drawActive(el, state.active));
		this.draw("offered", JSON.stringify(state.offered), el => this.drawOffered(el, state.offered));
		const recent = state.recent.filter(job => job.job_id !== state.active?.job_id);
		this.draw("recent", JSON.stringify([recent, archived]), el => this.drawRecent(el, recent));
		this.draw("watcher", JSON.stringify(state.watcher), el => this.drawWatcher(el, state.watcher));
	}

	private draw(name: string, key: string, render: (el: HTMLElement) => void) {
		if (this.shown[name] === key) return;
		this.shown[name] = key;
		const el = this.sections[name];
		el.empty();
		render(el);
	}

	private async act(action: () => Promise<unknown>) {
		try {
			await action();
		} catch (error) {
			new Notice(`转录服务：${(error as Error).message}`);
		}
		await this.refresh();
	}

	private drawService(el: HTMLElement, up: boolean) {
		const head = el.createDiv({ cls: "mt-head" });
		head.createSpan({ cls: `mt-dot ${up ? "is-up" : "is-down"}` });
		head.createSpan({ text: up ? "转录服务运行中" : "转录服务没有运行" });
		if (!up) {
			const button = el.createEl("button", { text: "启动转录服务", cls: "mod-cta" });
			button.onclick = () => this.plugin.startService();
		}
	}

	private drawActive(el: HTMLElement, job: Job | null) {
		if (!job) return;
		el.createEl("h6", { text: "当前任务" });
		el.createDiv({ cls: "mt-name", text: basename(job.source_path) });
		el.createDiv({ cls: "mt-muted", text: job.message || STATUS[job.status] || job.status });
		const bar = el.createEl("progress", { cls: "mt-progress" });
		bar.max = 1;
		bar.value = job.progress ?? 0;
		const cancel = el.createEl("button", { text: "取消" });
		cancel.onclick = () => this.act(() => this.plugin.api.cancel());
	}

	private drawOffered(el: HTMLElement, paths: string[]) {
		if (!paths.length) return;
		el.createEl("h6", { text: `新录屏（${paths.length}）` });
		for (const path of paths) {
			const row = el.createDiv({ cls: "mt-row" });
			row.createDiv({ cls: "mt-name", text: basename(path) });
			const actions = row.createDiv({ cls: "mt-actions" });
			actions.createEl("button", { text: "转录", cls: "mod-cta" }).onclick = () =>
				this.act(() => this.plugin.api.accept(path));
			actions.createEl("button", { text: "忽略" }).onclick = () => this.act(() => this.plugin.api.dismiss(path));
		}
	}

	private drawRecent(el: HTMLElement, jobs: Job[]) {
		el.createEl("h6", { text: "最近任务" });
		if (!jobs.length) el.createDiv({ cls: "mt-muted", text: "还没有任务。在资源管理器里右键录音选“转录”，或从托盘菜单添加。" });
		for (const job of jobs) {
			const row = el.createDiv({ cls: `mt-row is-${job.status}` });
			const info = row.createDiv({ cls: "mt-info" });
			info.createDiv({ cls: "mt-name", text: basename(job.source_path) });
			const detail = `${STATUS[job.status] ?? job.status} · ${when(job.finished_at || job.created_at)}`;
			info.createDiv({ cls: "mt-muted", text: detail });
			if (job.error) info.createDiv({ cls: "mt-error", text: job.error, attr: { title: job.error } });
			const actions = row.createDiv({ cls: "mt-actions" });
			this.addJobActions(actions, job);
		}
	}

	private addJobActions(actions: HTMLElement, job: Job) {
		const archivedPath = this.plugin.settings.archived[job.job_id];
		const archived = archivedPath ? this.app.vault.getAbstractFileByPath(archivedPath) : null;
		if (archived instanceof TFile) {
			actions.createEl("button", { text: "打开" }).onclick = () => this.app.workspace.getLeaf(false).openFile(archived);
		} else if (DONE.has(job.status) && job.output_path && existsSync(job.output_path)) {
			actions.createEl("button", { text: "归档…", cls: "mod-cta" }).onclick = () => this.archive(job);
		} else if (DONE.has(job.status)) {
			actions.createSpan({ cls: "mt-muted", text: "转录稿已移走" });
		}
		if (RETRYABLE.has(job.status)) {
			actions.createEl("button", { text: "重试" }).onclick = () => this.act(() => this.plugin.api.retry(job.job_id));
		}
	}

	private async archive(job: Job) {
		const settings = this.plugin.settings;
		const suggested = suggestFolder(this.app, transcriptTitle(job.output_path));
		const last = this.app.vault.getFolderByPath(settings.lastFolder);
		const first = [...new Set([suggested, last])].filter((f): f is TFolder => f !== null);
		const folder = await pickFolder(this.app, first);
		if (!folder) return;
		try {
			const file = await moveIntoVault(this.app, job.output_path, folder);
			settings.archived[job.job_id] = file.path;
			settings.lastFolder = folder.path;
			await this.plugin.saveSettings();
			await this.app.workspace.getLeaf(true).openFile(file);
			new Notice(`已归档到 ${file.path}`);
		} catch (error) {
			new Notice(`归档失败：${(error as Error).message}`);
		}
		await this.refresh();
	}

	private drawWatcher(el: HTMLElement, watcher: ServiceState["watcher"]) {
		const label = el.createEl("label", { cls: "mt-toggle" });
		const box = label.createEl("input", { type: "checkbox" });
		box.checked = watcher.enabled;
		box.onchange = () => this.act(() => this.plugin.api.setWatching(box.checked));
		label.createSpan({ text: " 监听 OBS 录制目录，发现新录屏时询问是否转录" });
		el.createDiv({ cls: "mt-muted", text: watcher.dir });
	}
}
