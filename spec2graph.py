#!/usr/bin/env python
"""spec2graph -- quick viewer for NIfTI-MRS spectroscopy data.

Usage:
    python spec2graph.py /path/to/nifti.nii.gz [options]

Reads a NIfTI-MRS file (complex time-domain FID), Fourier transforms it to the
frequency domain, and plots the spectrum.  Datasets with multiple samples
(transients / dynamics, stored in the higher NIfTI dimensions) can be overlaid
or averaged, and the view can be restricted to a chemical-shift / frequency
range.
"""

import argparse
import json
import warnings

# Silence a transitive urllib3/chardet version-mismatch warning from requests
# (pulled in via fslpy); it has no bearing on this viewer.
warnings.filterwarnings('ignore', message='.*urllib3.*')

import numpy as np
import nibabel as nib
import matplotlib.pyplot as plt


# Standard ppm reference (chemical shift of the receiver centre) per nucleus.
# For 1H this is the water resonance at body temperature.
DEFAULT_PPM_REF = {'1H': 4.65, '31P': 0.0, '13C': 0.0, '2H': 4.65}


def load_mrs(path):
    """Load a NIfTI-MRS file.

    Returns the complex spectral array shaped (n_points, n_samples), the dwell
    time (s), the spectrometer frequency (MHz) and the resonant nucleus.
    """
    img = nib.load(path)
    data = np.asanyarray(img.dataobj)
    if not np.iscomplexobj(data):
        raise ValueError(f'{path} does not contain complex data -- is it NIfTI-MRS?')

    # NIfTI-MRS: dims 0-2 are spatial, dim 3 is the spectral/time axis, dims
    # 4-6 are higher dimensions (transients, coils, ...).  For a viewer we keep
    # the spectral axis and flatten everything else into a "samples" axis.
    dwell = float(img.header['pixdim'][4])

    spec_freq = None
    nucleus = '1H'

    # BIDS-MRS-first lookup: the JSON sidecar is the authoritative source
    # under the BIDS spec, and dcm2niix emits SpectrometerFrequency +
    # ResonantNucleus there. spec2nii writes the same fields in a NIfTI
    # header extension (BEP005, ecode 44) — we fall back to that for files
    # produced by tools that don't write a sidecar.
    import os
    base = os.path.splitext(path)[0]
    if base.endswith('.nii'):  # strip the second extension on .nii.gz
        base = base[:-len('.nii')]
    sidecar = base + '.json'
    if os.path.isfile(sidecar):
        try:
            with open(sidecar, 'r') as f:
                meta = json.load(f)
            if 'SpectrometerFrequency' in meta:
                spec_freq = float(np.atleast_1d(meta['SpectrometerFrequency'])[0])
            if 'ResonantNucleus' in meta:
                nucleus = str(np.atleast_1d(meta['ResonantNucleus'])[0])
        except (OSError, ValueError):
            pass
    if spec_freq is None:
        for ext in img.header.extensions:
            try:
                meta = json.loads(ext.get_content())
            except (ValueError, TypeError):
                continue
            if 'SpectrometerFrequency' in meta:
                spec_freq = float(np.atleast_1d(meta['SpectrometerFrequency'])[0])
            if 'ResonantNucleus' in meta and nucleus == '1H':
                nucleus = str(np.atleast_1d(meta['ResonantNucleus'])[0])

    # Move the spectral axis (dim 3) to the front and flatten the spatial
    # (singleton for SVS) and higher dims into a single "samples" axis.
    n_points = data.shape[3]
    spectra = np.moveaxis(data, 3, 0).reshape(n_points, -1)

    return spectra, dwell, spec_freq, nucleus


def to_spectrum(fid, dwell, spec_freq, ppm_ref):
    """FFT a (n_points, n_samples) FID array to the frequency domain.

    Returns (spectrum, hz_axis, ppm_axis).  ppm_axis is None if no spectrometer
    frequency is available.
    """
    spectrum = np.fft.fftshift(np.fft.fft(fid, axis=0), axes=0)
    hz = np.fft.fftshift(np.fft.fftfreq(fid.shape[0], d=dwell))

    ppm = None
    if spec_freq:
        # Hz -> ppm.  The spectral axis is reversed relative to FFT frequency
        # (MR convention: chemical shift increases to the left), hence -hz.
        ppm = -hz / spec_freq + ppm_ref
    return spectrum, hz, ppm


