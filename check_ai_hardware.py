from __future__ import annotations

import json
import os
import platform
import sys


def main() -> None:
    print("Image Geolocation Estimation App v2.4 - AI hardware check")
    print(f"Python: {sys.version.split()[0]}")
    print(f"OS: {platform.platform()}")
    print(f"Logical CPU threads: {os.cpu_count() or 'unknown'}")
    try:
        import torch
    except Exception as exc:
        print(f"PyTorch: unavailable ({exc})")
        return

    print(f"PyTorch: {torch.__version__}")
    print(f"torch.cuda.is_available(): {torch.cuda.is_available()}")
    print(f"torch.version.hip: {getattr(torch.version, 'hip', None)}")
    print(f"torch.version.cuda: {getattr(torch.version, 'cuda', None)}")
    if torch.cuda.is_available():
        try:
            print(f"GPU 0: {torch.cuda.get_device_name(0)}")
        except Exception as exc:
            print(f"GPU name lookup failed: {exc}")
    mps = bool(hasattr(torch.backends, 'mps') and torch.backends.mps.is_available())
    print(f"Apple MPS available: {mps}")
    try:
        import torch_directml  # type: ignore
        print(f"DirectML available: yes ({torch_directml.device()})")
    except Exception:
        print("DirectML available: no")

    try:
        from ai_matcher import check_environment
        info = check_environment()
        print("\nApplication device detection:")
        print(json.dumps({
            'ready': info.get('ready'),
            'preferredDevice': info.get('preferredDevice'),
            'preferredDeviceLabel': info.get('preferredDeviceLabel'),
            'deviceDetails': info.get('deviceDetails'),
            'message': info.get('message'),
        }, indent=2))
    except Exception as exc:
        print(f"Application detection failed: {exc}")


if __name__ == '__main__':
    main()
