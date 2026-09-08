/** Build a static field guide from the core checkout, with no duplicated tutorial prose.
 * npm run build -- --source /path/to/openreading-core selects a separate checkout.
 * Output uses relative URLs so GitHub Pages repository prefixes need no configuration.
 * The website folder can move to its own repository without changing the core package.
 */
import { readFile, writeFile, mkdir, cp } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import MarkdownIt from 'markdown-it';
import hljs from 'highlight.js';
import { chaptersFrom, slug, sourceLink, repository } from './content.mjs';

const here = import.meta.dirname;
const sourceArg = process.argv.indexOf('--source');
const root = resolve(sourceArg < 0 ? join(here, '..') : process.argv[sourceArg + 1]);
const out = join(here, 'dist');
await mkdir(join(out, 'assets'), { recursive: true });
await cp(join(root, 'assets/diagrams'), join(out, 'assets/diagrams'), { recursive: true });
for (const file of ['style.css', 'app.js']) await cp(join(here, file), join(out, file));
await writeFile(join(out, '.nojekyll'), '');
const source = await readFile(join(root, 'tutorial/README.md'), 'utf8');
const chapters = chaptersFrom(source);
const manifest = JSON.parse(await readFile(join(root, 'assets/diagrams/manifest.json'), 'utf8'));
const escape = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const md = new MarkdownIt({ html: true, linkify: true, highlight(code, lang) {
  return lang && hljs.getLanguage(lang) ? hljs.highlight(code, { language: lang }).value : escape(code);
} });
md.renderer.rules.heading_open = (tokens, index) => {
  const title = tokens[index + 1].content;
  return `<${tokens[index].tag} id="${slug(title)}">`;
};
md.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  const href = tokens[index].attrGet('href');
  if (href) tokens[index].attrSet('href', sourceLink(href, 'tutorial/README.md'));
  return renderer.renderToken(tokens, index, options);
};
const originalFence = md.renderer.rules.fence;
md.renderer.rules.fence = (tokens, index, options, env, renderer) => {
  const lang = tokens[index].info.trim().split(' ')[0] || 'text';
  return `<div class="code-block"><div class="code-label"><span>${escape(lang)}</span><button class="copy" type="button" aria-label="Copy code">Copy</button></div>${originalFence(tokens, index, options, env, renderer)}</div>`;
};
function bodyHtml(body) {
  return md.render(body.replace(/<details>\s*<summary>Diagram source \(Mermaid\)<\/summary>[\s\S]*?<\/details>/g, '')
    .replace(/<p align="center"><img src="[^\"]*\/([^/\"]+\.svg)" alt="([^\"]*)"\s*\/><\/p>/g,
      '<figure class="diagram"><a href="assets/diagrams/$1" target="_blank" rel="noopener" aria-label="Open diagram at full size"><img src="assets/diagrams/$1" alt="$2" loading="lazy" /></a><figcaption>Open diagram at full size</figcaption></figure>'));
}
function header(active) {
  return `<header class="topbar"><a class="brand" href="index.html" aria-label="OpenReading home"><span class="brand-mark" aria-hidden="true">or<span>↗</span></span>openreading</a><nav aria-label="Main navigation"><a ${active === 'tutorial' ? 'aria-current="page"' : ''} href="tutorial.html">Field guide</a><a ${active === 'diagrams' ? 'aria-current="page"' : ''} href="diagrams.html">Diagrams</a><a href="${repository}">GitHub <span aria-hidden="true">↗</span></a></nav></header>`;
}
function page(title, active, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="description" content="Read, compare, and route documents through one API. A practical guide to OpenReading, from your first parse to a strategy that escalates on quality."><meta name="theme-color" content="#164bc5"><meta property="og:title" content="${escape(title)}"><meta property="og:description" content="One request. Your choice of backend. A response you can build on."><meta property="og:type" content="website"><title>${escape(title)} · OpenReading</title><link rel="stylesheet" href="style.css"><script src="app.js" defer></script></head><body class="${active}"><a class="skip-link" href="#main">Skip to content</a>${header(active)}${body}<footer><a class="brand" href="index.html">openreading</a><span>Your documents. Your backends. One contract.</span><a href="${repository}/blob/main/LICENSE">Apache 2.0</a></footer><div class="toast" role="status" aria-live="polite"></div></body></html>`;
}
const groups = [
  { title: 'Get a result', description: 'Install locally, read your first page, and understand the response.', range: [0, 4] },
  { title: 'Find the differences', description: 'Read the same document two ways. See what one backend misses.', range: [4, 7] },
  { title: 'Write your rules', description: 'Choose a backend order, escalate on quality, and inspect every decision.', range: [7, 12] },
  { title: 'Put it to work', description: 'Process a folder, add a vendor key, serve locally, and resume a run.', range: [12, 17] },
];
const home = `<main id="main"><section class="hero"><div class="hero-copy"><p class="intro-label">The OpenReading field guide</p><h1>Every document.<br>One clear<br>contract.</h1><p class="hero-description">Read, compare, and route documents through one API. Choose your backends. Keep the same response shape.</p><div class="hero-actions"><a class="button primary" href="tutorial.html#${chapters[0].id}">Start reading <span aria-hidden="true">↗</span></a><a class="text-link" href="#chapters">Explore the guide</a></div><p class="hero-note">Start on your machine with PyMuPDF.<br>Add OCR and hosted backends when you need them.</p></div><div class="hero-art"><div class="art-caption"><span>A document’s journey</span><span>One request / one response shape</span></div><img src="assets/diagrams/README-1.svg" alt="Your documents follow your backend rules and return a shared JSON shape for reading, comparison, and replay."><div class="art-bottom"><span class="status-dot"></span>Same contract. Whichever backend reads it.</div></div></section><section class="first-command"><div><span class="section-kicker">Your first result</span><h2>A page in.<br>Structured data out.</h2><p>After installation, one command reads the bundled example with a local backend.</p><a class="text-link" href="tutorial.html#2-your-first-parse">Walk through your first parse</a></div><div class="terminal"><div class="terminal-top"><span>Terminal</span><span>Local / PyMuPDF</span></div><div class="code-block"><div class="code-label"><span>bash</span><button class="copy" type="button">Copy</button></div><pre><code>uv run openreading parse \\\n  examples/schedule_a_2024.pdf \\\n  --backend pymupdf &gt; sa.json</code></pre></div><div class="result-line"><span class="status-dot"></span><code>succeeded</code><span>One JSON envelope in sa.json</span></div></div></section><section class="curriculum" id="chapters"><div class="section-heading"><h2>From a first parse<br>to a plan of your own.</h2><p>Follow the tutorial in order, or go straight to the question in front of you. Every command comes from the repository walkthrough.</p></div><div class="chapter-groups">${groups.map(g => `<section class="chapter-group"><span class="chapter-range">Steps ${g.range[0] + 1}–${g.range[1]}</span><h3>${g.title}</h3><p>${g.description}</p><ol start="${g.range[0] + 1}">${chapters.slice(...g.range).map(c => `<li><a href="tutorial.html#${c.id}"><span>${c.number}</span>${escape(c.short)}</a></li>`).join('')}</ol></section>`).join('')}</div></section><section class="closing"><h2>The code is the source.<br>The guide is your way in.</h2><p>This field guide is built directly from the tutorial README. Diagrams share the same vector artwork across the guide and the repository.</p><a class="button primary" href="tutorial.html">Open the field guide <span aria-hidden="true">↗</span></a></section></main>`;
await writeFile(join(out, 'index.html'), page('Every document. One clear contract.', 'home', home));
const navigation = groups.map(g => `<div class="nav-group"><p>${g.title}</p>${chapters.slice(...g.range).map(c => `<a href="#${c.id}" data-chapter="${c.id}"><span>${c.number}</span><span>${escape(c.short)}</span></a>`).join('')}</div>`).join('') + `<a href="#${chapters.at(-1).id}" data-chapter="${chapters.at(-1).id}"><span>A</span><span>The complete configuration</span></a>`;
const tutorial = `<div class="guide-layout"><aside class="sidebar"><div class="sidebar-heading"><a href="index.html">Field guide</a><button class="menu-toggle" type="button" aria-expanded="false" aria-controls="chapter-navigation">Chapters</button></div><div id="chapter-navigation"><label class="search"><span class="sr-only">Find a chapter</span><input type="search" placeholder="Find a chapter…" aria-label="Find a chapter"><span aria-hidden="true">/</span></label><nav aria-label="Tutorial chapters">${navigation}</nav><p class="no-results" hidden>No matching chapters. Try “parse” or “strategy”.</p><a class="source-link" href="${repository}/blob/main/tutorial/README.md">Read the source on GitHub ↗</a></div></aside><main id="main" class="guide-main"><div class="guide-intro"><p class="intro-label">A practical guide / 17 steps</p><h1>Make sense<br>of every page.</h1><p>Start with one local parse. Build toward a strategy that knows when to try another backend.</p><div class="guide-facts"><span>Python 3.11+</span><span>Local walkthrough</span><span>Vendor keys optional</span></div><details class="before-start"><summary>Before you start</summary>${md.render(source.slice(source.indexOf('> **What you get.'), source.indexOf('## Index')))}</details></div>${chapters.map((c, i) => `<article class="lesson" id="${c.id}" data-lesson><div class="lesson-heading"><span class="lesson-number">${c.number}</span><h2>${escape(c.short)}</h2><a href="#${c.id}" aria-label="Link to this chapter">#</a></div><div class="prose">${bodyHtml(c.body)}</div>${i < chapters.length - 1 ? `<a class="next-lesson" href="#${chapters[i + 1].id}"><span>Up next</span>${escape(chapters[i + 1].short)} <span aria-hidden="true">↓</span></a>` : ''}</article>`).join('')}</main><aside class="reading-rail"><span>In this chapter</span><nav aria-label="Section navigation" id="section-navigation"></nav><div class="reading-progress"><span id="progress-label">Step 1 of 17</span><progress value="1" max="17" aria-label="Current tutorial step"></progress></div></aside></div>`;
await writeFile(join(out, 'tutorial.html'), page('The field guide', 'tutorial', tutorial));
const gallery = `<main id="main" class="gallery"><div class="gallery-heading"><p class="intro-label">The visual reference</p><h1>A system you<br>can follow.</h1><p>Eleven diagrams. One visual language. Open any diagram to inspect the vector artwork at full size.</p><div class="legend"><span><i class="legend-work"></i>Processing</span><span><i class="legend-gate"></i>Decision</span><span><i class="legend-good"></i>Accepted / retained</span><span><i class="legend-bad"></i>Error / omitted</span><span><i class="legend-hero"></i>Shared result</span></div></div><div class="diagram-grid">${manifest.map(d => `<figure><div class="gallery-figure-heading"><h2>${escape(d.title.replace(/`/g, ''))}</h2><a href="assets/diagrams/${d.file}" target="_blank" rel="noopener" aria-label="Open ${escape(d.title)} SVG">Open SVG ↗</a></div><a class="gallery-image" href="assets/diagrams/${d.file}" target="_blank" rel="noopener"><img src="assets/diagrams/${d.file}" alt="${escape(d.title)}" loading="lazy"></a><figcaption><a href="${repository}/blob/main/${d.source}">${escape(d.source)}</a><span>SVG / resolution independent</span></figcaption></figure>`).join('')}</div></main>`;
await writeFile(join(out, 'diagrams.html'), page('Diagram atlas', 'diagrams', gallery));
console.log(`Built ${chapters.length} chapters and ${manifest.length} diagrams in ${out}`);
