"""Shared helpers for batch.py and compare_spec2nii.py.

Both scripts walk `In/<corpus>/<series>/` the same way, invoke dcm2niix
with the same flags, and share a JSON sidecar validator. Living in one
place stops the two scripts from drifting when the directory convention
or the default flag set evolves.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# `-f %f` makes dcm2niix name its output after the input folder name, so a
# series at In/XA60/30_svs_se/ emits Out/30_svs_se.nii. This is the seam
# that pairs Ref/30_svs_se.nii with its Out twin and spec2nii's
# spec2nii/30_svs_se.nii.gz.
DEFAULT_DCM2NIIX_FLAGS = ["-b", "y", "-z", "n", "-f", "%f"]


def series_dirs(indir: Path) -> list[Path]:
    """Each per-series subfolder under `indir` is one dcm2niix run.

    Convention: `In/<corpus>/<series>/<DICOMs>`. Walk one level into
    each corpus dir, treat any populated leaf as a series.
    """
    series = []
    for corpus in sorted(p for p in indir.iterdir() if p.is_dir()):
        for sub in sorted(p for p in corpus.iterdir() if p.is_dir()):
            if any(sub.iterdir()):
                series.append(sub)
    return series


def series_out_relpath(series_dir: Path, indir: Path) -> Path:
    """Return the per-series output subdir relative to Out/<corpus>/.

    Layout: `In/XA60/30_svs_se/` → series_out_relpath returns `XA60`. The
    dcm2niix `-f %f` flag then writes `30_svs_se.nii` inside Out/<corpus>/XA60/
    so Ref/<corpus>/XA60/30_svs_se.json pairs up 1:1.
    """
    return series_dir.parent.relative_to(indir)


def validate_json(outdir: Path) -> int:
    """Re-parse every JSON sidecar under `outdir`. Returns count of bad ones."""
    bad = 0
    for jpath in sorted(outdir.rglob("*.json")):
        try:
            json.loads(jpath.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"INVALID JSON: {jpath}: {e}", file=sys.stderr)
            bad += 1
    return bad


def run_dcm2niix(exe: str, series_dir: Path, outdir: Path,
                 flags: list[str]) -> None:
    """Run dcm2niix on one series subfolder into `outdir`."""
    subprocess.run([exe, *flags, "-o", str(outdir), str(series_dir)], check=True)
