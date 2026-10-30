import { existsSync, readFileSync, unlinkSync } from "fs";
import { basename } from "path";
import { App, FuzzySuggestModal, normalizePath, TFile, TFolder } from "obsidian";

/** "CSC 290 14_48e5a0e0a9a3.md" -> "CSC 290 14": the transcriber appends a source fingerprint. */
export function transcriptTitle(outputPath: string): string {
	return basename(outputPath).replace(/\.md$/i, "").replace(/_[0-9a-f]{12}$/i, "");
}

const compact = (text: string) => text.toUpperCase().replace(/\s+/g, "");

/** The course's "Lecture Transcripts" folder when the title names a course code such as "CSC 290". */
export function suggestFolder(app: App, title: string): TFolder | null {
	const match = title.match(/([A-Za-z]{2,4})\s*(\d{3}[A-Za-z]?)/);
	if (!match) return null;
	const code = compact(match[1] + match[2]);
	const folders = app.vault.getAllFolders().filter(folder => compact(folder.path).includes(code));
	return folders.find(folder => folder.name === "Lecture Transcripts") ?? folders[0] ?? null;
}

class FolderModal extends FuzzySuggestModal<TFolder> {
	constructor(app: App, private readonly first: TFolder[], private readonly choose: (folder: TFolder) => void) {
		super(app);
		this.setPlaceholder("归档到哪个文件夹？推荐的放在最前面");
	}

	getItems(): TFolder[] {
		const rest = this.app.vault.getAllFolders().filter(folder => !this.first.includes(folder));
		return [...this.first, ...rest.sort((a, b) => a.path.localeCompare(b.path))];
	}

	getItemText(folder: TFolder): string {
		return folder.path || "/";
	}

	onChooseItem(folder: TFolder): void {
		this.choose(folder);
	}
}

export function pickFolder(app: App, first: TFolder[]): Promise<TFolder | null> {
	return new Promise(resolve => {
		const modal = new FolderModal(app, first, folder => resolve(folder));
		const close = modal.onClose.bind(modal);
		// Obsidian closes the modal before calling onChooseItem, so a chosen folder wins over this deferred null.
		modal.onClose = () => {
			close();
			setTimeout(() => resolve(null), 0);
		};
		modal.open();
	});
}

/**
 * Move a finished transcript from the transcriber's staging folder into the vault.
 * The staging copy is deleted only after the vault copy reads back identical.
 */
export async function moveIntoVault(app: App, outputPath: string, folder: TFolder): Promise<TFile> {
	const content = readFileSync(outputPath, "utf8");
	const title = transcriptTitle(outputPath);
	let target = normalizePath(`${folder.path}/${title}.md`);
	for (let n = 2; app.vault.getAbstractFileByPath(target); n++) {
		target = normalizePath(`${folder.path}/${title} ${n}.md`);
	}
	const file = await app.vault.create(target, content);
	if ((await app.vault.read(file)) !== content) throw new Error(`${target} does not match the transcript; kept both copies`);
	if (existsSync(outputPath)) unlinkSync(outputPath);
	return file;
}
