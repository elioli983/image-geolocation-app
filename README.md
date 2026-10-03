# Image Geolocation Estimation App v2.4

A local browser application for discovering Mapillary imagery around a user-supplied center/radius, reviewing it beside a user-selected target image, and ranking the imagery with local DINOv2 visual-similarity analysis.

> **POC status:** This remains a proof of concept. The current build has been tested on Windows. Linux and macOS remain untested.

## What's new in v2.4

### AI timing and performance status

- AI ranking now records and displays the exact **start time**, **finish time**, and **elapsed duration** for Fast and Thorough runs.
- Completion status also reports scored images, failures, cache hits, average throughput, and compute device.
- The latest AI-run timing/performance statistics are saved with a saved run and are included in CSV exports.

### Large Area Search cell list

- The Large Area Search cell table now renders **all generated cells** in a fixed-height scrollable list instead of reducing the post-run view to a small subset.
- The header remains sticky while scrolling.
- When a new cell starts running, the list automatically brings that cell into view once; manual scrolling remains usable afterward.
- Failed and currently running cells receive visual emphasis.

### Post-discovery AI image-type filter

- AI ranking can now be limited after discovery to **Both panorama and flat images**, **Panoramas only**, or **Flat images only**.
- The application displays the available counts for each type before AI runs.
- Zero-count Pano/Flat choices are disabled, and both browser and server prevent a zero-image AI job from starting.
- The original candidate set remains intact. AI similarity sorting ranks the images scored by the most recent AI run first while preserving the remainder of the candidate set.
- The selected AI image type is stored with AI scores.

### Validation improvements

- Large Area Search now gives a clearer message when a radius of 1,500 m or less is entered and directs the user to Standard Search.
- Target-image upload performs a browser decode check before upload; the server also verifies decodability with Pillow when Pillow is installed.
- Corrupt JPEG/PNG/WebP files now receive a clearer error rather than failing later during AI processing.

### v2.3 Large Area Search retained

- **Standard Search** and **Large Area Search** remain separate modes.
- Large Area Search accepts an overall radius from **1,501 m to 50,000 m** and creates overlapping 1,500 m cells using the existing triangular/hexagonal covering pattern.
- Cells continue to be checkpointed in SQLite with Pause, Cancel, Resume, Retry failed cells, and global Mapillary image-ID deduplication.

### v2.2 improvements retained

- The **AI top-match list now has an Add to shortlist button beside Go** for every displayed match.
- The application header now describes both the Mapillary discovery and AI visual-similarity/geolocation workflow.
- **AMD GPU acceleration support was added.** The application now detects an AMD ROCm-enabled PyTorch installation and exposes it as **AMD ROCm GPU** instead of incorrectly treating every `torch.cuda` device as NVIDIA CUDA.
- **Auto device selection** prefers AMD ROCm, then NVIDIA CUDA, Apple MPS, DirectML, and finally CPU.
- CPU inference explicitly configures PyTorch to use all available logical CPU threads, which is useful on high-core-count systems such as Ryzen AI Halo.
- Optional **DirectML GPU** support is detected when `torch-directml` is already installed. DirectML is a fallback; on supported AMD Ryzen/Radeon hardware, ROCm is preferred.
- Fixed **Sort by AI similarity** and **Restore distance order**. Each button now visibly reorders the current image list and jumps to item #1 in the selected order. The UI also displays the active ordering mode.
- The exact original distance order is retained so it can be restored after AI sorting.

## v2.1 behavior retained

- Center latitude and longitude start blank and are entered manually.
- Existing defaults remain:
  - Radius: `402.336 m`
  - Image type: `All imagery`
  - Captured on/after: blank
  - Captured on/before: blank
  - Maximum API images: `20000`
- Target images are selected from the computer/device in the browser. JPEG, PNG, and WebP are supported up to 30 MB.
- There is no bundled target photograph. Select a target in the **Target photograph** panel before running AI ranking.

## Target image workflow

1. Click the file chooser in **Target photograph**.
2. Select a JPEG, PNG, or WebP image.
3. Click **Use selected target**.
4. The target is uploaded to the local Python server and displayed in the target panel.
5. AI ranking uses the uploaded target.

Uploaded targets are stored under the local `target_uploads/` directory. The current target is tracked by `target_uploads/current.json`.

