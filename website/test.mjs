/** Verify source fidelity and portable links before publishing the generated guide. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, access } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chaptersFrom, slug, sourceLink } from './content.mjs';

test('chapter headings match GitHub anchors, including inline code', () => {
  assert.equal(slug('9. Your first strategy: `try` and `escalate_when`'), '9-your-first-strategy-try-and-escalate_when');
  const sample = '# Tutorial\n\n## Index\n\n## 1. Install\n\n```bash\n## not a chapter\n```\n\n## 2. Parse\n\nExact content.\n\n## Appendix: config\n';
  const chapters = chaptersFrom(sample);
  assert.equal(chapters.length, 3);
  assert.match(chapters[0].body, /## not a chapter/);
  assert.match(chapters[1].body, /Exact content\./);
});
test('relative source links retain the intended repository file and fragment', () => {
  assert.equal(sourceLink('../src/openreading/README.md#the-map', 'tutorial/README.md'), 'https://github.com/multiversal-ventures/openreading-core/blob/main/src/openreading/README.md#the-map');
  assert.equal(sourceLink('#2-your-first-parse', 'tutorial/README.md'), '#2-your-first-parse');
  assert.equal(sourceLink('https://example.org', 'tutorial/README.md'), 'https://example.org');
});
test('every tutorial chapter is generated from the source body', async () => {
  const source = await readFile(new URL('../tutorial/README.md', import.meta.url), 'utf8')
    .catch(error => {
      if (error.code !== 'ENOENT') throw error;
      return readFile(new URL('./core/tutorial/README.md', import.meta.url), 'utf8');
    });
  const chapters = chaptersFrom(source);
  assert.equal(chapters.length, 18);
  for (const chapter of chapters) assert.ok(source.includes(chapter.body));
});

test('built pages have unique anchors and all local links and images resolve', async () => {
  const output = new URL('./dist/', import.meta.url);
  for (const file of ['index.html', 'tutorial.html', 'diagrams.html']) {
    const html = await readFile(new URL(file, output), 'utf8');
    const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
    assert.equal(new Set(ids).size, ids.length, `Duplicate anchors in ${file}`);
    for (const [, url] of html.matchAll(/(?:href|src)="([^"]+)"/g)) {
      if (/^(https?:|mailto:)/.test(url)) continue;
      const [path, fragment] = url.split('#');
      const target = new URL(path || file, output);
      await access(target);
      if (fragment) assert.ok((await readFile(target, 'utf8')).includes(`id="${fragment}"`), `${file}: ${url}`);
    }
  }
});

test('SVG exports are accessible standalone vectors with embedded font settings', async () => {
  const manifest = JSON.parse(await readFile(new URL('./dist/assets/diagrams/manifest.json', import.meta.url), 'utf8'));
  assert.equal(manifest.length, 11);
  for (const item of manifest) {
    const svg = await readFile(resolve(import.meta.dirname, 'dist/assets/diagrams', item.file), 'utf8');
    const root = svg.match(/<svg\b[^>]*>/)[0];
    assert.equal([...root.matchAll(/\brole=/g)].length, 1);
    assert.match(svg, /<title>[^<]+<\/title>/);
    assert.match(svg, /font-family:Arial/);
    assert.match(root, /viewBox=/);
    assert.doesNotMatch(svg, /<foreignObject/);
  }
});
