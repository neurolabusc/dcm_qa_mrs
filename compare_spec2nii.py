#!/usr/bin/env python3
"""Validation pass: dcm2niix vs spec2nii.

Different from `batch.py`: batch.py compares dcm2niix's current build against
a frozen Ref/ baseline (regression). This script compares dcm2niix's current
build against spec2nii's current build (validation).

Two corpora are supported:

  --corpus local   (default): walk In/<corpus>/<series>/ in this repo —
                              the dcm_qa_mrs-specific XA60 corpus that ships
                              under LFS. Outputs land in Out/ and spec2nii/
                              and are diffed in-process.

  --corpus spec2nii          : delegate to dcm2niix/tools/spec2nii_compare.py
                              against the wider 40-dataset spec2nii corpus at
                              $SPEC2NII_DATA. That tool carries the curated
                              inventory (per-dataset skip_reason, expected
                              BIDS suffix, etc.) so we don't duplicate it
                              here. Requires $DCM2NIIX_TOOLS (or --dcm2niix-tools)
                              pointing at dcm2niix/tools/.

  --corpus both              : run local then spec2nii. Aggregate exit code.

Local-corpus pipeline:
  1. Run dcm2niix on every In/<corpus>/<series>/ → Out/<series>.nii(+.json)
     (uses `-f %f` so the NIfTI stem matches the source folder name)
  2. Run spec2nii dicom on every In/<corpus>/<series>/ →
     spec2nii/<series>.nii.gz(+.json), with `-f <series>` so stems align.
     Skips this pass when spec2nii/ already has all expected files, unless
     `--refresh` is passed (e.g. when bumping spec2nii versions).
  3. For each series, diff the dcm2niix and spec2nii outputs:
       - NIfTI: parse both headers, compare FID payload byte-by-byte, sform
         under float32 tolerance, dim/pixdim under same tolerance.
       - JSON: key-level dict compare with a vendored alias map and a list
         of "informational" fields (provenance, dcm2niix-wide extras).
     Reuses the same comparison rationale as
     dcm2niix/tools/spec2nii_compare.py — that file is the master.

Spec2nii version handling (local corpus):
  The first regeneration writes `spec2nii/.spec2nii_version` with `spec2nii
  --version`. Subsequent runs compare; mismatch suggests the cache is stale
  and prints a hint. `--refresh` always regenerates.

Usage:
    python3 compare_spec2nii.py                          # local corpus, cached
    python3 compare_spec2nii.py --refresh                # local + regenerate
    python3 compare_spec2nii.py --series 30_svs_se       # restrict to one
    python3 compare_spec2nii.py --corpus spec2nii        # wider corpus
    python3 compare_spec2nii.py --corpus both            # both

Environment:
    SPEC2NII_DATA    path to spec2nii_test_data root (for --corpus spec2nii)
    DCM2NIIX_TOOLS   path to dcm2niix/tools/ (master inventory + comparator)
    DCM2NIIX_BIN     dcm2niix binary (overrides --dcm2niix)
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from dcm_qa_mrs_lib import DEFAULT_DCM2NIIX_FLAGS, series_dirs as _series_dirs

REPO = Path(__file__).resolve().parent

# Mirror of dcm2niix/tools/spec2nii_compare.py ignore lists. Keep in sync when
# the master list grows — the rationale (PII vs wide-provenance) is the same
# here. Update both files in lockstep.
SPEC2NII_PII_FIELDS = {
    "ConversionMethod", "ConversionTime", "OriginalFile",
    "PatientDoB", "PatientID", "PatientName", "PatientSex", "PatientWeight",
    "kSpace", "PulseSequenceFile",
}
DCM2NIIX_WIDE_FIELDS = {
    "ConversionSoftware", "ConversionSoftwareVersion", "BidsGuess",
    "AcquisitionDuration", "AcquisitionMatrixPE", "AcquisitionNumber",
    "AcquisitionTime", "BaseResolution", "BodyPart",
    "CoilCombinationMethod", "CoilString", "ConsistencyInfo",
    "DeviceSerialNumber", "InstitutionAddress", "InstitutionalDepartmentName",
    "InstitutionName", "Manufacturer", "ManufacturersModelName",
    "MagneticFieldStrength", "MatrixCoilMode", "Modality",
    "ProcedureStepDescription", "ProtocolName", "RawImage",
    "ReceiveCoilActiveElements", "ScanOptions",
    "SeriesDescription", "SeriesNumber", "ShimSetting", "SoftwareVersions",
    "StationName", "StudyDescription", "TxRefAmp",
    "DecayCorrectionFactor", "FrameDuration", "FrameTimesStart",
    "FrameReferenceTime",
    "ImageComments", "ImageOrientationPatientDICOM", "ImageType",
    "ImageTypeText", "ImagingFrequency", "MRSpectroscopyAcquisitionType",
    "NonlinearGradientCorrection", "NumberOfAverages",
    "NumberOfKSpaceTrajectories", "ParallelReductionFactorInPlane",
    "ParallelReductionFactorOutOfPlane", "PercentPhaseFOV", "PercentSampling",
    "PhaseResolution", "PulseSequenceDetails", "PulseSequenceName",
    "SequenceName", "SequenceVariant", "SpoilingState",
    "TablePosition",
}
BIDS_MRS_ALIASES = {
    "ExcitationFlipAngle": "FlipAngle",
    "RxCoil": "ReceiveCoilName",
    "TxCoil": "TransmitCoilName",
}
IGNORE_FIELDS_GLOBAL = SPEC2NII_PII_FIELDS | DCM2NIIX_WIDE_FIELDS


class NiftiHeader(NamedTuple):
    sizeof_hdr: int
    dim: tuple
    pixdim: tuple
    datatype: int
    bitpix: int
    vox_offset: int
    sform_code: int
    qform_code: int
    srow_x: tuple
    srow_y: tuple
    srow_z: tuple


def _open_maybe_gz(p: Path):
    return gzip.open(p, "rb") if p.suffix == ".gz" else open(p, "rb")


def read_nifti_header(p: Path) -> NiftiHeader:
    with _open_maybe_gz(p) as f:
        head = f.read(4)
    sizeof_hdr = struct.unpack("<i", head)[0]
    if sizeof_hdr == 348:
        return _read_nifti1(p)
    if sizeof_hdr == 540:
        return _read_nifti2(p)
    raise ValueError(f"unknown NIfTI sizeof_hdr={sizeof_hdr} for {p}")


def _read_nifti1(p: Path) -> NiftiHeader:
    with _open_maybe_gz(p) as f:
        h = f.read(348)
    if len(h) < 348:
        raise ValueError(f"truncated NIfTI-1 header in {p}: got {len(h)}/348 bytes")
    return NiftiHeader(
        348,
        struct.unpack("<8h", h[40:56]),
        struct.unpack("<8f", h[76:108]),
        struct.unpack("<h", h[70:72])[0],
        struct.unpack("<h", h[72:74])[0],
        int(struct.unpack("<f", h[108:112])[0]),
        struct.unpack("<h", h[254:256])[0],
        struct.unpack("<h", h[252:254])[0],
        struct.unpack("<4f", h[280:296]),
        struct.unpack("<4f", h[296:312]),
        struct.unpack("<4f", h[312:328]),
    )


def _read_nifti2(p: Path) -> NiftiHeader:
    with _open_maybe_gz(p) as f:
        h = f.read(540)
    if len(h) < 540:
        raise ValueError(f"truncated NIfTI-2 header in {p}: got {len(h)}/540 bytes")
    return NiftiHeader(
        540,
        struct.unpack("<8q", h[16:80]),
        tuple(float(v) for v in struct.unpack("<8d", h[104:168])),
        struct.unpack("<h", h[12:14])[0],
        struct.unpack("<h", h[14:16])[0],
        struct.unpack("<q", h[168:176])[0],
        struct.unpack("<i", h[348:352])[0],
        struct.unpack("<i", h[344:348])[0],
        tuple(float(v) for v in struct.unpack("<4d", h[400:432])),
        tuple(float(v) for v in struct.unpack("<4d", h[432:464])),
        tuple(float(v) for v in struct.unpack("<4d", h[464:496])),
    )


def _expected_payload_bytes(hdr: NiftiHeader) -> int:
    """Bytes expected for the FID payload from dim[] × bitpix."""
    ndim = hdr.dim[0] if hdr.dim[0] > 0 else 0
    nvox = 1
    for k in range(1, ndim + 1):
        nvox *= max(int(hdr.dim[k]), 1)
    return (nvox * int(hdr.bitpix)) // 8


def read_nifti_payload(p: Path) -> bytes:
    """Read the FID payload, validating vox_offset + payload size.

    Without these guards a hostile / truncated vox_offset could silently
    `seek` past EOF and return `b""`, which the byte-compare in
    `_compare_series` would treat as a match (`b"" == b""`). Compare against
    the dim/bitpix-derived expected size as a corroboration check.
    """
    hdr = read_nifti_header(p)
    if hdr.vox_offset < 0:
        raise ValueError(f"negative vox_offset {hdr.vox_offset} in {p}")
    expected = _expected_payload_bytes(hdr)
    with _open_maybe_gz(p) as f:
        f.seek(hdr.vox_offset)
        data = f.read()
    # Allow ≥ expected (NIfTI extensions / trailing padding) but never less.
    if expected > 0 and len(data) < expected:
        raise ValueError(
            f"short FID payload in {p}: got {len(data)} bytes, "
            f"dim/bitpix expects {expected}")
    return data[:expected] if expected > 0 else data


def _spec2nii_version() -> str | None:
    try:
        out = subprocess.run(["spec2nii", "--version"],
                             capture_output=True, text=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or out.stderr).strip()


def _floats_close(a, b, ulps=5, rel=1e-5):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if a == b:
        return True
    eps = math.ulp(abs(a)) * ulps
    return abs(a - b) <= max(eps, rel * abs(a))


def _compare_series(series_name: str, dcm_nii: Path, dcm_json: Path | None,
                    spec_nii: Path, spec_json: Path | None) -> dict:
    """Return a per-series result dict (matches the keys in main()'s summary)."""
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
    """Walk In/<corpus>/<series>/ and compare dcm2niix vs spec2nii on each."""
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
    """Delegate to dcm2niix/tools/spec2nii_compare.py against $SPEC2NII_DATA.

    That tool carries the curated 40-dataset inventory (vendor / suffix /
    skip_reason). We invoke it verbatim and forward its exit code so this
    repo doesn't duplicate the inventory — the master stays in dcm2niix.
    """
    tools = args.dcm2niix_tools or os.environ.get("DCM2NIIX_TOOLS")
    if not tools:
        print("Error: --dcm2niix-tools or $DCM2NIIX_TOOLS must point at "
              "dcm2niix/tools/ (where spec2nii_compare.py lives).",
              file=sys.stderr); return 1
    tool_path = Path(tools).resolve() / "spec2nii_compare.py"
    if not tool_path.exists():
        print(f"Error: {tool_path} not found", file=sys.stderr); return 1
    spec_data = os.environ.get("SPEC2NII_DATA")
    if not spec_data:
        print("Error: $SPEC2NII_DATA must point at a local clone of\n"
              "       https://git.fmrib.ox.ac.uk/wclarke/spec2nii_test_data\n"
              "  This script does NOT auto-download — clone once with:\n"
              "       git clone https://git.fmrib.ox.ac.uk/wclarke/spec2nii_test_data.git\n"
              "       export SPEC2NII_DATA=$PWD/spec2nii_test_data\n"
              "  (~5 GB; --corpus local works without this.)",
              file=sys.stderr); return 1
    if not Path(spec_data).is_dir():
        print(f"Error: $SPEC2NII_DATA={spec_data} does not exist or is not a "
              f"directory. Clone the spec2nii_test_data repo there first.",
              file=sys.stderr); return 1
    env = os.environ.copy()
    # Forward DCM2NIIX_BIN if the user specified a non-default binary so the
    # delegated tool picks it up too.
    if args.dcm2niix and args.dcm2niix != "dcm2niix":
        env["DCM2NIIX_BIN"] = args.dcm2niix
    print(f"\n=== spec2nii corpus (delegating to {tool_path}) ===", flush=True)
    return subprocess.run([sys.executable, str(tool_path), "--all"],
                          env=env).returncode


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
    ap.add_argument("--dcm2niix-tools",
                    help="path to dcm2niix/tools/ (where spec2nii_compare.py "
                         "lives). Falls back to $DCM2NIIX_TOOLS. Used by "
                         "--corpus spec2nii / both.")
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
