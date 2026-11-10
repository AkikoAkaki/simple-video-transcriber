import { basename, extname } from "path";
import { ItemView, Menu, Notice, WorkspaceLeaf, setIcon } from "obsidian";
import media from "../../media_extensions.json";
import { ApiError, type Job, type ServiceState } from "./api";
import type MeetingTranscriberPlugin from "./main";

export const VIEW_TYPE = "meeting-transcriber-tasks";
const POLL_MS = 2000;
const IDLE_POLL_MS = 15000;
const LEAVE_MS = 160;
const DONE_LIMIT = 8;

const VIDEO = new Set(media.video);

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

/** The file a job came from, with its full path on hover. */
function fileName(parent: HTMLElement, path: string, cls = "mt-name"): void {
	parent.createDiv({ cls, text: basename(path), attr: { title: path } });
}

/** The icon and file name that open every full-size card. */
function cardTop(card: HTMLElement, iconName: string, path: string, tile = "mt-tile"): { top: HTMLElement; info: HTMLElement } {
	const top = card.createDiv({ cls: "mt-top" });
	icon(top, iconName, tile);
	const info = top.createDiv({ cls: "mt-info" });
	fileName(info, path);
	return { top, info };
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
		root.style.setProperty("--mt-leave", `${LEAVE_MS}ms`);
		for (const name of ["service", "active", "need", "done", "watcher"]) {
			this.sections[name] = root.createDiv({ cls: `mt-section mt-${name}` });
		}
		await this.refresh();
	}

	async onClose() {
		this.halt();
	}

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
		const todo = recent.filter(job => job.status === "completed" && !this.plugin.linkedNote(job.job_id));
		const failed = recent.filter(job => job.status === "failed");
		const done = recent.filter(job => (job.status === "completed" && this.plugin.linkedNote(job.job_id)) || job.status === "cancelled");
		this.drawActive(state.active, queued, state.steps);
		this.draw("need", JSON.stringify([state.offered, todo, failed]), el => this.drawNeed(el, state.offered, todo, failed));
		this.draw("done", JSON.stringify([done.slice(0, DONE_LIMIT), done.map(job => this.plugin.linkedNote(job.job_id)?.path)]),
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
	private drawActive(job: Job | null, queued: Job[], steps: string[]) {
		const el = this.sections.active;
		const queueKey = JSON.stringify(queued.map(q => q.job_id));
		if (job && job.job_id === this.activeId && this.shown.active === queueKey) {
			this.updateActive(el, job, steps);
			return;
		}
		el.empty();
		this.activeId = job?.job_id ?? null;
		this.shown.active = queueKey;
		if (!job && !queued.length) return;
		el.createDiv({ cls: "mt-label", text: job ? (queued.length ? `正在转录 · 之后还有 ${queued.length} 个` : "正在转录") : "排队中" });
		if (job) {
			const card = this.card(el, job.job_id, "mt-card mt-hero");
			const { top, info } = cardTop(card, VIDEO.has(extname(job.source_path).toLowerCase()) ? "film" : "mic", job.source_path);
			const sub = info.createDiv({ cls: "mt-sub" });
			sub.createSpan({ cls: "mt-stage" });
			sub.createSpan({ text: " · " });
			sub.createSpan({ cls: "mt-pct mt-mono" });
			const cancel = iconButton(top, "square", "取消");
			cancel.onclick = () => this.act(() => this.plugin.api.cancel());
			card.createDiv({ cls: "mt-bar" }).createDiv({ cls: "mt-bar-fill" });
			const stepRow = card.createDiv({ cls: "mt-steps" });
			steps.forEach(step => stepRow.createSpan({ text: step }));
			this.updateActive(el, job, steps);
		}
		for (const q of queued) {
			const row = this.card(el, q.job_id, "mt-card mt-slim");
			icon(row, "clock", "mt-tile is-plain");
			fileName(row, q.source_path, "mt-name is-light");
			row.createSpan({ cls: "mt-mono mt-faint", text: "排队中" });
		}
	}

	private updateActive(el: HTMLElement, job: Job, steps: string[]) {
		const p = job.overall_progress;
		const fill = el.querySelector<HTMLElement>(".mt-bar-fill");
		if (fill) fill.style.transform = `scaleX(${p})`;
		const stage = el.querySelector(".mt-stage");
		if (stage) stage.textContent = job.status === "cancel_requested" ? "正在取消" : steps[job.step];
		const pct = el.querySelector(".mt-pct");
		if (pct) pct.textContent = `${Math.round(p * 100)}%`;
		el.querySelectorAll(".mt-steps span").forEach((step, i) => {
			step.toggleClass("is-past", i < job.step);
			step.toggleClass("is-now", i === job.step);
		});
	}

	private drawNeed(el: HTMLElement, offered: string[], todo: Job[], failed: Job[]) {
		const count = offered.length + todo.length + failed.length;
		if (!count) return;
		el.createDiv({ cls: "mt-label", text: `需要你处理 · ${count}` });
		for (const path of offered) {
			const card = this.card(el, `offer:${path}`, "mt-card");
			const { top, info } = cardTop(card, "film", path);
			info.createDiv({ cls: "mt-sub", text: "OBS 新录屏" });
			top.createSpan({ cls: "mt-chip is-warn", text: "新" });
			const acts = card.createDiv({ cls: "mt-acts" });
			button(acts, "转录", { icon: "mic", primary: true }).onclick = () => this.act(() => this.plugin.api.accept(path), card);
			button(acts, "忽略", { ghost: true }).onclick = () => this.act(() => this.plugin.api.dismiss(path), card);
		}
		for (const job of todo) {
			const card = this.card(el, job.job_id, "mt-card");
			const { top, info } = cardTop(card, "file-text", job.source_path);
			info.createDiv({ cls: "mt-sub", text: `${when(job.finished_at || job.created_at)} 转完` });
			top.createSpan({ cls: "mt-chip", text: "待整理" });
			const acts = card.createDiv({ cls: "mt-acts" });
			if (job.output_path) {
				const copy = button(acts, "复制整理命令", { icon: "copy", primary: true });
				copy.onclick = () => this.copy(copy, job.output_path);
			}
			button(acts, "关联整理稿").onclick = () => this.plugin.chooseLinkedNote(job.job_id);
			this.attachMenu(card, acts, job);
		}
		for (const job of failed) {
			const card = this.card(el, job.job_id, "mt-card");
			const { top, info } = cardTop(card, "alert-circle", job.source_path, "mt-tile is-bad");
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
			fileName(row, job.source_path, "mt-name is-light");
			row.createSpan({ cls: "mt-mono mt-faint", text: cancelled ? "已取消" : when(job.finished_at || job.created_at).slice(0, 5) });
			const note = this.plugin.linkedNote(job.job_id);
			if (note) iconButton(row, "external-link", "打开整理稿").onclick = () => this.app.workspace.getLeaf(false).openFile(note);
			this.attachMenu(row, row, job);
		}
	}

	/** Offers a job's actions from a "more" button and from right-clicking its card. */
	private attachMenu(card: HTMLElement, buttonParent: HTMLElement, job: Job) {
		iconButton(buttonParent, "more-horizontal", "更多").onclick = event => this.menu(job).showAtMouseEvent(event);
		card.oncontextmenu = event => { event.preventDefault(); this.menu(job).showAtMouseEvent(event); };
	}

	private menu(job: Job): Menu {
		const menu = new Menu();
		const note = this.plugin.linkedNote(job.job_id);
		if (note) menu.addItem(item => item.setTitle("打开整理稿").setIcon("external-link").onClick(() => this.app.workspace.getLeaf(false).openFile(note)));
		if (job.status === "completed" && job.output_path) {
			menu.addItem(item => item.setTitle("复制整理命令").setIcon("copy").onClick(() => this.plugin.copyOrganizeCommand(job.output_path)));
			menu.addItem(item => item.setTitle("打开原稿").setIcon("file-text").onClick(() => this.plugin.openTranscript(job.output_path)));
			menu.addItem(item => item.setTitle(note ? "重新关联整理稿…" : "关联整理稿…").setIcon("link").onClick(() => this.plugin.chooseLinkedNote(job.job_id)));
		}
		if (job.retryable) {
			menu.addSeparator();
			menu.addItem(item => item.setTitle("重试").setIcon("rotate-ccw").onClick(() => this.act(() => this.plugin.api.retry(job.job_id))));
		}
		return menu;
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
