from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json
import math
import os
import shutil
import threading
import time
from typing import Callable, Iterable

MODEL_ID = "facebook/dinov2-small"

_MODEL_LOCK = threading.Lock()
_MODEL_STATE = {"device_key": None, "runtime_device": None, "model": None, "processor": None, "torch": None, "device_label": None}


def _device_details(torch) -> list[dict]:
    """Return available inference paths without assuming CUDA means NVIDIA.

    PyTorch's ROCm build intentionally uses the torch.cuda API surface, so AMD
    ROCm devices are detected via torch.version.hip and/or the reported GPU name.
    """
    cpu_threads = max(1, int(os.cpu_count() or 1))
    details = [{"key": "cpu", "label": f"CPU ({cpu_threads} logical threads)", "backend": "cpu"}]

    if torch.cuda.is_available():
        try:
            gpu_name = str(torch.cuda.get_device_name(0))
        except Exception:
            gpu_name = "GPU 0"
        hip_version = getattr(torch.version, "hip", None)
        looks_amd = bool(hip_version) or any(x in gpu_name.lower() for x in ("amd", "radeon"))
        if looks_amd:
            label = f"AMD ROCm GPU — {gpu_name}"
            if hip_version:
                label += f" (HIP {hip_version})"
            details.append({"key": "rocm", "label": label, "backend": "rocm", "gpuName": gpu_name})
        else:
            cuda_version = getattr(torch.version, "cuda", None)
            label = f"NVIDIA CUDA GPU — {gpu_name}"
            if cuda_version:
                label += f" (CUDA {cuda_version})"
            details.append({"key": "cuda", "label": label, "backend": "cuda", "gpuName": gpu_name})

    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        details.append({"key": "mps", "label": "Apple MPS GPU", "backend": "mps"})

    # Optional Windows fallback. It is deliberately not a hard dependency because
    # torch-directml can impose PyTorch version constraints. ROCm is preferred on
    # supported AMD Ryzen/Radeon hardware.
    try:
        import torch_directml  # type: ignore
        _ = torch_directml.device()
        details.append({"key": "directml", "label": "DirectML GPU (Windows)", "backend": "directml"})
    except Exception:
        pass

    return details


