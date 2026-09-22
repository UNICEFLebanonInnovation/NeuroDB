# Vendored front-end libraries

Downloaded from the npm registry with `npm pack <package>@<version>`; only the dist files listed
here are kept (source maps stripped). No CDN is used anywhere; WhiteNoise serves these files.
To upgrade: `npm pack <package>@<new version>`, copy the same files, update this table.

| Package | Version | License | Files | Used by |
|---|---|---|---|---|
| bootstrap | 5.3.8 | MIT | bootstrap/bootstrap.min.css, bootstrap/bootstrap.bundle.min.js | every page |
| htmx.org | 2.0.10 | 0BSD | htmx/htmx.min.js | every page (partials) |
| jquery | 3.7.1 | MIT | jquery/jquery.min.js | pivot pages only (pivottable dependency) |
| pivottable | 2.23.0 | MIT | pivottable/pivot.min.js, pivot.min.css, plotly_renderers.min.js, export_renderers.min.js | analytical pages |
| plotly.js-basic-dist-min | 2.35.3 | MIT | plotly/plotly-basic.min.js | charts, pivot chart renderers |
| maplibre-gl | 4.7.1 | BSD-3-Clause | maplibre-gl/maplibre-gl-csp.js, maplibre-gl-csp-worker.js, maplibre-gl.css | intervention map (CSP build: worker loaded from the same origin, no blob: needed) |
| tom-select | 2.6.2 | Apache-2.0 | tom-select/tom-select.complete.min.js, tom-select.bootstrap5.min.css | multi-select filters |
