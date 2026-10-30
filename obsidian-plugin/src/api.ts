import { readFileSync } from "fs";
import { join } from "path";
import { requestUrl } from "obsidian";

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

/** The tray writes this secret when it starts; a request without it is rejected. */
function readSecret(): string {
	try {
		return readFileSync(join(process.env.LOCALAPPDATA ?? "", "SimpleVideoTranscriber", "api_token"), "utf8").trim();
	} catch {
		return "";
	}
}

/** Client for the tray's local API (api.py). Throws when the tray is not running. */
export class TranscriberApi {
	constructor(private readonly port: () => number) {}

	private async call<T>(method: "GET" | "POST", route: string, body?: unknown): Promise<T> {
		const response = await requestUrl({
			url: `http://127.0.0.1:${this.port()}${route}`,
			method,
			headers: { Authorization: `Bearer ${readSecret()}`, "Content-Type": "application/json" },
			body: body === undefined ? undefined : JSON.stringify(body),
			throw: false,
		});
		if (response.status !== 200) throw new Error(response.json?.error ?? `HTTP ${response.status}`);
		return response.json as T;
	}

	state = () => this.call<ServiceState>("GET", "/api/state");
	queue = (paths: string[]) => this.call<{ queued: string[] }>("POST", "/api/jobs", { paths });
	retry = (jobId: string) => this.call("POST", `/api/jobs/${encodeURIComponent(jobId)}/retry`, {});
	cancel = () => this.call("POST", "/api/cancel", {});
	accept = (path: string) => this.call("POST", "/api/recordings/accept", { path });
	dismiss = (path: string) => this.call("POST", "/api/recordings/dismiss", { path });
	setWatching = (enabled: boolean) => this.call("POST", "/api/watcher", { enabled });
	setToken = (token: string) => this.call("POST", "/api/token", { token });
}
