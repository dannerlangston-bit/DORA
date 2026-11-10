"""Perceptual hash of 3.7 ("Heatmap and hash") and its Hamming distance.

pHash: 32x32 grayscale, 2-D DCT, top-left 8x8 coefficients, threshold at their median, 64 bits,
written as 16 lowercase hex characters (the form `Finding.phash`, `Signature.phash` and the
fingerprint's `render_probe_hash` carry). Bits are row-major over the 8x8 block, most significant
bit first; the DC coefficient is included in the block and in the median, as in the common
`imagehash.phash` formulation (choice: the spec does not say whether DC is excluded).

Hamming tolerance for decision signatures is 8 of 64 on tier-2 and tier-3 checkpoints and 6 on
tier-1 checkpoints and for cross-checkpoint clustering (3.7, 9.7); `render_probe_hash` is compared
at distance 0 (3.3).
"""
from __future__ import annotations

import cv2
import numpy as np

HASH_SIZE = 32
LOW = 8
CROP_PAD_PX = 8


def phash(image: np.ndarray) -> str:
    """64-bit DCT perceptual hash of an RGB (H x W x 3) or grayscale (H x W) uint8 image."""
    if image.size == 0:
        raise ValueError("phash of an empty image")
    gray = image if image.ndim == 2 else cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray.astype(np.float32), (HASH_SIZE, HASH_SIZE), interpolation=cv2.INTER_AREA)
    low = cv2.dct(small)[:LOW, :LOW]
    bits = (low > np.median(low)).flatten()
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return f"{value:016x}"


def hamming(a: str, b: str) -> int:
    """Number of differing bits between two 16-hex-character hashes."""
    if len(a) != 16 or len(b) != 16:
        raise ValueError("pHash values are 16 hex characters")
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def tolerance(tier: int | None) -> int:
    """Default Hamming tolerance for decision-signature matching (3.7): 6 on tier 1, else 8."""
    return 6 if tier == 1 else 8


def component_crop(image: np.ndarray, bbox: tuple[int, int, int, int], pad: int = CROP_PAD_PX) -> np.ndarray:
    """The anchored component crop of 3.7: the component's (x, y, w, h) box plus `pad` px, clipped."""
    x, y, w, h = bbox
    H, W = image.shape[:2]
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
    return image[y0:y1, x0:x1]
