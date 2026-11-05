import { basename } from "path";
import { ItemView, Menu, Notice, TFile, WorkspaceLeaf, setIcon } from "obsidian";
import { ApiError, type Job, type ServiceState } from "./api";
import type MeetingTranscriberPlugin from "./main";

export const VIEW_TYPE = "meeting-transcriber-tasks";
const POLL_MS = 2000;
const IDLE_POLL_MS = 15000;
const LEAVE_MS = 160;
const DONE_LIMIT = 8;

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
const VIDEO = /\.(mp4|mkv|mov|avi|webm|flv)$/i;

/** Each worker stage reports its own 0–1 progress; map it onto one bar for the whole job. */
const STAGE_SPAN: Record<string, [number, number]> = {
	queued: [0, 0],
	starting: [0, 0.02],
	converting: [0.02, 0.1],
	loading_whisper: [0.1, 0.12],
	transcribing: [0.12, 0.85],
	diarizing: [0.85, 0.9],
	merging: [0.9, 0.95],
	writing: [0.95, 1],
	completed: [1, 1],
};
const STEPS = ["转换", "识别 · 分离", "合并写出"];
const STEP_OF: Record<string, number> = { converting: 0, loading_whisper: 1, transcribing: 1, diarizing: 1, merging: 2, writing: 2, completed: 2 };

function overall(job: Job): number {
	const span = STAGE_SPAN[job.stage];
	const p = Math.max(0, Math.min(1, job.progress ?? 0));
	return span ? span[0] + (span[1] - span[0]) * p : p;
}