> v2.4 is still a single-user/local architecture. A future shared web deployment should isolate target uploads, runs, shortlists, and AI jobs per authenticated user/session.

## Mapillary discovery

Enter:

- Center latitude
- Center longitude
- Radius
- Image type
- Optional capture start/end dates
- Maximum API images

The server computes the circle's bounding box, queries the Mapillary Graph API, filters results back to the exact circle using great-circle distance, deduplicates image IDs, and sorts the initial run by distance from the center.

If Mapillary returns its "Please reduce the amount of data" error, the application automatically retries using smaller API pages and recursively subdivides dense geographic areas.

## Large Area Search workflow

1. Select **Large Area Search** in the Search mode menu.
2. Enter the overall center latitude/longitude and an overall radius larger than 1,500 m.
3. Enter an optional job name and choose the normal image/date filters. **Maximum API images** is applied per 1,500 m cell.
4. Click **Start Large Area Search**.
5. The server generates the overlapping cells first, stores them in SQLite, then processes them one at a time.
6. The selected job can be paused or cancelled safely; completed cells remain saved.
7. Use **Resume** to continue pending cells or **Retry failed cells** to retry only failed cells.
8. When useful results are available, click **Load combined results**. The deduplicated image set becomes the current reviewer run.
9. Run **Fast multi-crop** on the combined candidate set, shortlist promising images, then use **Thorough multi-crop** on the shortlist.

Large Area Search intentionally does not run DINOv2 once per cell. Discovery and deduplication finish first; AI is then run once against the combined candidate set.

## AI ranking

The optional AI stage uses `facebook/dinov2-small` through Hugging Face Transformers.

### Fast multi-crop

Recommended first pass. Compares the target against the full Mapillary image and multiple portrait-oriented candidate crops.

### Thorough multi-crop

Uses additional crop scales and vertical positions. It is best run on a reduced candidate set such as the shortlist.

Recommended workflow:

1. Discover imagery.
2. Run **Fast multi-crop** on the full run.
3. Use the new **Add to shortlist** buttons in the AI top-match table for promising candidates.
4. Optionally use **Sort by AI similarity** to browse the full run from highest to lowest score.
5. Load the shortlist.
6. Run **Thorough multi-crop** on the shortlist.

## Compute-device support

The **Compute device** menu supports:

- **Auto** — prefers ROCm, CUDA, MPS, DirectML, then CPU.
- **CPU** — PyTorch uses all available logical CPU threads.
- **AMD ROCm GPU** — preferred GPU path for supported AMD Ryzen/Radeon hardware.
- **NVIDIA CUDA GPU**.
- **DirectML GPU (Windows fallback)** — shown only when `torch-directml` is installed and usable.
- **Apple MPS**.

### AMD Ryzen AI Halo / Ryzen AI Max

For AMD GPU acceleration, the Python environment must contain an AMD ROCm-enabled PyTorch build. PyTorch ROCm intentionally exposes the GPU through the `torch.cuda` API for compatibility; v2.4 detects `torch.version.hip` and the AMD/Radeon device name so the UI correctly identifies it as **AMD ROCm GPU**.

The generic `install_ai.ps1` installs normal PyPI dependencies. If configuring a supported AMD Ryzen AI/Radeon system for ROCm, install AMD's current Windows/Linux ROCm PyTorch package according to AMD's official instructions first, then install the remaining application dependencies. Do not replace a working ROCm PyTorch installation with an unrelated CUDA build.

DirectML can be used as a Windows fallback by installing `torch-directml`, but it is not included in `requirements-ai.txt` because it can constrain or replace the installed PyTorch version. On a supported Ryzen AI Halo system, ROCm is the preferred path.

The app currently targets CPU/GPU acceleration; it does not yet offload DINOv2 to the Ryzen AI NPU.

To verify what the application sees, run:

```powershell
python .\check_ai_hardware.py
```

## Sort behavior

- **Sort by AI similarity** orders scored images from highest similarity to lowest; unscored images follow scored images. The viewer moves to the new #1 result.
- **Restore distance order** restores the original nearest-to-farthest run ordering and moves the viewer to #1.
- The active ordering appears beside the sort controls.

## Files

