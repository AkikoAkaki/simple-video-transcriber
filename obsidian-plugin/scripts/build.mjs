import { build } from 'esbuild';
import { mkdir, copyFile } from 'node:fs/promises';
await mkdir('dist', { recursive: true });
// Node built-ins stay external: Obsidian desktop provides them through require.
await build({ entryPoints: ['src/main.ts'], outfile: 'dist/main.js', bundle: true, format: 'cjs',
  platform: 'browser', target: 'es2022', external: ['obsidian', 'electron', 'fs', 'path', 'child_process'],
  minify: true, legalComments: 'eof', charset: 'utf8' });
await Promise.all(['manifest.json', 'styles.css'].map(name => copyFile(name, `dist/${name}`)));
console.log('Built dist/');
