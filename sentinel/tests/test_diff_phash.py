"""pHash and Hamming distance (3.7), and the diff parameter file."""
import copy

import numpy as np
import pytest

from sentinel import synth
from sentinel.diff import load_params, params_sha256
from sentinel.diff.phash import component_crop, hamming, phash, tolerance


def test_phash_is_16_lowercase_hex_and_deterministic():
    img = synth.render(synth.sample_layout(0), (640, 400)).image
    h = phash(img)
    assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)
    assert phash(img.copy()) == h
    assert phash(img[:, :, 0]) == phash(img[:, :, 0].copy())


def test_hamming_counts_bits():
    assert hamming("0" * 16, "0" * 16) == 0
    assert hamming("0" * 16, "f" * 16) == 64
    assert hamming("000000000000000f", "0000000000000000") == 4
    with pytest.raises(ValueError):
        hamming("abc", "0" * 16)


def test_similar_crops_are_close_and_different_crops_far():
    s = synth.render(synth.sample_layout(0), (640, 400))
    send = s.crop("ticket.send", 8)
    noisy = synth.perturb(send, seed=1, count=20, amplitude=3)
    assert hamming(phash(send), phash(noisy)) <= 2
    other = s.crop("watchlist.grid", 0)
    assert hamming(phash(send), phash(other)) > 8


def test_tolerances_and_component_crop():
    assert tolerance(1) == 6 and tolerance(2) == 8 and tolerance(None) == 8
    img = np.zeros((50, 60, 3), np.uint8)
    assert component_crop(img, (10, 10, 5, 5)).shape == (21, 21, 3)
    assert component_crop(img, (0, 0, 5, 5)).shape == (13, 13, 3)          # clipped at the border


def test_params_hold_the_spec_values_and_hash_by_section():
    p = load_params()
    assert p["pixel"]["t"] == 0.1 and p["pixel"]["max_delta_scale"] == 35215
    assert p["landmarks"]["confidence_min"] == 0.92 and p["landmarks"]["second_peak_max"] == 0.85
    assert p["ssim"]["sigma"] == 1.5 and p["ssim"]["k1"] == 0.01 and p["ssim"]["k2"] == 0.03
    assert p["components"]["dilate_kernel_wh"] == [5, 3] and p["components"]["min_area_px"] == 4
    assert p["translation"]["response_min"] == 0.25 and p["translation"]["shift_max_px"] == 64
    assert p["mask_budget"]["account_info"] == 0.05
    changed = copy.deepcopy(dict(p))
    changed["settle"] = dict(changed["settle"], max_wait_ms=1)
    assert params_sha256(changed) == params_sha256(p)                      # settle hashed separately
    assert params_sha256(changed, "settle") != params_sha256(p, "settle")
