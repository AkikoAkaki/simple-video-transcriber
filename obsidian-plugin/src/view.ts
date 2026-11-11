import { basename, extname } from "path";
import { ItemView, Notice, WorkspaceLeaf, setIcon } from "obsidian";
import media from "../../media_extensions.json";
import { ApiError, readHistory, type Job, type ServiceState } from "./api";
import type MeetingTranscriberPlugin from "./main";

export const VIEW_TYPE = "meeting-transcriber-tasks";
const POLL_MS = 2000;
const IDLE_POLL_MS = 15000;
const LEAVE_MS = 160;

const VIDEO = new Set(media.video);
const FINISHED = new Set<Job["status"]>(["completed", "failed", "cancelled"]);

function when(iso: string): string {
	if (!iso) return "";
	const date = new Date(iso);
	const pad = (n: number) => String(n).padStart(2, "0");
	return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function icon(parent: HTMLElement, name: string, cls: string): HTMLElement {
	const el = parent.createSpan({ cls });
	setIcon(el, name);
	return el;
}

function button(parent: HTMLElement, text: string, opts: { icon?: string; primary?: boolean } = {}): HTMLButtonElement {
	const el = parent.createEl("button", { cls: `mt-btn${opts.primary ? " is-primary" : ""}` });
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
		this.drawToolbar(root.createDiv({ cls: "mt-toolbar" }));
		for (const name of ["problem", "active", "history"]) {
			this.sections[name] = root.createDiv({ cls: `mt-section mt-${name}` });
		}
		root.ondragover = event => {
			event.preventDefault();
			root.addClass("is-drop");
		};
		root.ondragleave = event => {
			if (!root.contains(event.relatedTarget as Node | null)) root.removeClass("is-drop");
		};
		root.ondrop = event => {
			event.preventDefault();
			root.removeClass("is-drop");
			const paths = event.dataTransfer ? this.plugin.droppedPaths(event.dataTransfer.files) : [];
			if (paths.length) void this.plugin.queuePaths(paths);
			else new Notice("没有可转录的音视频文件");
		};
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

	/** Polls while the tray runs; once it has exited, shows the history it left and waits for the next action. */
	async refresh() {
		if (!this.opened || !this.isVisible()) return;
		if (this.refreshing) { this.refreshAgain = true; return; }
		this.stopPolling();
		this.refreshAgain = false;
		this.refreshing = true;
		const generation = this.generation;
		let state: ServiceState | null = null;
		let problem = "";
		let jobs: Job[] = [];
		try {
			state = await this.plugin.api.state();
			jobs = state.recent;
		} catch (error) {
			if (!(error instanceof ApiError && error.kind === "connection")) problem = (error as Error).message;
			jobs = await readHistory();
		} finally {
			this.refreshing = false;
		}
		if (!this.opened) return;
		if (generation !== this.generation) { void this.refresh(); return; }
		if (!this.isVisible()) return;
		if (state || this.refreshAgain) {
			const busy = state?.active || jobs.some(job => job.status === "queued");
			this.polling = window.setTimeout(() => void this.refresh(), this.refreshAgain ? 0 : busy ? POLL_MS : IDLE_POLL_MS);
		}
		this.draw("problem", problem, el => {
			if (problem) el.createDiv({ cls: "mt-problem", text: `转录服务：${problem}` });
		});
		const activeId = state?.active?.job_id;
		const queued = state ? jobs.filter(job => job.status === "queued" && job.job_id !== activeId) : [];
		const history = jobs.filter(job => FINISHED.has(job.status) && job.job_id !== activeId && !this.plugin.isCopied(job.job_id));
		this.drawActive(state?.active ?? null, queued, state?.steps ?? []);
		this.draw("history", JSON.stringify(history), el => this.drawHistory(el, history));
		this.primed = true;
	}

	private draw(name: string, key: string, render: (el: HTMLElement) => void) {
		if (this.shown[name] === key) return;
		const el = this.sections[name];
		el.empty();
		render(el);
		this.shown[name] = key;
	}

	private item(parent: HTMLElement, key: string, cls: string): HTMLElement {
		const fresh = this.primed && !this.seen.has(key);
		this.seen.add(key);
		return parent.createDiv({ cls: `${cls}${fresh ? " mt-enter" : ""}` });
	}

	private async act(action: () => Promise<unknown>, item?: HTMLElement) {
		if (item) {
			item.addClass("mt-leave");
			await new Promise(resolve => window.setTimeout(resolve, LEAVE_MS));
		}
		try {
			await action();
		} catch (error) {
			item?.removeClass("mt-leave");
			new Notice(`转录服务：${(error as Error).message}`);
		}
		await this.refresh();
	}

	private drawToolbar(el: HTMLElement) {
		button(el, "转录文件…", { icon: "file-plus", primary: true }).onclick = () => this.plugin.chooseFiles();
		el.createDiv({ cls: "mt-spacer" });
		iconButton(el, "folder-open", "打开转录文件夹").onclick = () => this.plugin.openPath(this.plugin.transcriptDir);
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
			const card = this.item(el, job.job_id, "mt-card");
			const top = card.createDiv({ cls: "mt-top" });
			icon(top, VIDEO.has(extname(job.source_path).toLowerCase()) ? "film" : "mic", "mt-tile");
			const info = top.createDiv({ cls: "mt-info" });
			info.createDiv({ cls: "mt-name", text: basename(job.source_path), attr: { title: job.source_path } });
			const sub = info.createDiv({ cls: "mt-sub" });
			sub.createSpan({ cls: "mt-stage" });
			sub.createSpan({ text: " · " });
			sub.createSpan({ cls: "mt-pct mt-mono" });
			iconButton(top, "square", "取消").onclick = () => this.act(() => this.plugin.api.cancel());
			card.createDiv({ cls: "mt-bar" }).createDiv({ cls: "mt-bar-fill" });
			const stepRow = card.createDiv({ cls: "mt-steps" });
			steps.forEach(step => stepRow.createSpan({ text: step }));
			this.updateActive(el, job, steps);
		}
		for (const q of queued) {
			const row = this.item(el, q.job_id, "mt-row");
			icon(row, "clock", "mt-glyph");
			row.createDiv({ cls: "mt-name", text: basename(q.source_path), attr: { title: q.source_path } });
			row.createSpan({ cls: "mt-time", text: "排队中" });
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

	/** One line per finished job; copying its organizing command removes it. */
	private drawHistory(el: HTMLElement, jobs: Job[]) {
		el.createDiv({ cls: "mt-label", text: "历史" });
		if (!jobs.length) {
			el.createDiv({ cls: "mt-empty", text: "没有待处理的转录。点“转录文件…”，或把录音拖到这里。" });
			return;
		}
		for (const job of jobs) {
			const row = this.item(el, job.job_id, `mt-row is-${job.status}`);
			icon(row, job.status === "completed" ? "check" : job.status === "failed" ? "alert-circle" : "x", "mt-glyph");
			const name = basename(job.source_path);
			if (job.status === "completed" && job.output_path) {
				const link = row.createEl("a", { cls: "mt-name is-link", text: name, attr: { href: "#", "aria-label": `打开原稿：${job.output_path}` } });
				link.onclick = event => { event.preventDefault(); void this.plugin.openTranscript(job.output_path); };
			} else {
				row.createDiv({ cls: "mt-name", text: name, attr: { title: job.source_path } });
			}
			row.createSpan({ cls: "mt-time", text: job.status === "cancelled" ? "已取消" : when(job.finished_at || job.created_at) });
			if (job.status === "completed" && job.output_path) {
				iconButton(row, "copy", "复制整理命令").onclick = () =>
					this.act(async () => { await this.plugin.copyOrganizeCommand(job.job_id, job.output_path); }, row);
			} else if (job.retryable) {
				iconButton(row, "rotate-ccw", "重试").onclick = () =>
					this.act(() => this.plugin.withService(() => this.plugin.api.retry(job.job_id)), row);
			}
			if (job.status === "failed") {
				const reason = job.error || job.message;
				row.createDiv({ cls: "mt-reason", text: reason, attr: { title: reason } });
			}
		}
	}
}
