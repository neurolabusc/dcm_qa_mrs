#!/usr/bin/env python3
"""Validation pass: dcm2niix vs spec2nii (live).

Different from `batch.py`: batch.py compares dcm2niix's current build against
a frozen `Ref/<corpus>/` sidecar baseline (regression). This script compares
dcm2niix's current build against spec2nii's current build (validation).

Two corpora are supported:

  --corpus local   (default): walk In/<corpus>/<series>/ in this repo
                              (the LFS-tracked XA60 SVS files). Outputs
                              land in Out/ and spec2nii/ and are diffed
                              in-process.

  --corpus spec2nii          : delegate to `spec2nii_compare.py` (sibling
                              file in this same folder) against the
                              wider 40-dataset spec2nii corpus at
                              $SPEC2NII_DATA. That script carries the
                              curated inventory (per-dataset skip_reason,
                              expected BIDS suffix, etc.) so we don't
                              duplicate it here.

  --corpus both              : run local then spec2nii. Aggregate exit code.

What's compared (both corpora):
 - FID payload byte-by-byte (after endian normalisation; DT_COMPLEX64)
 - sform under float32 tolerance (1e-4 absolute, 1e-5 relative)
 - dim/pixdim under the same tolerance
 - JSON sidecar key-level diff with the BIDS-MRS alias map (`RxCoil` ↔
   `ReceiveCoilName`, etc.) — see `spec2nii_compare.py` for the master
   ignore-list / alias-map rationale.

Spec2nii version handling (local corpus):
  First regeneration writes `spec2nii/.spec2nii_version` with
  `spec2nii --version`. Subsequent runs warn when the cached version
  drifts from the current `spec2nii --version`. `--refresh` always
  regenerates.

Usage:
    python3 compare_spec2nii.py                          # local corpus
    python3 compare_spec2nii.py --refresh                # local, regen
    python3 compare_spec2nii.py --series 30_svs_se       # single series
    python3 compare_spec2nii.py --corpus spec2nii        # wider corpus
    python3 compare_spec2nii.py --corpus both            # both

Environment:
    SPEC2NII_DATA    path to spec2nii_test_data root (for --corpus spec2nii)
    DCM2NIIX_BIN     dcm2niix binary (overrides --dcm2niix)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dcm_qa_mrs_lib import DEFAULT_DCM2NIIX_FLAGS, series_dirs as _series_dirs

# Reuse the canonical NIfTI parser + sidecar-compare logic from the sibling
# spec2nii_compare.py (moved here from dcm2niix/tools/). Single source of
# truth for the alias map / ignore lists.
from spec2nii_compare import (
    BIDS_MRS_ALIASES,
    IGNORE_FIELDS_GLOBAL,
    _floats_close,
    read_nifti_header,
    read_nifti_payload,
)

REPO = Path(__file__).resolve().parent


def _spec2nii_version() -> str | None:
    try:
        out = subprocess.run(["spec2nii", "--version"],
                             capture_output=True, text=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or out.stderr).strip()


def _compare_series(series_name: str, dcm_nii: Path, dcm_json: Path | None,
                    spec_nii: Path, spec_json: Path | None) -> dict:
    """Per-series compare result: FID + sform + dim/pixdim + sidecar."""
    out: dict = {"series": series_name, "ok": True, "notes": []}
    dcm_hdr = read_nifti_header(dcm_nii)
    spec_hdr = read_nifti_header(spec_nii)
    dcm_fid = read_nifti_payload(dcm_nii)
    spec_fid = read_nifti_payload(spec_nii)
    if dcm_fid != spec_fid:
        out["ok"] = False
        out["notes"].append(f"FID byte mismatch: dcm={len(dcm_fid)} spec={len(spec_fid)}")
    if tuple(dcm_hdr.dim) != tuple(spec_hdr.dim):
        out["ok"] = False
        out["notes"].append(f"dim: dcm={dcm_hdr.dim} spec={spec_hdr.dim}")
    for axis_name, dr, sr in (("x", dcm_hdr.srow_x, spec_hdr.srow_x),
                              ("y", dcm_hdr.srow_y, spec_hdr.srow_y),
                              ("z", dcm_hdr.srow_z, spec_hdr.srow_z)):
        for k, (d, s) in enumerate(zip(dr, sr)):
            if abs(d - s) > max(1e-4, 1e-5 * abs(s)):
                out["ok"] = False
                out["notes"].append(
                    f"sform[{axis_name}][{k}] dcm={d} spec={s} (Δ={d-s:.3e})")
    for k, (d, s) in enumerate(zip(dcm_hdr.pixdim, spec_hdr.pixdim)):
        if abs(d - s) > max(1e-4, 1e-5 * abs(s)):
            out["ok"] = False
            out["notes"].append(f"pixdim[{k}] dcm={d} spec={s} (Δ={d-s:.3e})")
    if dcm_json and spec_json:
        _compare_sidecar(out, json.loads(dcm_json.read_text()),
                         json.loads(spec_json.read_text()))
    elif not dcm_json:
        out["notes"].append("dcm2niix produced no JSON sidecar")
    elif not spec_json:
        out["notes"].append("spec2nii produced no JSON sidecar")
    return out


def _compare_sidecar(result: dict, dcm: dict, spec: dict) -> None:
    raw_dcm = set(dcm); raw_spec = set(spec)
    dcm_keys = raw_dcm - IGNORE_FIELDS_GLOBAL
    spec_keys = raw_spec - IGNORE_FIELDS_GLOBAL
    for spec_name, dcm_name in BIDS_MRS_ALIASES.items():
        if spec_name in raw_spec and dcm_name in raw_dcm:
            sv, dv = spec[spec_name], dcm[dcm_name]
            if not (sv == dv or
                    (isinstance(sv, (int, float)) and isinstance(dv, (int, float))
                     and _floats_close(sv, dv)) or
                    str(sv) == str(dv)):
                result["ok"] = False
                result["notes"].append(
                    f"sidecar alias {spec_name}/{dcm_name}: dcm={dv!r} spec={sv!r}")
            spec_keys.discard(spec_name); dcm_keys.discard(dcm_name)
    spec_only = sorted(spec_keys - dcm_keys)
    if spec_only:
        result["ok"] = False
        result["notes"].append(f"sidecar spec-only ({len(spec_only)}): {spec_only}")
    for k in sorted(spec_keys & dcm_keys):
        sv, dv = spec[k], dcm[k]
        if sv == dv:
            continue
        if isinstance(sv, list) and isinstance(dv, list) and len(sv) == len(dv) \
                and all(_floats_close(a, b) for a, b in zip(sv, dv)):
            continue
        if _floats_close(sv, dv):
            continue
        result["ok"] = False
        result["notes"].append(f"sidecar differs {k}: dcm={dv!r} spec={sv!r}")


def _run_local_corpus(args) -> int:
    indir = Path(args.indir).resolve()
    outdir = Path(args.outdir).resolve()
    specdir = Path(args.specdir).resolve()

    if not shutil.which("spec2nii"):
        print("Error: spec2nii not on PATH; install: pip install spec2nii",
              file=sys.stderr); return 1

    series = _series_dirs(indir)
    if args.series:
        series = [s for s in series if s.name == args.series]
    if not series:
        print(f"Error: no series found under {indir}", file=sys.stderr); return 1

    outdir.mkdir(exist_ok=True); specdir.mkdir(exist_ok=True)

    # 1) dcm2niix pass
    for s in series:
        subprocess.run([args.dcm2niix, *DEFAULT_DCM2NIIX_FLAGS,
                        "-o", str(outdir), str(s)], check=True)

    # 2) spec2nii pass (regen if empty, missing, or --refresh)
    expected = {s.name + ".nii.gz" for s in series}
    have = {p.name for p in specdir.glob("*.nii.gz")}
    need_regen = args.refresh or not expected.issubset(have)
    version = _spec2nii_version()
    if need_regen:
        print(f"Regenerating spec2nii/ with version: {version}")
        t0 = time.perf_counter()
        for s in series:
            subprocess.run(["spec2nii", "dicom",
                            "-f", s.name,
                            "-o", str(specdir),
                            "-j",
                            str(s)], check=True)
        elapsed = time.perf_counter() - t0
        print(f"Regenerated {len(series)} files in {elapsed:.1f}s")
        if version:
            (specdir / ".spec2nii_version").write_text(version + "\n")
    else:
        cached = (specdir / ".spec2nii_version")
        if cached.exists() and version and cached.read_text().strip() != version:
            print(f"WARNING: spec2nii/ cached for "
                  f"'{cached.read_text().strip()}' but `spec2nii --version` "
                  f"reports '{version}'. Re-run with --refresh.")

    # 3) per-series compare
    results = []
    for s in series:
        dcm_nii = outdir / f"{s.name}.nii"
        dcm_json = outdir / f"{s.name}.json"
        spec_nii = specdir / f"{s.name}.nii.gz"
        spec_json = specdir / f"{s.name}.json"
        if not dcm_nii.exists():
            results.append({"series": s.name, "ok": False,
                            "notes": [f"missing dcm2niix output: {dcm_nii}"]})
            continue
        if not spec_nii.exists():
            results.append({"series": s.name, "ok": False,
                            "notes": [f"missing spec2nii output: {spec_nii}"]})
            continue
        results.append(_compare_series(
            s.name, dcm_nii,
            dcm_json if dcm_json.exists() else None,
            spec_nii,
            spec_json if spec_json.exists() else None))

    n_pass = sum(1 for r in results if r["ok"])
    n_fail = len(results) - n_pass
    print(f"\n=== Local corpus ({len(results)} series) ===")
    for r in results:
        tag = "PASS" if r["ok"] else "FAIL"
        print(f"[{tag}] {r['series']}")
        for n in r["notes"]:
            print(f"      • {n}")
    print(f"summary: {n_pass} pass, {n_fail} fail (total {len(results)})")
    return 0 if n_fail == 0 else 1


def _run_spec2nii_corpus(args) -> int:
    """Delegate to the sibling `spec2nii_compare.py` against $SPEC2NII_DATA.

    The wider 40-dataset inventory + curated skip_reasons live in
    spec2nii_compare.py — we invoke it verbatim and forward its exit code
    so no duplication.
    """
    tool_path = REPO / "spec2nii_compare.py"
    if not tool_path.exists():
        print(f"Error: {tool_path} not found (expected as sibling file)",
              file=sys.stderr); return 1
    print(f"\n=== spec2nii corpus (delegating to {tool_path.name}) ===",
          flush=True)
    return subprocess.run([sys.executable, str(tool_path), "--all"]).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", choices=["local", "spec2nii", "both"],
                    default="local",
                    help="which corpus to validate against (default: local)")
    ap.add_argument("--indir", default=str(REPO / "In"))
    ap.add_argument("--outdir", default=str(REPO / "Out"))
    ap.add_argument("--specdir", default=str(REPO / "spec2nii"))
    ap.add_argument("--dcm2niix", default="dcm2niix")
    ap.add_argument("--refresh", action="store_true",
                    help="(local corpus) regenerate spec2nii/ even if "
                         "populated (use when bumping spec2nii versions)")
    ap.add_argument("--series", help="(local corpus) only compare this series")
    args = ap.parse_args()

    if not shutil.which(args.dcm2niix):
        print(f"Error: {args.dcm2niix} not on PATH", file=sys.stderr); return 1

    local_rc = 0
    spec_rc = 0
    if args.corpus in ("local", "both"):
        local_rc = _run_local_corpus(args)
    if args.corpus in ("spec2nii", "both"):
        spec_rc = _run_spec2nii_corpus(args)
    return local_rc or spec_rc


if __name__ == "__main__":
    sys.exit(main())
