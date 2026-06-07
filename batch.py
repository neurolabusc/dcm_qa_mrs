#!/usr/bin/env python3
"""Regression test: dcm2niix `.json` sidecars vs `Ref/<corpus>/...` baselines.

Two corpora:

  --corpus local     (default): walks `In/<corpus>/<series>/` in this repo
                                (currently XA60). Outputs land in
                                `Out/local/<corpus>/<series>.json` and are
                                diffed against `Ref/local/<corpus>/<series>.json`.

  --corpus spec2nii            : walks the curated 40-dataset inventory in
                                the sibling `spec2nii_compare.py`. Each
                                dataset's DICOMs live under
                                `$SPEC2NII_DATA`; outputs land in
                                `Out/spec2nii/<dataset_id>.json` and are
                                diffed against `Ref/spec2nii/<dataset_id>.json`.
                                Skip-reason'd datasets are skipped unless
                                `--run-skipped` is passed.

  --corpus both                : run both passes; aggregate exit code.

Only sidecar (`.json`) parity is checked here — the image-data (`.nii`) parity
is delegated to `compare_spec2nii.py --corpus={local,spec2nii,both}` which
uses spec2nii as the reference. Ref/ deliberately holds no `.nii` files;
keeping it sidecar-only avoids the LFS bill and aligns with "spec2nii is the
image-data ground truth."

When a Ref baseline doesn't exist (e.g. a new dataset just added, or one
that crashes dcm2niix), the dataset is reported as `NO-REF` — informational,
not a failure. Refresh the baseline by copying the relevant
`Out/<corpus>/.../<id>.json` into `Ref/<corpus>/.../<id>.json` and committing.

Usage:
    python3 batch.py                            # --corpus local
    python3 batch.py --corpus spec2nii          # needs env vars below
    python3 batch.py --corpus both
    python3 batch.py --no-clean                 # keep prior Out/ contents
    python3 batch.py --ignore <regex> ...       # additional JSON keys to ignore

Environment (for --corpus spec2nii / both):
    SPEC2NII_DATA    path to spec2nii_test_data root (clone of
                     git.fmrib.ox.ac.uk/wclarke/spec2nii_test_data; ~5 GB)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from dcm_qa_mrs_lib import (
    DEFAULT_DCM2NIIX_FLAGS,
    series_dirs as _series_dirs,
    series_out_relpath,
    run_dcm2niix as _run_dcm2niix,
    validate_json as _validate_json,
)

REPO = Path(__file__).resolve().parent
DEFAULT_IGNORE = ["ConversionSoftwareVersion", "BidsGuess"]


def _json_equivalent(ref: Path, out: Path, ignore: list[str]) -> bool:
    """JSON parity ignoring keys whose names match any `ignore` regex."""
    try:
        rj = json.loads(ref.read_text())
        oj = json.loads(out.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(rj, dict) or not isinstance(oj, dict):
        return rj == oj
    patterns = [re.compile(p) for p in ignore]
    def keep(k: str) -> bool:
        return not any(p.search(k) for p in patterns)
    rj = {k: v for k, v in rj.items() if keep(k)}
    oj = {k: v for k, v in oj.items() if keep(k)}
    return rj == oj


def _diff_corpus(refdir: Path, outdir: Path, ignore: list[str]) -> tuple[int, list[str]]:
    """Compare every Out/<corpus>/.json with its Ref/<corpus>/.json twin.

    .nii / non-json files in Out are not compared (Ref is sidecar-only).
    Ref entries with no Out twin are reported as MISSING; Out entries with
    no Ref twin are reported as NO-REF (informational — used to populate
    a new Ref baseline).
    """
    out_jsons = sorted(
        p.relative_to(outdir) for p in outdir.rglob("*.json")
        if p.name != ".DS_Store")
    ref_jsons = sorted(
        p.relative_to(refdir) for p in refdir.rglob("*.json")
        if p.name != ".DS_Store")
    out_set = set(out_jsons); ref_set = set(ref_jsons)

    differing = [str(r) for r in sorted(ref_set & out_set)
                 if not _json_equivalent(refdir / r, outdir / r, ignore)]
    missing = [str(r) for r in sorted(ref_set - out_set)]
    no_ref = [str(r) for r in sorted(out_set - ref_set)]

    summary = []
    if differing:
        summary.append(f"DIFFERING ({len(differing)}):")
        summary.extend(f"  {d}" for d in differing)
    if missing:
        summary.append(f"MISSING ({len(missing)}, Ref/ has it, Out/ doesn't):")
        summary.extend(f"  {m}" for m in missing)
    if no_ref:
        summary.append(f"NO-REF ({len(no_ref)}, populate by copying into Ref/):")
        summary.extend(f"  {n}" for n in no_ref)
    status = 1 if (missing or differing) else 0
    return status, summary


def _run_local(args, ignore: list[str]) -> int:
    indir = Path(args.indir).resolve()
    outdir = Path(args.outdir).resolve() / "local"
    refdir = Path(args.refdir).resolve() / "local"
    if not refdir.is_dir():
        print(f"Error: refdir {refdir} not found", file=sys.stderr); return 1

    if not args.no_clean and outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    series = _series_dirs(indir)
    if not series:
        print(f"Error: no series subfolders found under {indir}", file=sys.stderr)
        return 1

    print(f"\n=== Local corpus ({len(series)} series) ===")
    for s in series:
        sub = outdir / series_out_relpath(s, indir)
        sub.mkdir(parents=True, exist_ok=True)
        _run_dcm2niix(args.dcm2niix, s, sub, args.flags)

    bad = _validate_json(outdir)
    if bad:
        print(f"ERROR: {bad} malformed JSON sidecar(s)", file=sys.stderr); return 1

    status, lines = _diff_corpus(refdir, outdir, ignore)
    if not lines:
        print(f"OK — Out/local matches Ref/local under ignore list {ignore}")
    else:
        for line in lines:
            print(line)
    return status


def _run_spec2nii(args, ignore: list[str]) -> int:
    if not os.environ.get("SPEC2NII_DATA"):
        print("Error: $SPEC2NII_DATA must point at a local clone of\n"
              "  https://git.fmrib.ox.ac.uk/wclarke/spec2nii_test_data\n"
              "  (~5 GB; clone once, then export the path).", file=sys.stderr)
        return 1

    # Inventory now lives in the sibling spec2nii_compare.py (moved from
    # dcm2niix/tools/). Import via Python's normal sibling-module mechanism;
    # REPO is on sys.path because batch.py is executed as a script.
    sys.path.insert(0, str(REPO))
    try:
        from spec2nii_compare import DATASETS  # type: ignore
    finally:
        sys.path.pop(0)
    datasets = list(DATASETS)

    outdir = Path(args.outdir).resolve() / "spec2nii"
    refdir = Path(args.refdir).resolve() / "spec2nii"
    if not args.no_clean and outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    active = [d for d in datasets if (args.run_skipped or not d.skip_reason)]
    print(f"\n=== spec2nii corpus ({len(active)}/{len(datasets)} active, "
          f"{len(datasets)-len(active)} skipped) ===")

    errors: list[tuple[str, str]] = []
    for d in active:
        if not d.source.exists():
            errors.append((d.id, f"source missing: {d.source}"))
            continue
        try:
            # `-f <id>` so the output stem is the dataset ID (single-DICOM
            # files would otherwise pick up `%f` = parent-dir-name which
            # collides across vendors).
            flags = [f if f != "%f" else d.id for f in args.flags]
            subprocess.run([args.dcm2niix, *flags,
                            "-o", str(outdir), str(d.source)], check=True)
        except subprocess.CalledProcessError as e:
            errors.append((d.id, f"dcm2niix exited {e.returncode}"))

    bad = _validate_json(outdir)
    if bad:
        print(f"ERROR: {bad} malformed JSON sidecar(s)", file=sys.stderr); return 1

    status, lines = _diff_corpus(refdir, outdir, ignore)
    for eid, msg in errors:
        print(f"  CONVERSION-ERROR {eid}: {msg}")
    if not lines and not errors:
        print(f"OK — Out/spec2nii matches Ref/spec2nii under ignore list {ignore}")
    else:
        for line in lines:
            print(line)
    return status or (1 if errors else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", choices=["local", "spec2nii", "both"],
                    default="local",
                    help="which corpus to test (default: local)")
    ap.add_argument("--indir", default=str(REPO / "In"))
    ap.add_argument("--outdir", default=str(REPO / "Out"))
    ap.add_argument("--refdir", default=str(REPO / "Ref"))
    ap.add_argument("--dcm2niix", default="dcm2niix")
    ap.add_argument("--flags", nargs="*", default=DEFAULT_DCM2NIIX_FLAGS,
                    help="dcm2niix flags (default: %(default)s). For the "
                         "spec2nii corpus, `%%f` is replaced per-dataset with "
                         "the curated dataset ID.")
    ap.add_argument("--no-clean", action="store_true",
                    help="keep prior Out/<corpus>/ contents (default cleans)")
    ap.add_argument("--ignore", nargs="*", default=DEFAULT_IGNORE,
                    help="JSON keys to ignore in the parity diff (regex)")
    ap.add_argument("--run-skipped", action="store_true",
                    help="(spec2nii corpus) include skip_reason'd datasets")
    args = ap.parse_args()

    if not shutil.which(args.dcm2niix):
        print(f"Error: {args.dcm2niix} not on PATH", file=sys.stderr); return 1

    local_rc = 0
    spec_rc = 0
    if args.corpus in ("local", "both"):
        local_rc = _run_local(args, args.ignore)
    if args.corpus in ("spec2nii", "both"):
        spec_rc = _run_spec2nii(args, args.ignore)
    return local_rc or spec_rc


if __name__ == "__main__":
    sys.exit(main())
