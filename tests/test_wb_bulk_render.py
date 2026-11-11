"""Resolution-aware capture budget (Phase 2, gpu/vk/vk_memory.cpp): bulk-render pools.

A compiled fixture driving the production pieces: WbBulkHistory (a pool of at least 1 MB whose write-backs cover more
than half its size at 3+ consecutive bulk epochs is bulk-render; a light, half, empty or CPU-written submit breaks the
streak; a whole submission with no write-back shows idle and is swept) and wbd_pool_excluded_from_capture (excluded
from the capture's scans only while the split is on: RX_VK_DIRTY_ORIGIN=0 keeps every pool scanned, as before, so
small uniform/constant pools stay in capture).

CPU-compiled, no game/GPU.

python -I -B tests/test_wb_bulk_render.py [--cxx g++]
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
    hist = block(text, 'struct WbBulkHistory {') + ';'
    excl = block(text, 'inline bool wbd_pool_excluded_from_capture(')
    fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
''' + hist + '\n' + excl + r'''
int main() {
    CHECK(WbBulkHistory::kMinSize == (1u << 20));   // (only pools of 1 MB or more can go bulk)
    WbBulkHistory h;
    CHECK(!h.bulk && !h.idle(1));   // (epoch 0, fresh: observed "now", never idle)
    h.observe(70u << 20, 100u << 20, 1);   // (over half: one)
    CHECK(!h.bulk && !h.idle(2));
    h.observe(60u << 20, 100u << 20, 2);   // (two)
    CHECK(!h.bulk);
    h.observe(51u << 20, 100u << 20, 3);   // (three in a row: bulk-render)
    CHECK(h.bulk && !h.idle(4));
    CHECK(h.idle(5));   // (no write-back for a whole submission: the sweep resets it)
    h.observe(90u << 20, 100u << 20, 4);   // (stays bulk while covered)
    CHECK(h.bulk);
    h.observe(0, 100u << 20, 5);   // (a CPU-written submit is observed as zero: the streak breaks at once)
    CHECK(!h.bulk);
    h.observe(50u << 20, 100u << 20, 6);   // (exactly half is not over half: still light)
    CHECK(!h.bulk);
    h.observe(0, 0, 7);   // (an empty pool: not bulk)
    CHECK(!h.bulk);
    h.observe(0, 100u << 20, 8);   // (nothing written back: streak broken)
    CHECK(!h.bulk);
    WbBulkHistory u;   // (small pools never trip it: a fully rewritten 512 KB pool stays in capture as a class)
    u.observe(512u << 10, 512u << 10, 1);
    u.observe(512u << 10, 512u << 10, 2);
    u.observe(512u << 10, 512u << 10, 3);
    CHECK(!u.bulk);
    WbBulkHistory v;   // (a 1 MB pool fully rewritten three times is bulk; one light submit re-includes it)
    v.observe(1u << 20, 1u << 20, 1);
    v.observe(1u << 20, 1u << 20, 2);
    v.observe(1u << 20, 1u << 20, 3);
    CHECK(v.bulk);
    v.reset();
    CHECK(!v.bulk);
    // (the exclusion: bulk-render pools leave the capture's scans only while the split is on)
    CHECK(wbd_pool_excluded_from_capture(true, true));
    CHECK(!wbd_pool_excluded_from_capture(true, false));   // (kill switch: scanned, as before)
    CHECK(!wbd_pool_excluded_from_capture(false, true));
    CHECK(!wbd_pool_excluded_from_capture(false, false));
    std::puts("PASS: >50% of >=1 MB for 3+ consecutive submits is bulk-render; light, half, empty, small and "
        "CPU-written break it; idle sweeps; the kill switch keeps every pool scanned");
}
'''
    with tempfile.TemporaryDirectory(prefix='wb-bulk-render-') as tmp:
        src = Path(tmp) / 'bulk.cpp'
        exe = Path(tmp) / 'bulk.exe'
        src.write_text(fixture, encoding='utf-8')
        subprocess.run([args.cxx, '-std=c++20', '-O1', '-Wall', '-Wextra', '-Werror', str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
