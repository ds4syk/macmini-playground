#!/usr/bin/env python3
"""Benchmark the self-trained VLA/Share models on this Mac.

Only the user's own checkpoints are loaded; no external LLM is involved.
Run with the local torch venv:

    ~/labs/VLA/Share/v2/.venv/bin/python scripts/bench_local_models.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

DEFAULT_VLA = Path.home() / "labs" / "VLA" / "Share" / "v2"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vla-dir", default=str(DEFAULT_VLA))
    parser.add_argument("--date", default="2026-11-09")
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()

    vla = Path(args.vla_dir)
    sys.path.insert(0, str(vla))
    import compute  # noqa: E402
    import gdn_ntp  # noqa: E402

    panel = dict(np.load(vla / "local_data" / "panel.npz", allow_pickle=False))
    metadata = json.loads((vla / "local_data" / "metadata.json").read_text())
    dates = np.array(metadata["dates"])
    signal_day = int(np.searchsorted(dates, args.date))
    stocks = np.flatnonzero(panel["eligible"][:, signal_day])
    features = (panel["features"] - panel["center"]) / panel["scale"]
    features = np.nan_to_num(features, nan=0.0, posinf=8.0, neginf=-8.0).clip(-8.0, 8.0)
    inputs = features[stocks, signal_day - 59:signal_day + 1, :].astype(np.float32)

    checkpoints = {
        "attn_gru": "latest_attn_gru.pt",
        "tcn": "latest_tcn.pt",
        "transformer": "latest_transformer.pt",
        "cnn": "latest_cnn.pt",
        "gdn_ntp": "latest_gdn_ntp.pt",
    }
    devices = ["cpu"] + (["mps"] if torch.backends.mps.is_available() else [])
    print(f"date={dates[signal_day]} stocks={len(stocks)} input={inputs.shape}")
    print(f"torch={torch.__version__} mps={torch.backends.mps.is_available()}")
    print("model        device   latency_ms   stocks_per_s")
    for name, filename in checkpoints.items():
        payload = torch.load(
            vla / "local_data" / "models" / filename, map_location="cpu", weights_only=False
        )
        cfg = payload["config"]
        if name == "gdn_ntp":
            model = gdn_ntp.GDNToken(
                len(payload["features"]),
                width=cfg["gdn_width"],
                blocks=cfg["gdn_blocks"],
                bins=cfg["token_bins"],
            )
        else:
            model = compute.build_model(name, len(payload["features"]), cfg)
        model.load_state_dict(payload["state_dict"])
        model.eval()
        x = torch.from_numpy(inputs)
        for device in devices:
            model.to(device)
            xd = x.to(device)
            with torch.no_grad():
                for _ in range(3):
                    model(xd)
                if device == "mps":
                    torch.mps.synchronize()
                started = time.perf_counter()
                for _ in range(args.runs):
                    model(xd)
                if device == "mps":
                    torch.mps.synchronize()
                latency = (time.perf_counter() - started) / args.runs
            print(
                f"{name:<12} {device:<6} {latency * 1000:10.2f}   {len(stocks) / latency:10.0f}"
            )


if __name__ == "__main__":
    main()
