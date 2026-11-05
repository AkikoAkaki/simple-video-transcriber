import { readFile } from "fs/promises";
import { request } from "http";
import { join } from "path";

export class ApiError extends Error {
	constructor(message: string, readonly kind: "connection" | "timeout" | "auth" | "response") {
		super(message);
	}
}

export interface Job {
	job_id: string;
	source_path: string;
	status: string;
	stage: string;
	progress: number | null;
	message: string;
	output_path: string;
	error: string;
	created_at: string;
	finished_at: string;
}

export interface ServiceState {
	active: Job | null;
	recent: Job[];
	offered: string[];
	watcher: { enabled: boolean; dir: string };
	transcript_dir: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isJob(value: unknown): value is Job {
	return isRecord(value)
		&& ["job_id", "source_path", "status", "stage", "message", "output_path", "error", "created_at", "finished_at"]
			.every(key => typeof value[key] === "string")
		&& (value.progress === null || (typeof value.progress === "number" && Number.isFinite(value.progress)));
}

function isState(value: unknown): value is ServiceState {
	return isRecord(value) && (value.active === null || isJob(value.active))
		&& Array.isArray(value.recent) && value.recent.every(isJob)
		&& Array.isArray(value.offered) && value.offered.every(path => typeof path === "string")
		&& isRecord(value.watcher) && typeof value.watcher.enabled === "boolean" && typeof value.watcher.dir === "string"
		&& typeof value.transcript_dir === "string";
}

/** The tray writes this secret when it starts; a request without it is rejected. */
async function readSecret(): Promise<string> {
	try {
		return (await readFile(join(process.env.LOCALAPPDATA ?? "", "SimpleVideoTranscriber", "api_token"), "utf8")).trim();
	} catch {
		return "";
	}
}

/** Client for the tray's local API (api.py). Throws when the tray is not running. */
export class TranscriberApi {
	constructor(private readonly port: () => number) {}

	private async call(method: "GET" | "POST", route: string, body?: unknown): Promise<unknown> {
		const secret = await readSecret();
		const data = body === undefined ? undefined : JSON.stringify(body);
		const response = await new Promise<{ status: number; text: string }>((resolve, reject) => {
			const req = request({ hostname: "127.0.0.1", port: this.port(), path: route, method,
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
	accept = (path: string) => this.call("POST", "/api/recordings/accept", { path });
	dismiss = (path: string) => this.call("POST", "/api/recordings/dismiss", { path });
	setWatching = (enabled: boolean) => this.call("POST", "/api/watcher", { enabled });
	setToken = (token: string) => this.call("POST", "/api/token", { token });
}
