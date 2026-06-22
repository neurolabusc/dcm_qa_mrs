#!/usr/bin/env python3
"""NIfTI-MRS header-extension (ecode 44) parity: dcm2niix vs spec2nii.

dcm2niix embeds the NIfTI-MRS `hdr_ext` JSON inside the .nii(.gz) in addition to
the BIDS sidecar (so a tool can read SVS/MRSI metadata straight from the NIfTI —
e.g. sandboxed drag-and-drop). This walks the DICOM-MRS corpus, extracts the
ecode-44 JSON from both tools' output, and reports per-dataset key parity.

Reuses the conversion machinery + dataset inventory in spec2nii_compare.py.

    python3 compare_ext.py                 # whole corpus, summary
    python3 compare_ext.py <dataset_id>    # single dataset, verbose
    python3 compare_ext.py -v              # verbose (per-dataset deltas)

Env: SPEC2NII_DATA (corpus root), DCM2NIIX_BIN (defaults to PATH `dcm2niix`).
Needs `nifti_mrs` + `spec2nii` importable (e.g. the pyenv that ships them).
"""
import json
import sys
import tempfile
from pathlib import Path

import nibabel as nib
from spec2nii_compare import (DATASETS, run_dcm2niix, run_spec2nii,
                              _floats_close, SPEC2NII_PII_FIELDS)

# Fields excluded from parity, with the reason each is expected to differ:
IGNORE = set(SPEC2NII_PII_FIELDS) | {
    # spec2nii reshapes MEGA-PRESS onto an edit axis; dcm2niix leaves that to
    # tools/mrs_post.py, so the dim_6 edit tag is out of scope here.
    "dim_6", "dim_6_header",
    # Vendor-string conventions: dcm2niix emits canonical short names
    # ("Siemens" vs "Siemens Healthineers"); value is correct, spelling differs.
    "Manufacturer", "SoftwareVersions",
    # spec2nii emits these keys with EMPTY "" values when the source tag is blank
    # (UIH leaves InstitutionName/Address empty); dcm2niix omits empty optional
    # fields. Not a real metadata gap — dcm2niix's behavior is the cleaner one.
    "InstitutionName", "InstitutionAddress",
}


def _ext44(nii_path: Path):
    for e in nib.load(str(nii_path)).header.extensions:
        if e.get_code() == 44:
            return json.loads(e.get_content())
    return None


def _close(s, d):
    if isinstance(s, list) and isinstance(d, list) and len(s) == len(d):
        return all(_close(x, y) for x, y in zip(s, d))
    if isinstance(s, (int, float)) and isinstance(d, (int, float)) and not isinstance(s, bool):
        return _floats_close(s, d)
    return s == d


def compare_one(ds, verbose=False):
    """Return (status, detail). status: 'ok' | 'diff' | 'skip' | 'error'."""
    out = Path(tempfile.mkdtemp())
    try:
        dnii, _ = run_dcm2niix(ds, out)
        snii, _ = run_spec2nii(ds, out)
    except Exception as e:
        return "error", str(e)[:90]
    de, se = _ext44(dnii), _ext44(snii)
    if se is None:
        return "skip", "spec2nii emitted no ecode-44"
    if de is None:
        return "diff", "dcm2niix emitted NO ecode-44 extension"
    miss = sorted(k for k in se if k not in IGNORE and k not in de)
    mism = sorted(k for k in se if k in de and k not in IGNORE and not _close(se[k], de[k]))
    if not miss and not mism:
        return "ok", f"{len(de)} keys"
    parts = []
    if miss:
        parts.append("missing=" + ",".join(miss))
    if mism:
        parts.append("mismatch=" + ",".join(
            f"{k}({se[k]!r}!={de[k]!r})" if verbose else k for k in mism))
    return "diff", "; ".join(parts)


def main(argv):
    verbose = "-v" in argv
    ids = [a for a in argv if not a.startswith("-")]
    datasets = [d for d in DATASETS if not getattr(d, "skip_reason", None)]
    if ids:
        datasets = [d for d in datasets if d.id in ids]
        if not datasets:
            print(f"No dataset matched {ids}. Use spec2nii_compare.py --list.")
            return 2
        verbose = True
    tally = {"ok": 0, "diff": 0, "skip": 0, "error": 0}
    for ds in datasets:
        status, detail = compare_one(ds, verbose)
        tally[status] += 1
        if status != "ok" or verbose:
            print(f"  [{status:5s}] {ds.id:44s} {detail}")
        elif status == "ok":
            print(f"  [ok   ] {ds.id:44s} {detail}")
    print(f"\n=== ecode-44 parity: {tally['ok']}/{sum(tally.values())} "
          f"(diff={tally['diff']} skip={tally['skip']} error={tally['error']}) ===")
    print("ignored (expected to differ): PII/-ba, MEGA dim_6, Manufacturer/"
          "SoftwareVersions vendor strings, empty-valued Institution* keys.")
    return 1 if tally["diff"] or tally["error"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
