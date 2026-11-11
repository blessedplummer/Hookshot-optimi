"""Two-tier pipelined write-back (Phase 2, gpu/vk/vk_memory.cpp): URGENT vs BULK tiers, the gated classifier and the
urgent-value cache.

A compiled fixture driving the production pieces: wb_tier_of (readback pages, runs of 16 KB or less and odd leftovers
are URGENT, everything else BULK), wb_tier_of_gated (RX_VK_DIRTY_ORIGIN=0 forces everything BULK, as before) and
WbUrgentCache (the last urgent value kept for a stable small-result set: a hit only for the same range at the same note
sequence, anything else re-scanned).

CPU-compiled, no game/GPU.

python -I -B tests/test_wb_tier_split.py [--cxx g++]
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
    enum = block(text, 'enum class WbTier') + ';'
    tier = block(text, 'inline WbTier wb_tier_of(')
    gated = block(text, 'inline WbTier wb_tier_of_gated(')
    cache = block(text, 'struct WbUrgentCache {') + ';'
    fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
''' + enum + '\n' + tier + '\n' + gated + '\n' + cache + r'''
int main() {
    // (the tiers: readback pages are always urgent, whatever their size; 16 KB or less is urgent; odd leftovers are
    // urgent; a big aligned framebuffer run is bulk)
    CHECK(wb_tier_of(4, false, false) == WbTier::Urgent);
    CHECK(wb_tier_of(16u << 10, false, false) == WbTier::Urgent);
    CHECK(wb_tier_of((16u << 10) + 4, false, false) == WbTier::Bulk);
    CHECK(wb_tier_of(1u << 20, false, false) == WbTier::Bulk);
    CHECK(wb_tier_of(1u << 20, true, false) == WbTier::Urgent);
    CHECK(wb_tier_of(7, true, false) == WbTier::Urgent);
    CHECK(wb_tier_of(1u << 20, false, true) == WbTier::Urgent);
    CHECK(wb_tier_of(0, false, false) == WbTier::Urgent);   // (empty: harmless, urgent)
    // (the kill switch: off forces everything bulk, the pre-split whole path)
    CHECK(wb_tier_of_gated(4, false, false, false) == WbTier::Bulk);
    CHECK(wb_tier_of_gated(1u << 20, true, true, false) == WbTier::Bulk);
    CHECK(wb_tier_of_gated(4, false, false, true) == WbTier::Urgent);
    CHECK(wb_tier_of_gated((16u << 10) + 4, false, false, true) == WbTier::Bulk);
    CHECK(wb_tier_of_gated(1u << 20, true, false, true) == WbTier::Urgent);
    // (the cache: cold misses, a store hits for the same range at the same sequence only)
    WbUrgentCache c;
    CHECK(!c.hit(0x1000, 0x2000, 1));   // (cold: nothing stored)
    c.store(0x1000, 0x2000, 42, true, 1);
    CHECK(c.hit(0x1000, 0x2000, 1));
    CHECK(!c.hit(0x1000, 0x2001, 1));   // (a different range: re-scanned)
    CHECK(!c.hit(0x0, 0x2000, 1));
    CHECK(!c.hit(0x1000, 0x2000, 2));   // (an urgent note since: re-scanned)
    c.store(0x1000, 0x2000, 43, true, 2);
    CHECK(c.hit(0x1000, 0x2000, 2));   // (re-stored at the new sequence: hit again)
    WbUrgentCache d;   // (a range never written back: stored with ever false, still cached)
    d.store(0x3000, 0x4000, 0, false, 5);
    CHECK(d.hit(0x3000, 0x4000, 5));
    d.reset();
    CHECK(!d.hit(0x3000, 0x4000, 5));
    std::puts("PASS: readback/small/odd runs are URGENT, big aligned ones BULK; off forces BULK; "
        "the urgent cache hits only for the same range at the same note sequence");
}
'''
    with tempfile.TemporaryDirectory(prefix='wb-tier-split-') as tmp:
        src = Path(tmp) / 'tier.cpp'
        exe = Path(tmp) / 'tier.exe'
        src.write_text(fixture, encoding='utf-8')
        subprocess.run([args.cxx, '-std=c++20', '-O1', '-Wall', '-Wextra', '-Werror', str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
