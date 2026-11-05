// Copies only the three build artifacts; data.json in the vault is never touched.
import { mkdir, copyFile } from 'node:fs/promises';
const target = process.env.VAULT_PLUGIN_DIR ?? 'E:/obsidian-vault/.obsidian/plugins/meeting-transcriber';
await mkdir(target, { recursive: true });
await Promise.all(['main.js', 'manifest.json', 'styles.css'].map(name => copyFile(`dist/${name}`, `${target}/${name}`)));
console.log(`Deployed to ${target}`);