def project(spectrum, mode):
    """Reduce a complex spectrum to a real trace for plotting."""
    if mode == 'real':
        return spectrum.real
    if mode == 'imag':
        return spectrum.imag
    if mode == 'magnitude':
        return np.abs(spectrum)
    if mode == 'phase':
        return np.angle(spectrum)
    raise ValueError(f'unknown mode: {mode}')


def main():
    parser = argparse.ArgumentParser(description='Plot a NIfTI-MRS spectrum.')
    parser.add_argument('file', help='NIfTI-MRS file (.nii / .nii.gz)')
    parser.add_argument('-a', '--average', action='store_true',
                        help='average the FID across samples (transients) before FFT')
    parser.add_argument('-m', '--mode', default='real',
                        choices=['real', 'imag', 'magnitude', 'phase'],
                        help='component of the complex spectrum to plot (default: real)')
    parser.add_argument('--ppm-range', nargs=2, type=float, metavar=('LOW', 'HIGH'),
                        help='restrict the x-axis to this ppm range, e.g. --ppm-range 0 5')
    parser.add_argument('--hz', action='store_true',
                        help='plot the x-axis in Hz instead of ppm')
    parser.add_argument('--ref', type=float, default=None,
                        help='ppm reference offset (default: 4.65 for 1H, 0 otherwise)')
    parser.add_argument('-o', '--out', help='save the figure to this file instead of showing it')
    args = parser.parse_args()

    fid, dwell, spec_freq, nucleus = load_mrs(args.file)
    n_points, n_samples = fid.shape

    ppm_ref = args.ref if args.ref is not None else DEFAULT_PPM_REF.get(nucleus, 0.0)

    if args.average and n_samples > 1:
        fid = fid.mean(axis=1, keepdims=True)

    spectrum, hz, ppm = to_spectrum(fid, dwell, spec_freq, ppm_ref)
    trace = project(spectrum, args.mode)

    use_hz = args.hz or ppm is None
    x = hz if use_hz else ppm
    xlabel = 'Frequency (Hz)' if use_hz else 'Chemical shift (ppm)'

    fig, ax = plt.subplots(figsize=(9, 5))
    if trace.shape[1] == 1:
        ax.plot(x, trace[:, 0], lw=0.8)
    else:
        for i in range(trace.shape[1]):
            ax.plot(x, trace[:, i], lw=0.5, alpha=0.6)

    if not use_hz:
        ax.invert_xaxis()  # MR convention: high ppm on the left
    if args.ppm_range and not use_hz:
        lo, hi = min(args.ppm_range), max(args.ppm_range)
        ax.set_xlim(hi, lo)
        # Rescale the y-axis to the data inside the visible window, otherwise
        # the off-screen water peak dominates the autoscaled range.
        visible = (x >= lo) & (x <= hi)
        if visible.any():
            vals = trace[visible]
            ymin, ymax = float(vals.min()), float(vals.max())
            pad = 0.05 * (ymax - ymin) if ymax > ymin else 1.0
            ax.set_ylim(ymin - pad, ymax + pad)

    ax.set_xlabel(xlabel)
    ax.set_ylabel(f'Intensity ({args.mode})')
    n_shown = 1 if args.average else n_samples
    title = f'{args.file}  [{nucleus}, {spec_freq:.2f} MHz]' if spec_freq else args.file
    sub = 'averaged' if (args.average and n_samples > 1) else f'{n_shown} sample(s)'
    ax.set_title(f'{title}\n{n_points} points, {sub}')
    fig.tight_layout()

    if args.out:
        fig.savefig(args.out, dpi=150)
        print(f'wrote {args.out}')
    else:
        plt.show()


if __name__ == '__main__':
    main()
