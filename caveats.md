# dcm_qa_mrs caveats, gotchas, and collective wisdom

This document captures the institutional knowledge accumulated while
bringing dcm2niix's MR Spectroscopy support to byte parity with
spec2nii on the curated 40-dataset corpus. It is the working
companion to the regression scripts in this repository.

Contents:

1. [Current scoreboard](#current-scoreboard)
2. [Comparator usage](#comparator-usage)
3. [SKIP-state inventory](#skip-state-inventory)
4. [`tools/mrs_post.py` post-processor](#toolsmrs_postpy-post-processor)
5. [Per-vendor parity gotchas](#per-vendor-parity-gotchas)
6. [DICOM data layout gotchas](#dicom-data-layout-gotchas)
7. [Sidecar precision gotchas](#sidecar-precision-gotchas)
8. [Comparator quirks](#comparator-quirks)
9. [Decisions locked along the way](#decisions-locked-along-the-way)
10. [Future / out-of-scope work](#future--out-of-scope-work)

---

## Current scoreboard

As of the 2026-06-08 Phase 6 close-out commit on `dcm2niix@development`:

| Mode | PASS | FAIL | SKIP |
|---|---|---|---|
| **Bare** (`--all`) | **23 / 40** | 7 | 10 |
| **With `--with-mrs-post`** | **30 / 40** | **0** | 10 |

Every non-SKIP dataset PASSes byte-parity with spec2nii on FID + sform
+ dim + pixdim + suffix + JSON sidecar. The 7 bare-mode FAILs are all
either Siemens sLASER multi-DICOM reference-scan splits (6) or the
Philips press_mega MEGA-PRESS reshape (1) — those are intentional
post-processor cases handled by `tools/mrs_post.py`, not C-side gaps.

The 10 SKIPs are bounded by spec2nii's reference availability or by
data-format reality; see the [SKIP-state inventory](#skip-state-inventory).

### Per-vendor

| Vendor | Total | Bare PASS | mrs-post PASS | Notes |
|---|---|---|---|---|
| Siemens | 19 | 13 | 19 | mrs-post fills in 6 sLASER multi-DICOM |
| Philips | 18 | 7 | 8 | mrs-post fills in press_mega; 10 SKIPs |
| UIH | 3 | 3 | 3 | clean |

---

## Comparator usage

The comparator is `spec2nii_compare.py` at the root of this repo. It
expects:

- `dcm2niix` on `PATH` (or `$DCM2NIIX_BIN` set explicitly).
- `spec2nii` on `PATH` (`pip install spec2nii`).
- `$SPEC2NII_DATA` pointing at a clone of
  `https://git.fmrib.ox.ac.uk/wclarke/spec2nii_test_data` (~5 GB).
- `tools/mrs_post.py` from a sibling `dcm2niix` checkout for
  `--with-mrs-post` mode. Discovery order:
  - `../dcm2niix/tools/mrs_post.py`
  - `../../dcm2niix/tools/mrs_post.py`
  - `$MRS_POST_PY` env var override.

Common invocations:

```bash
# Walk every dataset; print per-dataset PASS/FAIL/SKIP + summary.
python3 spec2nii_compare.py --all

# Same, plus mrs-post — closes the 7 bare FAILs.
python3 spec2nii_compare.py --all --with-mrs-post

# Single dataset, verbose:
python3 spec2nii_compare.py siemens_sm_classic --verbose

# Restrict to one vendor:
python3 spec2nii_compare.py --all --vendor philips

# List inventory:
python3 spec2nii_compare.py --list

# Override SKIPs (only useful for debugging the SKIP rationale):
python3 spec2nii_compare.py --all --run-skipped
```

### Parity dimensions reported per dataset

- **FID**: byte-exact complex64 payload compare on the NIfTI body.
- **sform**: 5-ULP / 1e-5-relative tolerance on each of the 12
  populated `srow_*` entries.
- **dim**: tuple-equality on `(ndim, dim[1..7])`.
- **pixdim**: same tolerance as sform.
- **suffix**: dcm2niix sidecar's `BidsGuess[1]` matches the inventory
  expectation (`_svs` / `_mrsi` / `_mrsref`).
- **JSON**: sidecar fields. Alias-mapped where spec2nii and BIDS-MRS
  use different names (e.g. `RxCoil` ↔ `ReceiveCoilName`). Nested
  lists (e.g. the 4×4 `VOI` matrix) compared with recursive
  `_floats_close` tolerance — extends scalar/flat-array behaviour to
  arbitrarily nested numeric arrays.

### `JSON—` vs `JSON✓` vs `JSON Δn`

- `JSON✓` — sidecar produced and parity-clean.
- `JSON Δn` — sidecar produced and has `n` parity-meaningful
  differences (after the alias / tolerance / ignore-list filters).
- `JSON—` — converter did not produce a sidecar at all (typically
  because the parser hard-rejected the input). Distinguishes
  "wrong output" from "no output". Audit round-3 M5.

---

## SKIP-state inventory

**Important correction (2026-06-08)**: the inventory's prior label
for the 9 `orientation_tests/` rows ("Raw Data Storage non-MRS") was
inaccurate. The actual story below:

### 9 × `philips/spar_dcm_orientation_tests/4002–4802/`

These are Philips orientation-regression phantoms in 9 rotation
configurations (`iso_50-80-30_rot-0-0-0` through
`iso_50-80-30_rot-44-10-10`).

Each directory contains **two DICOM SOP classes mixed together**:

- 2× `MRs.*.dcm` — SOP `1.2.840.10008.5.1.4.1.1.4.2` (Enhanced MR
  Spectroscopy Storage). **These are valid MRS data.**
- 2× `RAW.*.dcm` — SOP `1.2.840.10008.5.1.4.1.1.66` (Raw Data
  Storage). Proprietary supplementary data, not MRS.

**dcm2niix handles the `MRs.*.dcm` files correctly.** When pointed at
one of them it emits a clean `_svs` NIfTI + BIDS-MRS sidecar with
`MRSpectroscopyAcquisitionType: SINGLE_VOXEL`, 1024 spectral points,
populated `SpectrometerFrequency`, etc. Verified on 4002 and 4302;
the rotation isolations are structurally identical so the rest
should follow.

**spec2nii fails locally** with
`AttributeError: 'Dataset' object has no attribute 'PixelMeasuresSequence'`
when invoked on these directories — a spec2nii-side issue (or an
incompatibility with the local pydicom version), not a dcm2niix
gap.

The comparator SKIPs reflect a **reference-side limitation**, not a
converter gap. To close them, we'd need a non-spec2nii validation
path — either compare to SPAR/SDAT-derived NIfTI, or a BIDS-MRS
well-formedness smoke test.

### 1 × `philips/hyper/converted_dcm.dcm`

HBCD HYPER edit-sequence acquisition. The companion `.SDAT`/`.SPAR`
files carry the real spectroscopy data; the `converted_dcm.dcm` is
a partial "classic DICOM export" that's not the canonical conversion
target for this kind of data.

- **spec2nii errors on the reference path** in this environment.
- **dcm2niix rejects the DICOM** with "No valid DICOM images were
  found" — the parser doesn't recognize the SOP/structure as MRS.

For HBCD HYPER users the canonical route is spec2nii's SDAT/SPAR
parser. Closing this SKIP would require either dcm2niix learning a
new Philips MRS DICOM variant (unlikely demand) or routing through
the SDAT path (out of scope; that's spec2nii's job).

---

## `tools/mrs_post.py` post-processor

Lives in the dcm2niix repo at `tools/mrs_post.py`. Pure pydicom +
numpy; no external compile or build step.

### When you need it

dcm2niix's C-side MRS writer intentionally **bundles raw multi-DICOM
/ multi-frame** series as a single NIfTI (`dim[5] = total frames`,
no reorder, no drop). Vendor-sequence-state-specific reshapes /
splits live above the converter because they require interpreting
private SQs or Phoenix Protocol — that's interpretation, not DICOM
semantics. `mrs_post.py` is the post-processor that does the
spec2nii-style reshape after the fact.

The split is documented in dcm2niix's `CLAUDE.md` under
"MRS split policy".

### Three handled cases (auto-detected)

1. **CMRR sLASER DKD multi-DICOM** (Siemens VB/VE). Port of
   spec2nii `dicomfunctions.py:identify_integrated_references`.
   Reads Phoenix Protocol (`tSequenceFileName`, `lAutoRefScanMode`,
   `lAutoRefScanNo`, `lAverages`) from the first source DICOM,
   classifies each bundled frame by `InstanceNumber`, writes
   per-group NIfTIs with spec2nii-style filename suffixes:
   `_svs` (main acquisition), `_svs_rf_off`,
   `_svs_rf_grads_ovs_off`, `_svs_vapor_ovs_rfoff`.
   Mode=8 (`svs_slaser_dkd`, `svs_slaserVOI_dkd2` — 4-and-4 ref
   layout) and mode=2 (`svs_slaserVOI_dkd2` — 1-and-1 ref layout)
   both supported. The bounds matter: `inst_num < num_ref` (strict
   less-than) for mode=2's start refs, `inst_num >= (total_dyn -
   num_ref)` (inclusive) for end refs. Off-by-one breaks FID
   parity.

2. **Philips MEGA-PRESS reshape** (single Enhanced DICOM,
   multi-frame). Port of spec2nii `philips_dcm.py:
   _process_philips_svs_new` MEGA branch. Reads `(2005,1597)=='Y'`
   (is_edited gate) + per-frame `(2005,1304)` (ref-flag) +
   `(2005,1598)` (edit ON/OFF) from the source DICOM. Writes paired
   `_svs (1024,144,2)` (main, with edit ON/OFF on the 6th axis) +
   `_mrsref (1024,9)` (the 9 reference frames cropped out).

3. **`_mrsref` companion sanity** (Philips classic 2× case). Pure
   pass-through validation that the C-side companion writer
   produced both `_svs.nii(.gz)` and `_mrsref.nii(.gz)` from a
   single 2× payload.

### Sidecar / BidsGuess convention

After the split, the **main group keeps `BidsGuess = ["mrs", "_svs"]`**
so the comparator's existing BidsGuess-match logic picks it
automatically as the parity target. **Reference groups get
`BidsGuess = ["mrs", "_mrsref"]`** (they ARE water-suppression-off
references). Filenames carry the spec2nii-style group suffix for
human traceability (`_svs_rf_off` etc.), but BIDS routing relies on
the sidecar value, not the filename suffix.

### Invocation

```bash
# Run manually after a dcm2niix conversion:
python3 tools/mrs_post.py <bundled.nii(.gz)> --dicoms <source-dir-or-file>

# Or let the comparator do it:
python3 spec2nii_compare.py --all --with-mrs-post
```

The post-processor auto-detects which case applies (from the sidecar
Manufacturer + source DICOM private tags). If no case matches it's a
no-op and leaves the bundled output alone.

When the comparator runs in `--with-mrs-post` mode and the post-
processor produces split outputs, the bundled input `.nii`/`.json`
are removed so the BidsGuess-match logic picks the new main file.

### Limitations / not-yet-implemented

- **HYPER** edit sequences (Philips). The MEGA-PRESS handler covers
  the simplest edit-on/off case; HYPER's two-step interleave is more
  complex and not yet ported.
- **Multi-coil** (3×+ payload multipliers). Currently the C side
  treats any Philips Enhanced `>=3×` payload as plain dynamics. If
  the data is actually coil-axis-bundled, the post-processor would
  need a coil-axis split that doesn't exist yet.

---

## Per-vendor parity gotchas

### Siemens

**Phase convention by SOPClassUID.** spec2nii dispatches on
SOPClassUID, NOT on `SoftwareVersions`. The Enhanced MR Spectroscopy
Storage SOP (`1.2.840.10008.5.1.4.1.1.4.2`) always goes through
`process_siemens_csi_xa` / `process_siemens_svs_xa`, which **negates
the imag channel** (`spec[0::2] - 1j * spec[1::2]`). This is true
regardless of whether the file's `SoftwareVersions` says XA, VA, or
E11 — `sm_enhanced` is labeled E11 but spec2nii still negates because
the SOP is Enhanced. dcm2niix's MRSI writer mirrors: negate for
`manufacturer == SIEMENS` AND `mrsAcqType != kMRSAcqNone` (the
Enhanced gate). For classic CSA-Non-Image SOP (`1.3.12.2.1107.5.9.1`,
VB/VE) the convention is `spec[0::2] + 1j * spec[1::2]` (no negation).

**`-0.0` canonicalization on zero-padded spectral tails.** DICOM
sources sometimes store the imag channel of zero-padded spectral
samples as `-0.0` (bit pattern `0x80000000`). spec2nii's numpy
expression `-1j * 0.0` collapses both `±0.0` to `+0.0`. dcm2niix's
SVS path's preservation guard `if (imag != 0.0f) imag = -imag;`
accidentally preserves `-0.0` because both ±0.0 compare equal to
`0.0f`. For MRSI's frequent zero-padded tails that diverged by ~5%
of total bytes vs spec2nii. The MRSI writer uses
`raw[2*i+1] = -raw[2*i+1] + 0.0f` (or `+ 0.0f` alone for the
no-negate VB/VE arm) — `+0.0f` canonicalizes `-0.0 → +0.0` per IEEE
754.

**Classic VB/VE MRSI dim from CSA, not public tags.** Classic Siemens
CSI files use SOP `1.3.12.2.1107.5.9.1` (CSA Non-Image Storage) and
have NO public `Rows` / `Columns` / `NumberOfFrames`. They live
inside the CSA image header. dcm2niix's `readCSAforMRS` reads them
into `xyzDim[]`; the MRSI dispatch fires on
`mrsAcqType == kMRSAcqNone && hasSpatialGridForMrsi`.

**CSA tag order matters.** Inside the CSA image header the tag
**order** is `SliceThickness` → `PixelSpacing` → `Columns` → `Rows`
→ `NumberOfFrames` → `VoiPosition` → `VoiPhaseFoV` → `VoiReadoutFoV`
→ `VoiThickness`. Note `PixelSpacing` comes BEFORE `Rows`/`Columns`,
so a gate of the form "only read PixelSpacing when we know it's
MRSI" can't be implemented at PixelSpacing time. Implementation: read
PixelSpacing unconditionally and have the SVS-only `VoiPhaseFoV` /
`VoiReadoutFoV` handlers skip via their existing `xyzMM[k] <= 1.0f`
sentinel check.

**`PixelSpacing[0] ↔ PixelSpacing[1]` swap.** spec2nii's
`orientationFuncs.py:90` does `xyzMM[1], xyzMM[0] = xyzMM[0], xyzMM[1]`
before scaling the rotation matrix columns. For non-square pixel CSI
(e.g. VB 3D CSI 11.25 × 9.375) this swap is required for sform
parity; for square pixels it's invisible. dcm2niix's MRSI writer
performs the swap inside the `m_ij` computation and the pixdim
emission, gated on `manufacturer == SIEMENS` (UIH does NOT swap).

**Classic Siemens MRSI IPP shift.** dcm2niix's CSA reader populates
`patientPosition` from `VoiPosition` (= the SVS-path source-of-truth
= grid CENTER for MRSI). spec2nii reads CSA `ImagePositionPatient`
(= the grid CORNER). The MRSI writer converts: `IPP = VoiCenter -
(cols/2)*PxlSp[0]*row1_dir - (rows/2)*PxlSp[1]*row2_dir -
((slices-1)/2)*SliceThickness*slice_normal`. Empirical (sm_classic,
F3T_voi_in_mrsi, VB/VE 3D CSI). Note: **x/y use `cols/2`** (whole
half-grid) but **z uses `(slices-1)/2`** — DICOM IPP convention is
first-voxel-center, so the grid corner is `cols/2 - 0.5` half-
pixels from grid center in-plane; for the slice axis VoiPosition is
at the middle slice's center.

**`half_shift=True` on classic Siemens MRSI.** spec2nii's
`process_siemens_csi_vx` uses `half_shift=True`. The half-shift is
`[0.5, 0.5, 0] @ Q44.T` — but **all three position components** pick
up contributions when the rotation is non-identity. For axis-aligned
scans (sm_classic) the z component falls out because `m20=m21=0`,
but for rotated VB/VE 3D CSI (e.g. `csi_se_3D_C>S23.5>T20.3`) the
rotated IOP makes `m20` and `m21` nonzero, so the slice-axis
translation picks up a real `sz = 0.5*(m20 + m21)` contribution.
Forgetting this was the last sticking point on Phase 6.

**Multi-echo sLASER total TE.** The DICOM `(0018,0081) EchoTime`
only carries `alTE[0]`. The pulse sequence's true total TE is the
sum of `alTE[0..N]` from the Phoenix Protocol. `siemensMrsTotalEchoTimeUs`
at `nii_dicom_batch.cpp:~11588` re-opens the CSA header and sums.
**Audit caveat**: if `siemensCsaAscii` early-returns before the
ASCCONV path runs, `csaAscii.alTE[]` is uninitialized stack data
and the sum corrupts `d0->TE`. The round-5 audit fix at line
`~11486` zero-inits the struct (`TCsaAscii csaAscii = {};`). Other
five `siemensCsaAscii` call sites don't read `alTE[]` — left alone.

**M4 RxCoil precedence (MRS path).** spec2nii's `RxCoil` reads CSA
`ReceivingCoil` first, falling back to `ImaCoilString` when the
former has zero items (common on VB/VE classic Siemens MRS, where
`ReceivingCoil` exists but is empty and the short coil-element
identifier lives in `ImaCoilString`). dcm2niix's M4 fix at
`nii_dicom.cpp:1862-1907` overrides the public `(0018,1250)
ReceiveCoilName` (long marketing label) with whichever CSA value is
nonempty. **Known quirk**: when both CSA values are nonempty, the
LATER one in CSA tag order wins (no empty-target guard). Corpus has
no observed both-nonempty-and-disagreeing case so the divergence is
theoretical. The lock-in is a one-line addition
(`if (d->coilName[0] != '\0') break;`) at the top of the `||`
branch if it ever surfaces.

### Philips

**Classic SVS conjugation.** spec2nii's `philips_dcm.py:91` applies
`.conj()` (imag negation) to the FID with the comment "Data appears
to require conjugation to meet standard's conventions." dcm2niix's
Philips SVS writer mirrors with the same `-x + 0.0f` canonicalization
as the Siemens path.

**Classic SVS IOP-negation on sform.** spec2nii's
`_enhanced_dcm_svs_to_orientation` (philips_dcm.py:341) does
`imageOrientationPatient *= -1` on both IOP rows before building the
rotation. dcm2niix's MRS branch of `saveDcm2NiiMRS` does the same
inside the m_ij build, gated on `manufacturer == kMANUFACTURER_PHILIPS`.

**Per-frame IOP sometimes mirrors the wrong slab pair (45deg_AP).**
On Enhanced Philips MRS the per-frame `(0020,0037) IOP` occasionally
encodes `slabs[1] + slabs[2]` instead of `slabs[0] + slabs[1]`
(observed on `SV_phantom_45deg_AP`). spec2nii reads `SlabOrientation`
directly from `(0018,9126) VolumeLocalizationSequence` to dodge this.
dcm2niix's parser reads SlabOrientation into `TDICOMdata.slabOrient[]`;
`saveDcm2NiiMRS` prefers it over per-frame IOP for Philips MRS when
`slabOrientCount >= 2`.

**Classic 2× payload `_mrsref` companion.** Classic Philips SVS
sometimes packs `[main_FID, water_ref_FID]` in `(5600,0020)` as a 2×
multiplier (vs single-frame). dcm2niix's writer detects this and
emits a paired `<stem>_mrsref.nii(.gz)` companion alongside the
main `_svs`. 3×+ multipliers (multi-coil / dynamics / edit-on-off)
are treated as plain `dim[5]` dynamics; vendor-state interpretation
is out of scope for the C side.

**MEGA-PRESS reshape was reverted from C → moved to `mrs_post.py`.**
Per the MRS split policy: the C side ships `press_mega` raw at
`dim[5] = 297` (all 288 main + 9 reference frames intact). The
spec2nii-style reshape `(1024, 144, 2)` with edit ON/OFF + paired
`_mrsref` lives in the Python tool. Direct dcm2niix users get a
correct-and-complete raw bundle; comparator users go through
`--with-mrs-post`.

### UIH

**IOP encoded as `direction × PixelSpacing`.** UIH MRS encodes
`(0020,0037) IOP` with non-unit row magnitudes equal to PixelSpacing
(unlike Siemens/Philips which use unit IOP). dcm2niix's MRSI writer
normalizes both row vectors before the `m_ij` scaling so xyzMM
isn't double-counted. Gated on `manufacturer == kMANUFACTURER_UIH`;
non-UIH IOP fails closed via the strict `r1mag ∈ [0.5, 1.5]`
`orientShapeOK` check (audit round-3 H1).

**`(0065,FF06)[2] < 0` flips slice direction.** UIH MRSI encodes a
slice-axis handedness flip in this private tag. spec2nii's
`uih.py:243` negates the third column of the rotation matrix when
the value is negative — in the corpus this is always the case. The
MRSI writer mirrors by negating the cross-product result `rz0/rz1/rz2`
for UIH, and sets `pixdim[0] = -1.0` (qfac handedness flag).

**Extra `[0, 0, 0.5] @ Q44.T` half-shift.** spec2nii's `uih.py:247`
adds this on top of the standard `half_shift=True` shift, giving an
effective `[0.5, 0.5, 0.5] @ Q44.T`. dcm2niix's MRSI writer combines
both: `sx = -0.5*(m00+m01+m02)`, `sy = -0.5*(m10+m11+m12)`,
`sz = +0.5*(m20+m21+m22)`.

**Different byte layout.** UIH packs the DICOM `(5600,0020)` payload
as `(cols, rows, frames, spec)` C-order, then spec2nii applies
`swapaxes(0,1)`. Siemens packs as `(slices, rows, cols, spec)`. The
MRSI writer branches its transpose on `manufacturer == UIH`.

---

## DICOM data layout gotchas

### MRS FID payload location

- **Siemens VB/VE classic** (`1.3.12.2.1107.5.9.1`): private
  `(7FE1,1010)`.
- **Siemens XA + Enhanced MR Spectroscopy** (`1.2.840.10008.5.1.4.1.1.4.2`):
  public `(5600,0020)`.
- **Philips classic + Enhanced**: public `(5600,0020)`.
- **UIH**: public `(5600,0020)`.

### MRSI spatial dim packing

- **dcm2niix NIfTI Fortran-order**: `dim[1]=cols` varies fastest,
  then `dim[2]=rows`, then `dim[3]=slices`, then `dim[4]=spec_pts`.
- **Siemens Enhanced DICOM `(5600,0020)`**: `(slices, rows, cols,
  spec)` C-order. Transpose to NIfTI: `new[c,r,s,p] = raw[s,r,c,p]`.
- **UIH DICOM `(5600,0020)`**: `(cols, rows, frames, spec)` C-order.
  Different transpose: `new[r,c,f,p] = raw[c,r,f,p]`.

### `kMaxEPI3D` cap

Volumes with `dim[3] > kMaxEPI3D` MUST still get affine finalisation
— do not early-return out of `headerDcm2Nii2()` in the high-slice
branch (commit `1cd1620` fixed a regression there).

### Stack ceiling — TDICOMdata size

`TDICOMdata` is passed BY VALUE through 5 functions in the save chain
(`saveDcm2NiiCore → nii_loadImgXL → headerDcm2Nii → headerDcm2Nii2 →
headerDcm2NiiSForm`). `readDICOMx` allocates ~1.5 MB of stack. At
~9.6 KB per struct the chain just fits the macOS 8 MB main-thread
stack; growing the struct by even ~4 KB tips `headerDcm2NiiSForm`'s
prologue probe past the guard page and crashes (dcm2niix issue #877).

MRS additions tracked against this budget:
- `slabOrient[7]` (float) + `slabOrientCount` (int): +32 B (round 4).
- `voiPhaseFoV`, `voiReadoutFoV`, `voiThickness`: +12 B (Phase 6).
- `voiCenterLPS[3]` (double): +24 B (Phase 6 — double for precision).
- Total MRS-cycle addition: **+68 B** (~1.7 % of the +4 KB margin).

Anything larger should use the `deID_CS` heap-pointer template
(8-byte pointer + lazy `calloc` + `free_TDICOMdata_<name>` helper).

---

## Sidecar precision gotchas

### VOI matrix needs full double precision

The BIDS-MRS `VOI` field is a 4×4 matrix. dcm2niix's float32-stored
IOP introduces ~1e-7 precision noise relative to spec2nii's float64
source. For sub-millimetre `VoiCenterLPS` values the float32 round
shows up at the 8th significant digit (e.g. spec
`-0.0548384089838` vs float32-round `-0.054838409`). Fixes:

- `TDICOMdata.voiCenterLPS[3]` declared as **`double`** (not `float`).
- Classic CSA path uses a new `csaMultiDouble` helper (sibling of
  `csaMultiFloat`) that parses DS-text items as double.
- Enhanced DICOM path uses `dcmFloatDouble` directly per axis
  (bypassing `dcmMultiFloatDouble` which downcasts to float32).
- Sidecar emission at `%.17g` for full round-trip.

### Comparator nested-list tolerance

After the precision fix above, the leading-IOP float32 noise still
shows up in the VOI rotation columns (because dcm2niix's
`d->orient` is float[7]). The comparator's recursive `_nested_close`
applies the same 5-ULP / 1e-5-relative tolerance at each leaf, so
matrices that match in numpy float64 vs C float32 are accepted as
equal. Without this nested tolerance the only fix would be promoting
`d->orient` to double, which is far more invasive.

---

## Comparator quirks

### `_mrsref` companion suffix discovery

When dcm2niix emits multiple outputs (`_svs` main + `_mrsref`
companion), `run_dcm2niix` picks the file whose JSON `BidsGuess[1]`
matches the dataset's expected suffix. Round-3 H1 caught a bug where
the spec2nii side was picking the wrong file in the `_mrsref` case;
both sides now do BidsGuess-keyed selection.

### `mrs_post.py` mode replaces bundled with split outputs

When `--with-mrs-post` produces a real split, `_maybe_run_mrs_post`
**removes the bundled input `.nii` + `.json`** so the comparator's
existing BidsGuess-match logic picks the split's main output as the
parity target. No-op when `mrs_post` returns without producing
splits (most cases).

### Alias table for spec2nii ↔ dcm2niix sidecar field naming

spec2nii emits names like `RxCoil`, `TxCoil`, `ExcitationFlipAngle`;
dcm2niix uses `ReceiveCoilName`, `TransmitCoilName`, `FlipAngle`.
The `BIDS_MRS_ALIASES` table in `spec2nii_compare.py` maps them so
the same value isn't flagged as `ref_only`/`out_only`.

### Float32 vs JSON integer

spec2nii emits `90.0` while dcm2niix sometimes emits `90` for the
same integer-valued field (JSON-typed differently). The alias-pair
compare uses the same `_floats_close` tolerance the generic loop
uses (round-3 H4 follow-up).

### `JSON—` sentinel preserved

`json_state="not-produced"` shows as `JSON—` in the printer so it's
distinguishable from `JSON✓` (clean) and `JSON Δn` (n diffs). Audit
round-3 M5.

---

## Decisions locked along the way

### Q1 Suffixes

Support every BIDS-MRS suffix where spec2nii has a DICOM sample in
its corpus: `_svs` ✓, `_mrsi` ✓, `_mrsref` ✓, `_unloc` deferred (no
corpus driver).

### Q2 GE

Skipped entirely. spec2nii has no GE DICOM-MRS source data; the GE
corpus is P-files only.

### Q3 Validation tolerance

Byte-identical FID + sform float32-precise — the XA60 SVS standard.

### Q5 Anonymisation

`-ba` modes: `y` (default; strip dates + PII), `n` (keep both),
`o` (omit PII, keep timestamps). `reproinx.py` uses `o` internally.

### Q6 Validation data

Validate live against spec2nii's output on the same source DICOMs
(no new tracked test data). Building our own validation corpus is
deferred.

### Q8 Commit cadence

One commit per vendor family (Phase 1 shipped Siemens, then Philips,
then UIH).

### MRS split policy (2026-06-07)

dcm2niix bundles **raw multi-DICOM / multi-frame MRS series** as a
single NIfTI (`dim[5] = total frames`, no reorder, no drop). The
existing C-side per-DICOM split criteria (multi-echo `EchoTime`,
multi-PLD ASL, coil splitting under `-m o`) all key off **standard,
public DICOM tags** that vary per file — those stay in the C path.
Vendor-sequence-state-specific reshapes/splits (CMRR sLASER DKD
ref-grouping, Philips MEGA-PRESS edit-on/off + water-ref crop,
HYPER edit pairing) live in vendor Phoenix Protocol or per-frame
private SQs — that's interpretation, not DICOM semantics, and
belongs above the converter.

---

## Future / out-of-scope work

### Nice to have

- **`saveDcm2NiiMRS` extraction refactor (P4.1)**. The MRSI writer
  landed as a sibling `saveDcm2NiiMRSI` rather than via P4.1's
  extracted helpers; the SVS / MRSI paths now duplicate ~30 % of
  the affine + header logic. Extracting shared helpers
  (`mrsValidateMembers`, `mrsBuildHeader`, `mrsWriteFID`,
  `mrsWriteSidecar`) would reduce duplication without changing
  observable behaviour.

- **HYPER + orientation_tests parity validation harness**. Both
  SKIP groups are bounded by spec2nii reference availability, not
  by dcm2niix capability (orientation_tests in particular). A
  non-spec2nii validation path — compare to SPAR/SDAT-derived
  reference, or smoke-test that dcm2niix output is well-formed
  BIDS-MRS — could close them.

- **`_unloc` (unlocalized MRS)**. No corpus driver currently. Defer
  until one appears.

- **`csi_se` 2D VB/VE CSI** variants outside the comparator
  inventory (e.g. `csi_se_C>S23.5>T20.3` rather than the 3D
  variants). The same pipeline should handle them by inspection,
  but they aren't validated.

### `mrs_post.py` extensions

- **HYPER edit interpretation**. The current MEGA-PRESS handler
  covers the simplest edit-on/off case; HYPER's two-step interleave
  is more complex.

- **Multi-coil split**. Philips Enhanced `>=3×` payloads that turn
  out to be coil-axis-bundled rather than dynamic-axis. Needs a
  vendor-private-tag inspection to disambiguate.

### Comparator extensions

- **`--with-spar-ref` mode**. Use the SPAR/SDAT-derived reference
  for the orientation_tests SKIPs. Would close 9 of 10 SKIPs.

- **BIDS-MRS validator integration**. Smoke-test dcm2niix output
  for well-formedness when no reference is available.

---

## Attribution

The dcm2niix MRS pipeline is ported from
[spec2nii](https://github.com/wtclarke/spec2nii) (BSD-3-Clause,
William Clarke, U. Oxford 2020). The C-side writer's vendor
branches mirror spec2nii's per-vendor modules:

- `spec2nii/Siemens/dicomfunctions.py` —
  `process_siemens_svs_vx` / `_xa`, `process_siemens_csi_vx` / `_xa`,
  `identify_integrated_references`, `_detect_and_fill_voi` /
  `_detect_and_fill_voi_enh`.
- `spec2nii/Philips/philips_dcm.py` — `_process_philips_svs_new`,
  `_enhanced_dcm_svs_to_orientation`.
- `spec2nii/uih.py` — `process_uih_svs`, `process_uih_csi`.
- `spec2nii/dcm2niiOrientation/orientationFuncs.py` —
  `dcm_to_nifti_orientation`, `nifti_dicom2mat`, `verify_slice_dir`,
  `apply_half_voxel_shift`.

Reading those alongside dcm2niix's `saveDcm2NiiMRS` /
`saveDcm2NiiMRSI` / `readCSAforMRS` is the fastest way to come up to
speed on any MRS regression.
