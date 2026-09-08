/** Preserve tutorial bodies while deriving chapter navigation and portable repository links. */
import { posix } from 'node:path';
export const repository = 'https://github.com/multiversal-ventures/openreading-core';
export function slug(text) {
  return text.toLowerCase().replace(/[^\p{L}\p{N}_\-\s]/gu, '').replace(/\s/g, '-');
}
export function chaptersFrom(source) {
  const lines = source.split('\n');
  const starts = [];
  let fence = false;
  lines.forEach((line, index) => {
    if (line.startsWith('```')) fence = !fence;
    if (!fence && /^## (\d+\.|Appendix:)/.test(line)) starts.push(index);
  });
  return starts.map((start, index) => {
    const title = lines[start].slice(3);
    return { title, id: slug(title), number: index < 17 ? String(index + 1).padStart(2, '0') : 'A',
      short: title.replace(/^\d+\. /, '').replace(/`/g, ''),
      body: lines.slice(start + 1, starts[index + 1] ?? lines.length).join('\n') };
  });
}
export function sourceLink(href, source) {
  if (/^(?:[a-z]+:|#|\/\/)/i.test(href)) return href;
  return `${repository}/blob/main/${posix.normalize(posix.join(posix.dirname(source), href))}`;
}
