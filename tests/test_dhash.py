"""Our dHash (tellybox.detect.dhash) must stay bit-identical to imagehash.dhash.

Title-card hashes are stored in split_reference.card_hash. The expected values below were
generated with imagehash 4.3 (``str(imagehash.dhash(Image.fromarray(a), hash_size=8))``) on the
deterministic inputs of ``inputs()``. When the dependency was dropped, the same check ran against
imagehash on those inputs plus 41 real frames and crops from a synthetic compilation: no mismatch.
"""

from __future__ import annotations

import numpy as np
import pytest

from tellybox import detect


def inputs() -> list[np.ndarray]:
    rng = np.random.default_rng(1234)
    out = []
    for _ in range(60):  # noise, odd sizes
        h, w = int(rng.integers(2, 240)), int(rng.integers(2, 320))
        out.append(rng.integers(0, 256, (h, w), dtype=np.uint8))
    for v in (0, 1, 127, 128, 255):  # flat
        out.append(np.full((36, 64), v, np.uint8))
    for h, w in [(1, 1), (1, 9), (8, 1), (9, 9), (7, 13), (33, 17), (180, 320), (235, 319)]:
        out.append(np.zeros((h, w), np.uint8) + 7)
        out.append(np.linspace(0, 255, w)[None, :].repeat(h, 0).astype(np.uint8))  # gradients
        out.append(np.linspace(255, 0, w)[None, :].repeat(h, 0).astype(np.uint8))
        out.append(np.linspace(0, 255, h)[:, None].repeat(w, 1).astype(np.uint8))
        out.append(((np.indices((h, w)).sum(0) % 2) * 255).astype(np.uint8))  # checkerboard
    out.append(rng.integers(0, 256, (50, 70, 3), dtype=np.uint8))  # RGB
    return out


EXPECTED = [
    '25296cc96644ab6d', '23b64666ab66c6a2', '984284c4131294b3', 'e6d01697de29659d',
    'f83317038ccccccc', 'e0a6446ab2061a66', '35b229a626446d3b', '4c6629bd8d49698c',
    '62da9b1589495e18', '244b528adcd65ba7', 'aaa628c8d692b25a', '49cea6b349083366',
    '67b467cecf69268c', 'f5c6e92a96e6ac8b', '5125658a9aa5a92b', '524652d8d92a9bdd',
    'c469330b522c9ea7', 'cc32b2e5aa29acd4', '942412656c2ee795', '5969ae15b5462429',
    'd426574959b1dc33', '3265b514c41a4c51', 'aa30575a5c94ab4b', '164e186da43524ac',
    '2dcad089ca6c71b1', '4962c94d34354999', 'ea626c4ae5253239', '462293940e29729a',
    'aaa509aacad292ad', 'd2693dd34d93b348', '5561655aa3d1e069', '5366224bb4a61328',
    'a39ba52942ca9ca5', '66c85ac58b686096', 'c7b5552ad461a98c', '2a9212462b9533ab',
    '31354b0d229db6c2', '6bb18c1496caa5b9', 'adade5f1b8a8a9a9', '69a5051525cd670d',
    '3ccc846a511d99e5', '9b9792674a1b7b2d', 'e4aa94a555ad4d54', 'ad6412ec45495a92',
    'cb65a5cd4a6865a9', '212cdca6c9264895', '4d8daa6ad6389888', '4628d992b164e696',
    '6aaa351326489394', 'e4c5994c74cdd456', '26ad2d59993c2666', '2e2226b439299971',
    '5b8c5a562589935a', '9464545421a4d948', '6d8a5058aa5ba971', 'ced8654ab8482a05',
    'd09a5a3292b45158', '4b4ac4b33be4d45c', 'a1a5abd373dcae16', '2a522c4a568d9291',
    '0000000000000000', '0000000000000000', '0000000000000000', '0000000000000000',
    '0000000000000000', '0000000000000000', '0000000000000000', '0000000000000000',
    '0000000000000000', '0000000000000000', '0000000000000000', 'ffffffffffffffff',
    '0000000000000000', '0000000000000000', 'aaaaaaaaaaaaaaaa', '0000000000000000',
    '0000000000000000', '0000000000000000', '0000000000000000', '0000000000000000',
    '0000000000000000', 'ffffffffffffffff', '0000000000000000', '0000000000000000',
    'aa55aa5555aa55aa', '0000000000000000', 'ffffffffffffffff', '0000000000000000',
    '0000000000000000', '9249924949924992', '0000000000000000', 'ffffffffffffffff',
    '0000000000000000', '0000000000000000', '8241824141824182', '0000000000000000',
    'ffffffffffffffff', '0000000000000000', '0000000000000000', 'a54aa500004aa54a',
    '0000000000000000', 'ffffffffffffffff', '0000000000000000', '0000000000000000',
    '8249820808824982', 'b093abbab6b44454',
]


def test_dhash_matches_imagehash_values():
    arrays = inputs()
    assert len(arrays) == len(EXPECTED)
    for i, (a, expected) in enumerate(zip(arrays, EXPECTED)):
        assert detect.hash_hex(detect.dhash(a)) == expected, f"input {i}"


def test_hash_hex_pads_to_16_digits_and_round_trips():
    assert detect.hash_hex(0) == "0" * 16
    assert detect.hash_hex(int("00ab", 16)) == "00000000000000ab"
    assert int(detect.hash_hex(2**64 - 1), 16) == 2**64 - 1


@pytest.mark.parametrize(("a", "b", "d"), [(0, 0, 0), (0, 1, 1), (0xFF, 0x0F, 4), (2**64 - 1, 0, 64)])
def test_hamming(a, b, d):
    assert detect.hamming(a, b) == d
