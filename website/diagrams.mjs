/** Export README Mermaid sources as self-contained SVGs without HTML labels.
 * Run npm run diagrams from this folder after editing any README diagram.
 * README sources remain beside their explanations, inside disclosure elements.
 * MERMAID_CHROME optionally selects a local Chrome executable for the renderer.
 */
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve, relative, dirname, join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { styleDiagram, theme } from './theme.mjs';

const sourceArg = process.argv.indexOf('--source');
const root = resolve(sourceArg < 0 ? join(import.meta.dirname, '..') : process.argv[sourceArg + 1]);
const paths = execFileSync('git', ['ls-files', '*README.md'], { cwd: root, encoding: 'utf8' }).trim().split('\n');
const scratch = resolve(import.meta.dirname, '.render');
const assets = join(root, 'assets/diagrams');
await mkdir(scratch, { recursive: true });
await mkdir(assets, { recursive: true });
const puppeteer = process.env.MERMAID_CHROME ? { executablePath: process.env.MERMAID_CHROME } : {};
await writeFile(join(scratch, 'puppeteer.json'), JSON.stringify(puppeteer));
await writeFile(join(scratch, 'theme.json'), JSON.stringify(theme));
const manifest = [];
for (const path of paths) {
  let source = await readFile(join(root, path), 'utf8');
  let index = 0;
  for (const match of [...source.matchAll(/```mermaid\n([\s\S]*?)```/g)]) {
    index++;
    const name = `${path.replace(/\/README.md$|\.md$/g, '').replaceAll('/', '-')}-${index}`;
    const before = source.slice(0, source.indexOf(match[0]));
    const heading = [...before.matchAll(/^#{1,3} (.+)$/gm)].at(-1)?.[1] ?? 'OpenReading';
    const code = styleDiagram(match[1]);
    const input = join(scratch, `${name}.mmd`);
    const output = join(assets, `${name}.svg`);
    await writeFile(input, code);
    execFileSync(join(import.meta.dirname, 'node_modules/.bin/mmdc'),
      ['-i', input, '-o', output, '-b', 'white', '-w', '1800', '-p', join(scratch, 'puppeteer.json'), '-c', join(scratch, 'theme.json')], { stdio: 'inherit' });
    let svg = await readFile(output, 'utf8');
    const escaped = heading.replaceAll('&', '&amp;').replaceAll('<', '&lt;');
    svg = svg.replace(/(<svg[^>]*?) role="[^"]*"/, '$1 role="img"')
      .replace(/(<svg[^>]*>)/, `$1<title>${escaped}</title>`);
    await writeFile(output, svg);
    const link = relative(dirname(join(root, path)), output).replaceAll('\\', '/');
    const block = `\`\`\`mermaid\n${code}\`\`\``;
    const replacement = before.includes(`<!-- diagram:${name} -->`)
      ? block : `<!-- diagram:${name} -->\n<p align="center"><img src="${link}" alt="${heading.replaceAll('"', '&quot;')}" /></p>\n\n<details>\n<summary>Diagram source (Mermaid)</summary>\n\n${block}\n\n</details>`;
    source = source.replace(match[0], replacement);
    manifest.push({ name, source: path, title: heading, file: `${name}.svg` });
  }
  if (index) await writeFile(join(root, path), source);
}
await writeFile(join(assets, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`Exported ${manifest.length} diagrams.`);