def check_environment() -> dict:
    info = {"ready": False, "modelId": MODEL_ID}
    missing = []
    versions = {}
    for mod_name, label in [("torch", "torch"), ("torchvision", "torchvision"), ("transformers", "transformers"), ("PIL", "Pillow"), ("numpy", "numpy")]:
        try:
            mod = __import__(mod_name)
            versions[label] = getattr(mod, "__version__", "installed")
        except Exception:
            missing.append(label)
    info["versions"] = versions
    info["missing"] = missing
    if missing:
        info["message"] = "Missing AI packages: " + ", ".join(missing)
        return info
    try:
        import torch
        details = _device_details(torch)
        keys = [d["key"] for d in details]
        preferred_order = ("rocm", "cuda", "mps", "directml", "cpu")
        preferred_key = next((k for k in preferred_order if k in keys), "cpu")
        preferred = next(d for d in details if d["key"] == preferred_key)
        info["devices"] = keys
        info["deviceDetails"] = details
        info["preferredDevice"] = preferred_key
        info["preferredDeviceLabel"] = preferred["label"]
        info["cpuThreads"] = max(1, int(os.cpu_count() or 1))
        info["hipVersion"] = getattr(torch.version, "hip", None)
        info["cudaVersion"] = getattr(torch.version, "cuda", None)
        info["cudaName"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except Exception as exc:
        info["message"] = f"AI packages import failed: {exc}"
        return info
    info["ready"] = True
    info["message"] = "AI dependencies are installed. The DINOv2 model downloads automatically on the first ranking run."
    return info


@dataclass(frozen=True)
class DeviceSpec:
    key: str
    runtime_device: object
    label: str
    backend: str


def choose_device(requested: str, torch) -> DeviceSpec:
    requested = (requested or "auto").lower()
    aliases = {"amd": "rocm", "dml": "directml", "gpu": "auto"}
    requested = aliases.get(requested, requested)
    details = _device_details(torch)
    by_key = {d["key"]: d for d in details}

    if requested == "auto":
        for key in ("rocm", "cuda", "mps", "directml", "cpu"):
            if key in by_key:
                requested = key
                break

    if requested not in by_key:
        available = ", ".join(d["label"] for d in details)
        if requested == "rocm":
            raise RuntimeError(f"AMD ROCm GPU was requested, but this PyTorch installation does not expose an AMD ROCm device. Available: {available}")
        if requested == "cuda":
            raise RuntimeError(f"NVIDIA CUDA GPU was requested, but CUDA is not available. Available: {available}")
        if requested == "directml":
            raise RuntimeError(f"DirectML was requested, but torch-directml is not installed/available. Available: {available}")
        if requested == "mps":
            raise RuntimeError(f"Apple MPS was requested, but it is not available. Available: {available}")
        raise RuntimeError(f"Unsupported AI device: {requested}. Available: {available}")

    detail = by_key[requested]
    if requested == "cpu":
        runtime = torch.device("cpu")
        # PyTorch already uses threaded kernels, but make the intent explicit so a
        # large Ryzen AI Halo CPU can use all logical processors unless the runtime
        # itself imposes a lower limit.
        try:
            torch.set_num_threads(max(1, int(os.cpu_count() or 1)))
        except Exception:
            pass
    elif requested in {"rocm", "cuda"}:
        # ROCm preserves PyTorch's torch.cuda device API for compatibility.
        runtime = torch.device("cuda:0")
    elif requested == "mps":
        runtime = torch.device("mps")
    elif requested == "directml":
        import torch_directml  # type: ignore
        runtime = torch_directml.device()
    else:  # pragma: no cover - guarded above
        raise RuntimeError(f"Unsupported AI device: {requested}")

    return DeviceSpec(key=requested, runtime_device=runtime, label=detail["label"], backend=detail["backend"])


def load_model(requested_device: str = "auto"):
    try:
        import torch
        from transformers import AutoImageProcessor, AutoModel
    except Exception as exc:
        raise RuntimeError(
            "AI dependencies are not installed. Run: python -m pip install -r requirements-ai.txt"
        ) from exc

    spec = choose_device(requested_device, torch)
    with _MODEL_LOCK:
        if _MODEL_STATE["model"] is not None and _MODEL_STATE["device_key"] == spec.key:
            return _MODEL_STATE["torch"], _MODEL_STATE["processor"], _MODEL_STATE["model"], spec

        processor = _MODEL_STATE["processor"] or AutoImageProcessor.from_pretrained(MODEL_ID)
        model = _MODEL_STATE["model"]
        if model is None:
            model = AutoModel.from_pretrained(MODEL_ID)
        else:
            # Moving between some third-party backends can fail. Reloading is safer
            # than leaving a half-moved model in cache.
            try:
                model.to("cpu")
            except Exception:
                model = AutoModel.from_pretrained(MODEL_ID)
        model.eval()
        model.to(spec.runtime_device)
        _MODEL_STATE.update({
            "device_key": spec.key,
            "runtime_device": spec.runtime_device,
            "device_label": spec.label,
            "model": model,
            "processor": processor,
            "torch": torch,
        })
        return torch, processor, model, spec


def _safe_rgb(image):
    from PIL import Image
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", image.size, (127, 127, 127))
        alpha = image.getchannel("A") if "A" in image.getbands() else None
        bg.paste(image.convert("RGB"), mask=alpha)
        return bg
    return image.convert("RGB")


def _letterbox_square(image, size: int = 224):
    from PIL import Image, ImageOps
    im = _safe_rgb(image)
    contained = ImageOps.contain(im, (size, size), method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (size, size), (127, 127, 127))
    x = (size - contained.width) // 2
    y = (size - contained.height) // 2
    canvas.paste(contained, (x, y))
    return canvas


def _positions(max_offset: int, count: int) -> list[int]:
    if max_offset <= 0 or count <= 1:
        return [0]
    vals = [round(max_offset * i / (count - 1)) for i in range(count)]
    out = []
    for v in vals:
        if v not in out:
            out.append(v)
    return out


def candidate_crops(image, target_aspect: float, mode: str):
    """Return (label, PIL image) crops.

    The target photograph may occupy only part of a Mapillary image. We therefore
    search each candidate using aspect-matched windows at several horizontal
    locations. Thorough mode adds smaller vertical scales and multiple y positions.
    """
    im = _safe_rgb(image)
    w, h = im.size
    target_aspect = max(0.2, min(float(target_aspect), 2.0))
    crops = [("full", im.copy())]

    if mode == "fast":
        scales = [(1.0, 1)]
        x_count = 5
    else:
        scales = [(1.0, 1), (0.78, 3), (0.60, 3)]
        x_count = 5

    for scale, y_count in scales:
        crop_h = max(64, min(h, int(round(h * scale))))
        crop_w = max(64, int(round(crop_h * target_aspect)))
        if crop_w > w:
            crop_w = w
            crop_h = max(64, min(h, int(round(crop_w / target_aspect))))
        xs = _positions(max(0, w - crop_w), x_count)
        ys = _positions(max(0, h - crop_h), y_count)
        for yi, y in enumerate(ys):
            for xi, x in enumerate(xs):
                box = (x, y, x + crop_w, y + crop_h)
                label = f"s{scale:.2f}_x{xi}_y{yi}"
                crops.append((label, im.crop(box)))

    return crops


def _embed_images(images: list, processor, model, torch, runtime_device):
    prepared = [_letterbox_square(im, 224) for im in images]
    inputs = processor(images=prepared, return_tensors="pt")
    inputs = {k: v.to(runtime_device) for k, v in inputs.items()}
    with torch.inference_mode():
        outputs = model(**inputs)
        emb = getattr(outputs, "pooler_output", None)
        if emb is None:
            emb = outputs.last_hidden_state[:, 0]
        emb = torch.nn.functional.normalize(emb.float(), dim=-1)
    return emb


def load_target_embedding(target_path: Path, processor, model, torch, runtime_device):
    from PIL import Image
    with Image.open(target_path) as im:
        target = _safe_rgb(im.copy())
    target_aspect = target.width / max(1, target.height)
    emb = _embed_images([target], processor, model, torch, runtime_device)[0]
    return emb, target_aspect


def graph_image_metadata(image_id: str, token: str, thumb_size: int, graph_url: str = "https://graph.mapillary.com") -> dict:
    field = "thumb_2048_url" if int(thumb_size) >= 2048 else "thumb_1024_url"
    url = f"{graph_url}/{image_id}?fields=id,{field},thumb_1024_url,thumb_2048_url"
    req = Request(url, headers={"Authorization": f"OAuth {token}", "Accept": "application/json", "User-Agent": "ImageGeolocationEstimationApp/2.4"})
    try:
        with urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Mapillary metadata HTTP {exc.code} for image {image_id}: {body[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Mapillary metadata request failed for image {image_id}: {exc.reason}") from exc


def download_thumbnail(image_id: str, token: str, thumb_size: int, cache_root: Path, keep_cache: bool):
    from PIL import Image
    cache_dir = cache_root / f"thumb_{2048 if int(thumb_size) >= 2048 else 1024}"
    cache_file = cache_dir / f"{image_id}.jpg"
    if cache_file.exists() and cache_file.stat().st_size > 1000:
        try:
            with Image.open(cache_file) as im:
                return _safe_rgb(im.copy()), True
        except Exception:
            try:
                cache_file.unlink()
            except OSError:
                pass

    meta = graph_image_metadata(image_id, token, thumb_size)
    key = "thumb_2048_url" if int(thumb_size) >= 2048 else "thumb_1024_url"
    url = meta.get(key) or meta.get("thumb_1024_url") or meta.get("thumb_2048_url")
    if not url:
        raise RuntimeError(f"Mapillary did not return a thumbnail URL for image {image_id}.")
    req = Request(url, headers={"User-Agent": "ImageGeolocationEstimationApp/2.4"})
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except HTTPError as exc:
        raise RuntimeError(f"Thumbnail HTTP {exc.code} for image {image_id}.") from exc
    except URLError as exc:
        raise RuntimeError(f"Thumbnail download failed for image {image_id}: {exc.reason}") from exc

    try:
        with Image.open(BytesIO(raw)) as im:
            image = _safe_rgb(im.copy())
    except Exception as exc:
        raise RuntimeError(f"Downloaded thumbnail for image {image_id} is not a readable image.") from exc

    if keep_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(cache_file)
    return image, False


def cache_info(cache_root: Path) -> dict:
    if not cache_root.exists():
        return {"files": 0, "bytes": 0}
    files = 0
    size = 0
    for p in cache_root.rglob("*"):
        if p.is_file():
            files += 1
            try:
                size += p.stat().st_size
            except OSError:
                pass
    return {"files": files, "bytes": size}


def clear_cache(cache_root: Path) -> dict:
    before = cache_info(cache_root)
    if cache_root.exists():
        shutil.rmtree(cache_root, ignore_errors=True)
    return before


@dataclass
class RankSettings:
    mode: str = "fast"
    thumb_size: int = 1024
    device: str = "auto"
    max_images: int = 0
    keep_cache: bool = True


def rank_images(
    images: list[dict],
    token: str,
    target_path: Path,
    cache_root: Path,
    settings: RankSettings,
    progress: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    if not target_path.exists():
        raise RuntimeError(f"Target image was not found: {target_path}")
    mode = (settings.mode or "fast").lower()
    if mode not in {"fast", "thorough"}:
        raise RuntimeError("AI comparison mode must be fast or thorough.")
    thumb_size = 2048 if int(settings.thumb_size) >= 2048 else 1024
    max_images = max(0, int(settings.max_images or 0))
    work = list(images[:max_images] if max_images else images)
    if not work:
        raise RuntimeError("There are no images to rank.")

    started = time.time()
    if progress:
        progress({"phase": "model", "message": f"Loading {MODEL_ID}…", "completed": 0, "total": len(work)})
    torch, processor, model, device_spec = load_model(settings.device)
    runtime_device = device_spec.runtime_device
    target_emb, target_aspect = load_target_embedding(target_path, processor, model, torch, runtime_device)

    results = []
    failures = []
    cache_hits = 0
    for idx, im in enumerate(work, start=1):
        if cancel_event is not None and cancel_event.is_set():
            break
        image_id = str(im.get("id") or "")
        if not image_id:
            failures.append({"id": "", "error": "Missing image ID"})
            continue
        try:
            candidate, hit = download_thumbnail(image_id, token, thumb_size, cache_root, settings.keep_cache)
            if hit:
                cache_hits += 1
            crops = candidate_crops(candidate, target_aspect, mode)
            labels = [x[0] for x in crops]
            crop_images = [x[1] for x in crops]
            emb = _embed_images(crop_images, processor, model, torch, runtime_device)
            sims = (emb @ target_emb).detach().float().cpu()
            best_idx = int(torch.argmax(sims).item())
            score = float(sims[best_idx].item())
            results.append({
                "id": image_id,
                "score": score,
                "bestCrop": labels[best_idx],
                "cropCount": len(crops),
                "model": MODEL_ID,
                "mode": mode,
                "thumbSize": thumb_size,
                "device": device_spec.label,
                "deviceKey": device_spec.key,
            })
        except Exception as exc:
            failures.append({"id": image_id, "error": str(exc)})

        elapsed = max(0.001, time.time() - started)
        completed = idx
        rate = completed / elapsed
        eta = (len(work) - completed) / rate if rate > 0 else None
        if progress:
            top = sorted(results, key=lambda x: x["score"], reverse=True)[:10]
            progress({
                "phase": "ranking",
                "message": f"Scored {completed:,} of {len(work):,} images",
                "completed": completed,
                "total": len(work),
                "failed": len(failures),
                "cacheHits": cache_hits,
                "currentImageId": image_id,
                "elapsedSec": elapsed,
                "imagesPerSec": rate,
                "etaSec": eta,
                "top": top,
                "device": device_spec.label,
                "deviceKey": device_spec.key,
            })

    cancelled = bool(cancel_event is not None and cancel_event.is_set())
    results.sort(key=lambda x: x["score"], reverse=True)
    elapsed = time.time() - started
    return {
        "status": "cancelled" if cancelled else "complete",
        "model": MODEL_ID,
        "mode": mode,
        "thumbSize": thumb_size,
        "device": device_spec.label,
                "deviceKey": device_spec.key,
        "requested": len(work),
        "scored": len(results),
        "failed": len(failures),
        "cacheHits": cache_hits,
        "elapsedSec": elapsed,
        "scores": results,
        "failures": failures[:100],
    }