function when(iso: string): string {
	if (!iso) return "";
	const date = new Date(iso);
	const pad = (n: number) => String(n).padStart(2, "0");
	return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function icon(parent: HTMLElement, name: string, cls = "mt-tile"): HTMLElement {
	const el = parent.createSpan({ cls });
	setIcon(el, name);
	return el;
}

function button(parent: HTMLElement, text: string, opts: { icon?: string; primary?: boolean; ghost?: boolean } = {}): HTMLButtonElement {
	const el = parent.createEl("button", { cls: `mt-btn${opts.primary ? " is-primary" : ""}${opts.ghost ? " is-ghost" : ""}` });
	if (opts.icon) setIcon(el.createSpan({ cls: "mt-btn-icon" }), opts.icon);
	el.createSpan({ text });
	return el;
}

function iconButton(parent: HTMLElement, name: string, label: string): HTMLButtonElement {
	const el = parent.createEl("button", { cls: "mt-icon-btn", attr: { "aria-label": label } });
	setIcon(el, name);
	return el;
}

export class TaskView extends ItemView {
	private sections: Record<string, HTMLElement> = {};
	// Each section re-renders only when its data changes, so a click is never lost to a redraw.
	private shown: Record<string, string> = {};
	// Cards seen in an earlier draw; only new ones play the entrance animation.
	private seen = new Set<string>();
	private primed = false;
	private activeId: string | null = null;
	private polling: number | null = null;
	private generation = 0;
	private refreshing = false;
	private opened = false;
	private refreshAgain = false;

	constructor(leaf: WorkspaceLeaf, private readonly plugin: MeetingTranscriberPlugin) {
		super(leaf);
		const visible = () => {
			this.stopPolling();
			if (this.opened && this.isVisible()) void this.refresh();
		};
		this.registerEvent(this.app.workspace.on("layout-change", visible));
		this.registerDomEvent(this.containerEl.ownerDocument, "visibilitychange", visible);
		this.register(() => this.halt());
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
		this.opened = true;
		this.generation++;
		this.shown = {};
		this.seen.clear();
		this.primed = false;
		this.activeId = null;
		const root = this.contentEl;
		root.empty();
		root.addClass("mt-view");
		for (const name of ["service", "active", "need", "done", "watcher"]) {
			this.sections[name] = root.createDiv({ cls: `mt-section mt-${name}` });
		}
		await this.refresh();
	}

	async onClose() {
		this.halt();
	}

	/** Stops polling and discards any response still in flight. */
	private halt() {
		this.opened = false;
		this.generation++;
		this.stopPolling();
	}

	private isVisible() {
		return !this.containerEl.ownerDocument.hidden && this.containerEl.isShown();
	}

	private stopPolling() {
		if (this.polling !== null) window.clearTimeout(this.polling);
		this.polling = null;
	}

	async refresh() {
		if (!this.opened || !this.isVisible()) return;
		if (this.refreshing) { this.refreshAgain = true; return; }
		this.stopPolling();
		this.refreshAgain = false;
		this.refreshing = true;
		const generation = this.generation;
		let state: ServiceState | null = null;
		let failure: Error | null = null;
		try {
			state = await this.plugin.api.state();
		} catch (error) {
			failure = error instanceof Error ? error : new Error(String(error));
		} finally {
			this.refreshing = false;
		}
		if (!this.opened) return;
		if (generation !== this.generation) { void this.refresh(); return; }
		if (!this.isVisible()) return;
		const busy = state?.active || state?.recent.some(job => job.status === "queued");
		this.polling = window.setTimeout(() => void this.refresh(), this.refreshAgain ? 0 : busy ? POLL_MS : IDLE_POLL_MS);
		this.draw("service", state ? "up" : String(failure), el => this.drawService(el, failure));
		if (!state) {
			for (const name of ["active", "need", "done", "watcher"]) this.draw(name, "", el => el.empty());
			this.activeId = null;
			return;
		}
		const recent = state.recent.filter(job => job.job_id !== state.active?.job_id);
		const queued = recent.filter(job => job.status === "queued");
		const todo = recent.filter(job => DONE.has(job.status) && !this.linkedNote(job));
		const failed = recent.filter(job => job.status === "failed");
		const done = recent.filter(job => (DONE.has(job.status) && this.linkedNote(job)) || job.status === "cancelled");
		const archived = this.plugin.settings.archived;
		this.drawActive(state.active, queued);
		this.draw("need", JSON.stringify([state.offered, todo, failed]), el => this.drawNeed(el, state.offered, todo, failed));
		this.draw("done", JSON.stringify([done.slice(0, DONE_LIMIT), done.map(job => archived[job.job_id])]),
			el => this.drawDone(el, done.slice(0, DONE_LIMIT)));
		this.draw("watcher", JSON.stringify(state.watcher), el => this.drawWatcher(el, state.watcher));
		this.primed = true;
	}

	private draw(name: string, key: string, render: (el: HTMLElement) => void) {
		if (this.shown[name] === key) return;
		const el = this.sections[name];
		el.empty();
		render(el);
		this.shown[name] = key;
	}

	/** Marks a card for the entrance animation the first time it appears. */
	private card(parent: HTMLElement, key: string, cls: string): HTMLElement {
		const fresh = this.primed && !this.seen.has(key);
		this.seen.add(key);
		return parent.createDiv({ cls: `${cls}${fresh ? " mt-enter" : ""}` });
	}

	private async act(action: () => Promise<unknown>, card?: HTMLElement) {
		if (card) {
			card.addClass("mt-leave");
			await new Promise(resolve => window.setTimeout(resolve, LEAVE_MS));
		}
		try {
			await action();
		} catch (error) {
			card?.removeClass("mt-leave");
			new Notice(`转录服务：${(error as Error).message}`);
		}
		await this.refresh();
	}

	private async copy(button: HTMLButtonElement, path: string) {
		if (button.hasClass("is-done")) return;
		if (!await this.plugin.copyOrganizeCommand(path)) return;
		const label = button.querySelector("span:last-child");
		const old = label?.textContent ?? "";
		button.addClass("is-done");
		if (label) label.textContent = "已复制";
		window.setTimeout(() => {
			button.removeClass("is-done");
			if (label) label.textContent = old;
		}, 1400);
	}

	private drawService(el: HTMLElement, failure: Error | null) {
		if (failure === null) return;
		const card = el.createDiv({ cls: "mt-card mt-down" });
		const top = card.createDiv({ cls: "mt-top" });
		icon(top, "plug-zap", "mt-tile is-bad");
		const info = top.createDiv({ cls: "mt-info" });
		info.createDiv({ cls: "mt-name", text: "转录服务没有运行" });
		info.createDiv({ cls: "mt-sub", text: failure.message, attr: { title: failure.message } });
		if (failure instanceof ApiError && failure.kind === "connection") {
			const acts = card.createDiv({ cls: "mt-acts" });
			button(acts, "启动转录服务", { icon: "play", primary: true }).onclick = () => this.plugin.startService();
		}
	}

	/** The running job updates in place, so its bar moves instead of the card re-entering every poll. */
	private drawActive(job: Job | null, queued: Job[]) {
		const el = this.sections.active;
		const queueKey = JSON.stringify(queued.map(q => q.job_id));
		if (job && job.job_id === this.activeId && this.shown.active === queueKey) {
			this.updateActive(el, job);
			return;
		}
		el.empty();
		this.activeId = job?.job_id ?? null;
		this.shown.active = queueKey;
		if (!job) {
			if (!queued.length) return;
		}
		el.createDiv({ cls: "mt-label", text: job ? (queued.length ? `正在转录 · 之后还有 ${queued.length} 个` : "正在转录") : "排队中" });
		if (job) {
			const card = this.card(el, job.job_id, "mt-card mt-hero");
			const top = card.createDiv({ cls: "mt-top" });
			icon(top, VIDEO.test(job.source_path) ? "film" : "mic");
			const info = top.createDiv({ cls: "mt-info" });
			info.createDiv({ cls: "mt-name", text: basename(job.source_path), attr: { title: job.source_path } });
			const sub = info.createDiv({ cls: "mt-sub" });
			sub.createSpan({ cls: "mt-stage" });
			sub.createSpan({ text: " · " });
			sub.createSpan({ cls: "mt-pct mt-mono" });
			const cancel = iconButton(top, "square", "取消");
			cancel.onclick = () => this.act(() => this.plugin.api.cancel());
			card.createDiv({ cls: "mt-bar" }).createDiv({ cls: "mt-bar-fill" });
			const steps = card.createDiv({ cls: "mt-steps" });
			STEPS.forEach(step => steps.createSpan({ text: step }));
			this.updateActive(el, job);
		}
		for (const q of queued) {
			const row = this.card(el, q.job_id, "mt-card mt-slim");
			icon(row, "clock", "mt-tile is-plain");
			row.createDiv({ cls: "mt-name is-light", text: basename(q.source_path), attr: { title: q.source_path } });
			row.createSpan({ cls: "mt-mono mt-faint", text: "排队中" });
		}
	}

	private updateActive(el: HTMLElement, job: Job) {
		const p = overall(job);
		const fill = el.querySelector<HTMLElement>(".mt-bar-fill");
		if (fill) fill.style.transform = `scaleX(${p})`;
		const stage = el.querySelector(".mt-stage");
		if (stage) stage.textContent = job.status === "cancel_requested" ? STATUS.cancel_requested : STEPS[STEP_OF[job.stage] ?? 0];
		const pct = el.querySelector(".mt-pct");
		if (pct) pct.textContent = `${Math.round(p * 100)}%`;
		const current = STEP_OF[job.stage] ?? 0;
		el.querySelectorAll(".mt-steps span").forEach((step, i) => {
			step.toggleClass("is-past", i < current);
			step.toggleClass("is-now", i === current);
		});
	}

	private drawNeed(el: HTMLElement, offered: string[], todo: Job[], failed: Job[]) {
		const count = offered.length + todo.length + failed.length;
		if (!count) return;
		el.createDiv({ cls: "mt-label", text: `需要你处理 · ${count}` });
		for (const path of offered) {
			const card = this.card(el, `offer:${path}`, "mt-card");
			const top = card.createDiv({ cls: "mt-top" });
			icon(top, "film");
			const info = top.createDiv({ cls: "mt-info" });
			info.createDiv({ cls: "mt-name", text: basename(path), attr: { title: path } });
			info.createDiv({ cls: "mt-sub", text: "OBS 新录屏" });
			top.createSpan({ cls: "mt-chip is-warn", text: "新" });
			const acts = card.createDiv({ cls: "mt-acts" });
			button(acts, "转录", { icon: "mic", primary: true }).onclick = () => this.act(() => this.plugin.api.accept(path), card);
			button(acts, "忽略", { ghost: true }).onclick = () => this.act(() => this.plugin.api.dismiss(path), card);
		}
		for (const job of todo) {
			const card = this.card(el, job.job_id, "mt-card");
			const top = card.createDiv({ cls: "mt-top" });
			icon(top, "file-text");
			const info = top.createDiv({ cls: "mt-info" });
			info.createDiv({ cls: "mt-name", text: basename(job.source_path), attr: { title: job.source_path } });
			info.createDiv({ cls: "mt-sub", text: `${when(job.finished_at || job.created_at)} 转完` });
			const warned = job.status === "completed_with_warning";
			top.createSpan({ cls: `mt-chip${warned ? " is-warn" : ""}`, text: warned ? "有警告" : "待整理", attr: warned && job.error ? { title: job.error } : {} });
			const acts = card.createDiv({ cls: "mt-acts" });
			if (job.output_path) {
				const copy = button(acts, "复制整理命令", { icon: "copy", primary: true });
				copy.onclick = () => this.copy(copy, job.output_path);
			}
			button(acts, "关联整理稿").onclick = () => this.plugin.linkNote(job.job_id);
			iconButton(acts, "more-horizontal", "更多").onclick = event => this.menu(job).showAtMouseEvent(event);
			card.oncontextmenu = event => { event.preventDefault(); this.menu(job).showAtMouseEvent(event); };
		}
		for (const job of failed) {
			const card = this.card(el, job.job_id, "mt-card");
			const top = card.createDiv({ cls: "mt-top" });
			icon(top, "alert-circle", "mt-tile is-bad");
			const info = top.createDiv({ cls: "mt-info" });
			info.createDiv({ cls: "mt-name", text: basename(job.source_path), attr: { title: job.source_path } });
			info.createDiv({ cls: "mt-sub mt-error", text: job.error || job.message, attr: { title: job.error || job.message } });
			top.createSpan({ cls: "mt-chip is-bad", text: "失败" });
			const acts = card.createDiv({ cls: "mt-acts" });
			button(acts, "重试", { icon: "rotate-ccw" }).onclick = () => this.act(() => this.plugin.api.retry(job.job_id), card);
		}
	}

	private drawDone(el: HTMLElement, jobs: Job[]) {
		el.createDiv({ cls: "mt-label", text: "已完成" });
		if (!jobs.length) {
			el.createDiv({ cls: "mt-empty", text: "还没有任务。在资源管理器里右键录音选“转录”，或从托盘菜单添加。" });
			return;
		}
		for (const job of jobs) {
			const row = this.card(el, job.job_id, "mt-card mt-slim");
			const cancelled = job.status === "cancelled";
			icon(row, cancelled ? "x" : "check", `mt-tile is-plain${cancelled ? "" : " is-ok"}`);
			row.createDiv({ cls: "mt-name is-light", text: basename(job.source_path), attr: { title: job.source_path } });
			row.createSpan({ cls: "mt-mono mt-faint", text: cancelled ? "已取消" : when(job.finished_at || job.created_at).slice(0, 5) });
			const note = this.linkedNote(job);
			if (note) iconButton(row, "external-link", "打开整理稿").onclick = () => this.app.workspace.getLeaf(false).openFile(note);
			iconButton(row, "more-horizontal", "更多").onclick = event => this.menu(job).showAtMouseEvent(event);
			row.oncontextmenu = event => { event.preventDefault(); this.menu(job).showAtMouseEvent(event); };
		}
	}

	private menu(job: Job): Menu {
		const menu = new Menu();
		const note = this.linkedNote(job);
		if (note) menu.addItem(item => item.setTitle("打开整理稿").setIcon("external-link").onClick(() => this.app.workspace.getLeaf(false).openFile(note)));
		if (DONE.has(job.status) && job.output_path) {
			menu.addItem(item => item.setTitle("复制整理命令").setIcon("copy").onClick(() => this.plugin.copyOrganizeCommand(job.output_path)));
			menu.addItem(item => item.setTitle("打开原稿").setIcon("file-text").onClick(() => this.plugin.openTranscript(job.output_path)));
			menu.addItem(item => item.setTitle(note ? "重新关联整理稿…" : "关联整理稿…").setIcon("link").onClick(() => this.plugin.linkNote(job.job_id)));
		}
		if (RETRYABLE.has(job.status)) {
			menu.addSeparator();
			menu.addItem(item => item.setTitle("重试").setIcon("rotate-ccw").onClick(() => this.act(() => this.plugin.api.retry(job.job_id))));
		}
		return menu;
	}

	/** The organized note the user associated with this job, while it still exists. */
	private linkedNote(job: Job): TFile | null {
		const path = this.plugin.settings.archived[job.job_id];
		const file = path ? this.app.vault.getAbstractFileByPath(path) : null;
		return file instanceof TFile ? file : null;
	}

	private drawWatcher(el: HTMLElement, watcher: ServiceState["watcher"]) {
		const row = el.createDiv({ cls: "mt-watch" });
		const text = row.createDiv({ cls: "mt-info" });
		text.createDiv({ text: "监听 OBS 录屏" });
		text.createDiv({ cls: "mt-sub mt-mono", text: watcher.dir, attr: { title: watcher.dir } });
		const toggle = row.createDiv({ cls: `checkbox-container${watcher.enabled ? " is-enabled" : ""}`, attr: { "aria-label": "发现新录屏时询问是否转录" } });
		toggle.createEl("input", { type: "checkbox", attr: { tabindex: "0" } });
		toggle.onclick = () => {
			const next = !toggle.hasClass("is-enabled");
			toggle.toggleClass("is-enabled", next);
			void this.act(() => this.plugin.api.setWatching(next));
		};
	}
}