- `serve.py` — local web server, Mapillary API proxy, target upload API, SQLite persistence, checkpointed Large Area Search orchestration, AI job API.
- `ai_matcher.py` — DINOv2 model loading, hardware detection, thumbnail retrieval/cache, crop generation, similarity scoring.
- `index.html` — browser UI.
- `app.js` — reviewer, target upload, shortlist, sorting, and AI UI logic.
- `style.css` — styling.
- `requirements-ai.txt` — optional cross-platform AI dependencies.
- `check_ai_hardware.py` — reports the PyTorch backend/device paths visible to the application.
- `install_ai.ps1` — PowerShell AI installer.
- `config.json` — optional local Mapillary token configuration.
- `config.example.json` — token example.
- `mapillary_runs.sqlite3` — created automatically beside `serve.py`.
- `ai_cache/` — optional Mapillary AI-thumbnail cache.
- `target_uploads/` — created automatically after selecting a target.

## Mapillary token

Set the token in PowerShell before starting the application:

```powershell
$env:MAPILLARY_ACCESS_TOKEN="MLY|YOUR_TOKEN_HERE"
python .\serve.py
```

or place it in `config.json`:

```json
{
  "mapillary_access_token": "MLY|YOUR_TOKEN_HERE"
}
```

The current local version exposes the token to MapillaryJS in the browser because MapillaryJS requires an access token client-side. Do **not** publish this version unchanged if the Mapillary token must remain secret from web users. A production web deployment should use a server-side secret architecture and either avoid the direct MapillaryJS platform viewer, require each tester's own token for that viewer, or use another supported authentication/data-provider arrangement.

## Install AI dependencies

```powershell
.\install_ai.ps1
```

or:

```powershell
python -m pip install -r .\requirements-ai.txt
```

The requirements include:

- PyTorch
- Torchvision
- Transformers
- Pillow
- NumPy
- Safetensors

The DINOv2 model downloads automatically on the first ranking run and is reused from the Hugging Face cache afterward.

## Start

```powershell
python .\serve.py
```

Then open:

```text
http://localhost:8000/
```

To use another port:

```powershell
python .\serve.py --port 8001
```

## Storage

Saved runs and the shortlist are stored in:

```text
mapillary_runs.sqlite3
```

AI thumbnails can be cached under:

```text
ai_cache\thumb_1024\
ai_cache\thumb_2048\
```

The uploaded target is stored under:

```text
target_uploads\
```

## Web-deployment note

The application can be adapted for a small group of web testers, but the local v2.4 architecture is not yet the final multi-user deployment build. A production/shared version should add authentication, per-user data isolation, server-side secrets, HTTPS, persistent server storage, request limits, and concurrency controls for AI jobs.

## License

Copyright (C) 2026 elioli983

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the [LICENSE](LICENSE) file for the full text of the GNU General Public License v3.0.

### Mapillary

This project is not affiliated with, endorsed by, or sponsored by Mapillary or Meta. Mapillary imagery, metadata, APIs, MapillaryJS, and access tokens remain subject to [Mapillary's own terms](https://www.mapillary.com/terms), and imagery is licensed by Mapillary under its own terms (currently CC BY-SA 4.0). The GPL license of this project does not apply to, and grants no rights in, any Mapillary content or service. Each user must obtain their own Mapillary access token and is responsible for complying with Mapillary's terms, including attribution and API usage limits.

### Third-party software and models

This project's GPL license covers only the code in this repository. The third-party libraries and models it uses are not included in this repository; they are downloaded separately and remain subject to their own licenses, which are listed below as published by each project at the time of writing. Check each project for current terms.

| Component | How it is used | License |
|---|---|---|
| [MapillaryJS](https://github.com/mapillary/mapillary-js) | Image viewer, loaded from a CDN | MIT |
| [PyTorch](https://pytorch.org) / [torchvision](https://github.com/pytorch/vision) | AI inference | BSD-3-Clause |
| [Hugging Face Transformers](https://github.com/huggingface/transformers) | Model loading | Apache-2.0 |
| [safetensors](https://github.com/huggingface/safetensors) | Model weight format | Apache-2.0 |
| [NumPy](https://numpy.org) | Numerical processing | BSD-3-Clause |
| [Pillow](https://python-pillow.org) | Image decoding | MIT-CMU (HPND) |
| [DINOv2 (`facebook/dinov2-small`)](https://huggingface.co/facebook/dinov2-small) | Visual-similarity model weights, downloaded on first use | Apache-2.0 |
