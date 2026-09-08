# OpenReading field guide preview

You can review the tutorial website locally, then move this folder into a separate repository when its design is ready.
The build reads the tutorial directly from the core checkout, so edits to that walkthrough appear on the next build.

```sh
cd website
PUPPETEER_SKIP_DOWNLOAD=true npm ci
npm run build
npm run preview
```

Open `http://localhost:4173` for the introduction, tutorial, and diagram gallery.
The website uses relative asset paths, so the same build supports a GitHub Pages repository prefix.
Fonts load from Google Fonts, with local fallbacks when you are offline.

```sh
npm run diagrams
npm run build
npm test
```

The diagram command requires Chrome through Puppeteer, or a local executable selected by `MERMAID_CHROME`.
It refreshes SVG files in `assets/diagrams/` and the Mermaid sources embedded in the owning READMEs.
SVG is a vector format, so these illustrations stay sharp at any zoom level without fixed pixel dimensions.
The shared palette and diagram spacing live in `theme.mjs` beside the generator that applies them.

For a separate repository, move this folder's contents into its root and copy `pages.yml` to `.github/workflows/pages.yml`.
Select GitHub Actions under Settings, Pages, then run the workflow with the core revision you want to publish.
That revision must contain the generated artwork under `assets/diagrams/`, including its manifest.
The workflow remains inactive here because deployment belongs to the separate website repository.

For a separate local checkout, run `npm run build -- --source /path/to/openreading-core` before starting the preview.
The diagram generator accepts the same `--source` argument when you need to refresh artwork in that separate core checkout.
The build writes the complete static website into `dist/`, which is ignored and can also be hosted by another static server.
