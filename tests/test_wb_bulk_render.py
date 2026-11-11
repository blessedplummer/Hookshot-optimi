"""Resolution-aware capture budget (Phase 2, gpu/vk/vk_memory.cpp): bulk-render pools.

A compiled fixture driving the production pieces: WbBulkHistory (a pool whose write-backs cover more than half its
size for 3+ consecutive write-back submissions is bulk-render; a light submission, exactly half, or an empty pool
breaks the streak) and wbd_pool_excluded_from_capture (excluded from the capture's scans only while the split is on:
RX_VK_DIRTY_ORIGIN=0 keeps every pool scanned, as before, so small uniform/constant pools stay in capture).

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
    WbBulkHistory h;
    CHECK(!h.bulk);
    h.observe(70, 100);   // (over half: one)
    CHECK(!h.bulk);
    h.observe(60, 100);   // (two)
    CHECK(!h.bulk);
    h.observe(51, 100);   // (three in a row: bulk-render)
    CHECK(h.bulk);
    h.observe(90, 100);   // (stays bulk while covered)
    CHECK(h.bulk);
    h.observe(10, 100);   // (a light submission breaks the streak: back in capture)
    CHECK(!h.bulk);
    h.observe(50, 100);   // (exactly half is not over half: still light)
    CHECK(!h.bulk);
    h.observe(0, 0);   // (an empty pool: not bulk)
    CHECK(!h.bulk);
    h.observe(0, 100);   // (nothing written back: streak broken)
    CHECK(!h.bulk);
    WbBulkHistory u;   // (small uniform pools never trip it: a few pages of megabytes)
    u.observe(4096, 1u << 20);
    u.observe(8192, 1u << 20);
    u.observe(4096, 1u << 20);
    CHECK(!u.bulk);
    u.reset();
    CHECK(!u.bulk);
    // (the exclusion: bulk-render pools leave the capture's scans only while the split is on)
    CHECK(wbd_pool_excluded_from_capture(true, true));
    CHECK(!wbd_pool_excluded_from_capture(true, false));   // (kill switch: scanned, as before)
    CHECK(!wbd_pool_excluded_from_capture(false, true));
    CHECK(!wbd_pool_excluded_from_capture(false, false));
    std::puts("PASS: >50% for 3+ consecutive write-backs is bulk-render; light, half and empty break it; "
        "small pools stay in capture; the kill switch keeps every pool scanned");
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
