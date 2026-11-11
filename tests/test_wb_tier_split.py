"""Two-tier pipelined write-back (Phase 2, gpu/vk/vk_memory.cpp): URGENT vs BULK tiers, the gated classifier and the
urgent-value cache.

A compiled fixture driving the production pieces: wb_tier_of (readback pages, runs of 16 KB or less and odd leftovers
are URGENT, everything else BULK), wb_tier_of_gated (RX_VK_DIRTY_ORIGIN=0 forces everything BULK, as before),
wb_partition_runs (the end's split, pure over per-run rb_refresh flags: off passes every run bulk) and WbUrgentCache
(the last urgent value kept for a stable small-result set: a hit only for the same range at the same note sequence,
anything else re-scanned).

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
    part = block(text, 'inline void wb_partition_runs(')
    cache = block(text, 'struct WbUrgentCache {') + ';'
    fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>
struct VkBufferCopy { uint64_t srcOffset, dstOffset, size; };
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
''' + enum + '\n' + tier + '\n' + gated + '\n' + part + '\n' + cache + r'''
static uint64_t total(const std::vector<VkBufferCopy>& rs) {
    uint64_t n = 0;
    for (const VkBufferCopy& r : rs) n += r.size;
    return n;
}
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
    // (the end's split, pure over per-run rb_refresh flags: off passes every run bulk, as before; on, small, rb
    // and odd runs go urgent whole, big aligned ones bulk. Order kept within each tier)
    {
        std::vector<VkBufferCopy> runs = {{0, 0x0, 4096}, {0x10000, 0x10000, 1u << 20}, {0x200000, 0x200000, 1u << 20},
                                          {0x400003, 0x400003, 1u << 20}};
        std::vector<char> rb = {0, 0, 1, 0};
        std::vector<VkBufferCopy> urgent, bulk;
        wb_partition_runs(runs, rb, true, urgent, bulk);
        CHECK(urgent.size() == 3 && bulk.size() == 1);
        CHECK(urgent[0].dstOffset == 0x0 && urgent[1].dstOffset == 0x200000 && urgent[2].dstOffset == 0x400003);
        CHECK(bulk[0].dstOffset == 0x10000);
        CHECK(total(urgent) == 4096 + 2 * (1u << 20));
    }
    {
        std::vector<VkBufferCopy> runs = {{0, 0x0, 4096}, {0x10000, 0x10000, 1u << 20}};
        std::vector<char> rb = {0, 1};
        std::vector<VkBufferCopy> urgent, bulk;
        wb_partition_runs(runs, rb, false, urgent, bulk);   // (kill switch: everything bulk)
        CHECK(urgent.empty() && bulk.size() == 2);
    }
    {   // (a short rb flag list: missing flags read as not-readback; empty runs split empty)
        std::vector<VkBufferCopy> runs = {{0, 0x0, 1u << 20}};
        std::vector<VkBufferCopy> urgent, bulk;
        wb_partition_runs(runs, {}, true, urgent, bulk);
        CHECK(urgent.empty() && bulk.size() == 1);
        runs.clear();
        urgent.clear(), bulk.clear();
        wb_partition_runs(runs, {1}, true, urgent, bulk);
        CHECK(urgent.empty() && bulk.empty());
    }
    std::puts("PASS: readback/small/odd runs are URGENT, big aligned ones BULK; off forces BULK; "
        "the partition keeps every run on its tier, in order; "
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
