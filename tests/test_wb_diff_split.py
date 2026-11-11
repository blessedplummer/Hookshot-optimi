"""CPU check of the write-back diff's chunks (gpu/vk/vk_memory.cpp wbd_split): a run in whole words comes out as its words
before the first 16-byte vector, the vectors 64 KB at most at a time, and the words after the last vector, covering the run
exactly once and in order, every vector chunk 16-byte aligned and whole. Also the dirty-origin predicates (Phase 1):
wbd_keep_page (the end culls to unknown-to-shadow pages, GPU-dirty ones and pending ones) and dirty_origin_of
(0 clean, 1 CPU-only, 2 GPU-only, 3 both).

python -I -B tests/test_wb_diff_split.py [--cxx clang++]
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

RUN_ALL_ARGS = ["--cxx", "g++"]

ROOT = Path(__file__).resolve().parents[1]


def block(text, marker):
    start = text.index(marker)
    end = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cxx', default='g++')
    parser.add_argument('--source', type=Path, default=ROOT / 'gpu/vk/vk_memory.cpp')
    args = parser.parse_args()
    text = args.source.read_text(encoding='utf-8')
    helper = block(text, 'template <class F> void wbd_split(')
    keep = block(text, 'inline bool wbd_keep_page(')
    origin = block(text, 'inline uint8_t dirty_origin_of(')
    fixture = r'''
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed: %s (at %llu size %llu)\n", #ok, \
    (unsigned long long)at0, (unsigned long long)size0); std::exit(1); } } while (0)
static uint64_t at0, size0;
''' + helper + keep + origin + r'''
static void check(uint64_t at, uint64_t size) {
    at0 = at, size0 = size;
    uint64_t next = at, vec_bytes = 0;
    int pieces = 0, words_before = 0, words_after = 0;
    bool seen_vecs = false;
    wbd_split(at, size, [&](uint64_t a, uint64_t len, bool vecs) {
        CHECK(a == next && len > 0);   // (in order, no gap, no overlap, nothing empty)
        CHECK(a % 4 == 0 && len % 4 == 0);
        if (vecs) {
            CHECK(a % 16 == 0 && len % 16 == 0 && len <= (64u << 10));
            CHECK(!words_after);
            seen_vecs = true, vec_bytes += len;
        } else {
            CHECK(len < 16);   // (only the odd words at an end)
            (seen_vecs ? words_after : words_before)++;
        }
        next = a + len, pieces++;
    });
    CHECK(next == at + size);
    CHECK(words_before + words_after <= 2 && words_after <= 1);   // (with no vector, its head and tail both come "before")
    CHECK(!seen_vecs || words_before <= 1);
    // (every whole vector inside the run went as one)
    const uint64_t first = (at + 15) & ~15ull, last = (at + size) & ~15ull;
    CHECK(vec_bytes == (last > first ? last - first : 0));
}
int main() {
    for (uint64_t at = 0; at < 64; at += 4)
        for (uint64_t size = 4; size <= 300; size += 4) check(0x100000 + at, size);
    for (uint64_t at = 0; at < 32; at += 4)
        for (uint64_t size : {65532ull, 65536ull, 65540ull, 65552ull, 131072ull, 131084ull, (64ull << 20) + 12})
            check(0x40000 + at, size);
    // (dirty-origin, Phase 1: the end keeps unknown-to-shadow pages, GPU-dirty ones and pending ones, culls the rest)
    CHECK(wbd_keep_page(false, false, false) && wbd_keep_page(false, false, true));
    CHECK(wbd_keep_page(false, true, false) && wbd_keep_page(false, true, true));
    CHECK(!wbd_keep_page(true, false, false));
    CHECK(wbd_keep_page(true, false, true) && wbd_keep_page(true, true, false) && wbd_keep_page(true, true, true));
    // (dirty origins: 0 clean, 1 CPU-only, 2 GPU-only, 3 both; stamped exactly at the sync is already mirrored)
    CHECK(dirty_origin_of(0, 0, 0, 0) == 0);
    CHECK(dirty_origin_of(5, 0, 4, 9) == 1 && dirty_origin_of(5, 3, 4, 9) == 1);
    CHECK(dirty_origin_of(4, 10, 4, 9) == 2 && dirty_origin_of(0, 10, 0, 9) == 2);
    CHECK(dirty_origin_of(5, 10, 4, 9) == 3);
    CHECK(dirty_origin_of(4, 9, 4, 9) == 0);
    std::puts("PASS: write-back diff chunks cover each run once, in order, vectors aligned and at most 64 KB; "
        "the cull keeps unknown, GPU-dirty and pending pages; origins classify clean/CPU/GPU/both");
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'split.cpp'
        exe = Path(tmp) / 'split.exe'
        src.write_text(fixture, encoding='utf-8')
        subprocess.run([args.cxx, '-std=c++20', '-O1', str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
