# Third-party integrations

## dsh-pet

- Upstream: https://github.com/PC2005-cloud/dsh-pet
- Vendored and tested version: 0.4.1, under `vendor/dsh-pet/upstream/` (published package without node_modules).
- Code copyright: (c) 2026 PC2005-cloud
- Code license: MIT; the installed LICENSE is copied alongside the prepared runtime.
- Assets: upstream README states non-commercial use and attribution for public derivatives. At the user's request, the 109 WebM clips are included in `vendor/dsh-pet/upstream/assets/webm/`, with the upstream README and license preserved. Updated packages are stored separately under `.local/plugins/`.

The adapter prepares a local copy of the Electron renderer, preserving the upstream code license. Changes isolate the Chromium profile, direct host actions to Whale Friend, hide features not connected to this application, and route chat/state through the existing Companion and SQLite store. Whale Friend adds an editable clip catalog and model-selected animation events, plus desktop/mobile avatar players. The installed DSH package, credentials and histories are left unchanged.

## edge-tts

- Upstream: https://github.com/rany2/edge-tts
- Python dependency, LGPL-3.0; no source is vendored or modified here.
- Uses Microsoft Edge's online speech service. Speech is optional and disabled by default; enabling it sends speech text to that service. The application caches generated MP3 files locally, outside memory backups.

## PyAV

- Upstream: https://github.com/PyAV-Org/PyAV
- Python dependency, BSD-3-Clause; no source is vendored or modified here.
- Binary wheels include FFmpeg/libvpx components with their own licenses; retain the wheel's included license files when redistributing. Used only for local clip decoding; no video is sent to a model or outside service.
