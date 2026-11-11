import { readFile } from "fs/promises";
import { request } from "http";
import { join } from "path";

export class ApiError extends Error {
	constructor(message: string, readonly kind: "connection" | "timeout" | "auth" | "response") {
		super(message);
	}
}

const JOB_TEXT_FIELDS = ["job_id", "source_path", "message", "output_path", "error", "created_at", "finished_at"] as const;

/** Mirrors the job fields in background/api.py. */
export type Job = Record<(typeof JOB_TEXT_FIELDS)[number], string> & {
	status: "queued" | "running" | "cancel_requested" | "completed" | "failed" | "cancelled";
	overall_progress: number;
	step: number;
	retryable: boolean;
};

export interface ServiceState {
	active: Job | null;
	recent: Job[];
	steps: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isJob(value: unknown): value is Job {
	return isRecord(value)
		&& JOB_TEXT_FIELDS.every(key => typeof value[key] === "string") && typeof value.status === "string"
		&& typeof value.overall_progress === "number" && Number.isFinite(value.overall_progress)
		&& typeof value.step === "number" && typeof value.retryable === "boolean";
}

function isState(value: unknown): value is ServiceState {
	return isRecord(value) && (value.active === null || isJob(value.active))
		&& Array.isArray(value.recent) && value.recent.every(isJob)
		&& Array.isArray(value.steps) && value.steps.every(step => typeof step === "string");
}

/** The tray's app data folder (appdata.APP_DATA_DIR). */
export const APP_DATA_DIR = join(process.env.LOCALAPPDATA ?? "", "SimpleVideoTranscriber");

/** A value the tray publishes in its app data folder. */
async function readPublished(name: string): Promise<string> {
	try {
		return (await readFile(join(APP_DATA_DIR, name), "utf8")).trim();
	} catch {
		return "";
	}
}

/** The recent jobs the tray last published, readable after it has exited. */
export async function readHistory(): Promise<Job[]> {
	try {
		const jobs: unknown = JSON.parse(await readPublished("history.json"));
		return Array.isArray(jobs) ? jobs.filter(isJob) : [];
	} catch {
		return [];
	}
}

/** Client for the tray's local API (background/api.py). Throws when the tray is not running. */
export class TranscriberApi {
	private async call(method: "GET" | "POST", route: string, body?: unknown): Promise<unknown> {
		const [secret, port] = await Promise.all([readPublished("api_token"), readPublished("api_port")]);
		if (!port) throw new ApiError("转录服务还没有启动过", "connection");
		const data = body === undefined ? undefined : JSON.stringify(body);
		const response = await new Promise<{ status: number; text: string }>((resolve, reject) => {
			const req = request({ hostname: "127.0.0.1", port: Number(port), path: route, method,
				headers: { Authorization: `Bearer ${secret}`, "Content-Type": "application/json",
					...(data === undefined ? {} : { "Content-Length": Buffer.byteLength(data) }) },
			}, res => {
				const chunks: string[] = [];
				res.setEncoding("utf8");
				res.on("data", (chunk: string) => chunks.push(chunk));
				res.on("error", error => reject(new ApiError(error.message, "connection")));
				res.on("end", () => resolve({ status: res.statusCode ?? 0, text: chunks.join("") }));
			});
			const timer = setTimeout(() => req.destroy(new ApiError("请求超时，请稍后刷新确认操作结果", "timeout")), 10000);
			req.on("close", () => clearTimeout(timer));
			req.on("error", error => reject(error instanceof ApiError ? error : new ApiError(error.message, "connection")));
			req.end(data);
		});
		let payload: unknown;
		try {
			payload = JSON.parse(response.text);
		} catch {
			throw new ApiError(`HTTP ${response.status}：服务返回了非 JSON 内容`, "response");
		}
		if (response.status !== 200) {
			throw new ApiError(response.status === 401 ? "服务鉴权失败，请检查本机 API 密钥" :
				isRecord(payload) && typeof payload.error === "string" ? payload.error : `HTTP ${response.status}`,
				response.status === 401 ? "auth" : "response");
		}
		return payload;
	}

	async state(): Promise<ServiceState> {
		const state = await this.call("GET", "/api/state");
		if (!isState(state)) throw new Error("转录服务返回了无效的任务状态");
		return state;
	}
	async queue(paths: string[]): Promise<{ queued: string[] }> {
		const result = await this.call("POST", "/api/jobs", { paths });
		if (!isRecord(result) || !Array.isArray(result.queued) || !result.queued.every(id => typeof id === "string")) {
			throw new Error("转录服务返回了无效的排队结果");
		}
		return { queued: result.queued };
	}
	retry = (jobId: string) => this.call("POST", `/api/jobs/${encodeURIComponent(jobId)}/retry`, {});
	cancel = () => this.call("POST", "/api/cancel", {});
	setToken = (token: string) => this.call("POST", "/api/token", { token });
	async clearCache(): Promise<{ deleted_count: number; reclaimed_bytes: number }> {
		const result = await this.call("POST", "/api/cache/clear", {});
		if (!isRecord(result) || typeof result.deleted_count !== "number" || typeof result.reclaimed_bytes !== "number") {
			throw new Error("转录服务返回了无效的清理结果");
		}
		return { deleted_count: result.deleted_count, reclaimed_bytes: result.reclaimed_bytes };
	}
}
