"""Dirty-origin split end to end across two submissions (gpu/vk/vk_memory.cpp, Phase 1).

A compiled fixture driving the production pieces: stamp the CPU/GPU tables, classify origins the way mirror_sync
does (origin 1 and 3 take the live path, 0 and 2 are skipped), cull a whole queued run the way memory_end_submission
does (real wbd_keep_page with real wb_pages pending), advance both baselines the way the end does, and apply the
capture's scan-prune rule (even submits drop GPU-only tex/small-pool ranges against a lagging base, odd submits scan
everything so a pruned range recovers next submit).

CPU-compiled, no game/GPU. Written, not run, with the rest of Phase 1.

python -I -B tests/test_wb_origin_split.py [--cxx g++]
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
    # (the page tables and both stamps, opener through the generation block's close: the span must stay balanced,
    # or the fixture's trailing namespace close misparses everything after it)
    start = text.index('// ---- page write generations ----')
    end = text.index('} // namespace', text.index('inline uint8_t dirty_origin_of(')) + len('} // namespace')
    tables = text[start:end]
    if tables.count('namespace {') != tables.count('} // namespace') or not tables.rstrip().endswith('} // namespace'):
        raise SystemExit('page-table span is not a balanced namespace block; cut markers moved')
    since = block(text, 'bool memory_written_since(')
    keep = block(text, 'inline bool wbd_keep_page(')
    pages = block(text, 'struct WbPages {') + ';'   # (block() stops at the closing brace: terminate the struct)
    fixture = r'''
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <vector>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
static void logf(const char*, ...) {}   // (dirty_origin_on logs once; the tables themselves never log)
''' + tables + '\n' + since + '\n' + keep + '\n' + pages + r'''
// (the submission pipeline, modelled the way the source drives it: stamp tables at record, classify origins at
// sync, cull whole queued runs at the end, advance both baselines; the scan prune keeps a lagging base and scans
// odd submits whole. The prune decision below mirrors capture_make; the stickiness it relies on — the host watch
// keeps what a scan skips, the collection re-marks capture pages — lives in host/rx_backend.c and is asserted by
// tests/test_wb_diff_cpu_writes.py, not here)
static uint32_t prune_seen = 1;   // (gen_now at the scan before last: lags the takes by design)
static bool scan_keep(uint64_t lo, uint64_t len, bool in_cap, bool even) {
    if (!even) return true;   // (odd submits scan everything: a pruned range recovers next submit)
    if (in_cap) return true;   // (mirrored pools always stay: their captures fix the flashes)
    return memory_written_since(lo, len, prune_seen);
}
int main() {
    CHECK(dirty_origin_on());   // (the split reads on by default; every expectation below classifies with it)
    CHECK(pages_collected == 0);   // (driven by direct stamps, no collection path ran)
    uint32_t cpu_synced = 1, gpu_synced = 1;   // (a pool synced at generation 1, both sides)
    // submission 1: the CPU writes pages 0,1; the GPU writes pages 1,2
    const uint32_t g1 = ++gen_now, h1 = ++gpu_now;
    stamp_page(0, g1); stamp_page(1, g1);
    stamp_gpu_range(1u << 12, 2u << 12);
    CHECK(g1 == 2 && h1 == 2);
    // (sync: origin 3 joins origin 1 on the live path; 0 and 2 are skipped, the mirror holding the truth or nothing)
    CHECK(dirty_origin_of(page_gen(0), gpu_page_gen(0), cpu_synced, gpu_synced) == 1);
    CHECK(dirty_origin_of(page_gen(1), gpu_page_gen(1), cpu_synced, gpu_synced) == 3);
    CHECK(dirty_origin_of(page_gen(2), gpu_page_gen(2), cpu_synced, gpu_synced) == 2);
    CHECK(dirty_origin_of(page_gen(3), gpu_page_gen(3), cpu_synced, gpu_synced) == 0);
    cpu_synced = gen_now, gpu_synced = gpu_now;   // (the end advances both baselines, as memory_end_submission does)
    // submission 2: the CPU writes page 2; the GPU writes pages 2,3
    const uint32_t g2 = ++gen_now, h2 = ++gpu_now;
    stamp_page(2, g2);
    stamp_gpu_range(2u << 12, 2u << 12);
    CHECK(g2 == 3 && h2 == 3);
    CHECK(dirty_origin_of(page_gen(0), gpu_page_gen(0), cpu_synced, gpu_synced) == 0);
    CHECK(dirty_origin_of(page_gen(1), gpu_page_gen(1), cpu_synced, gpu_synced) == 0);
    CHECK(dirty_origin_of(page_gen(2), gpu_page_gen(2), cpu_synced, gpu_synced) == 3);
    CHECK(dirty_origin_of(page_gen(3), gpu_page_gen(3), cpu_synced, gpu_synced) == 2);
    // (end cull of a whole queued run over pages 0..3, every page known to the shadow)
    WbPages wb;
    wb.set(0, 5);   // (written back by a submission still in flight: value 5 past done 4)
    const uint64_t done = 4;
    bool kept[4] = {};
    for (uint64_t pi = 0; pi < 4; pi++)
        kept[pi] = wbd_keep_page(true, gpu_page_gen(pi) > gpu_synced, wb.pending(pi, done));
    CHECK(kept[0] && !kept[1] && kept[2] && kept[3]);   // (pending kept, clean culled, GPU-dirty kept)
    // (scan prune: an idle tex-only range is dropped on even submits and recovered on odd ones)
    const uint64_t TB = 0x100000ull, TN = 0x20000ull, HB = 0x200000ull, HN = 0x20000ull;
    CHECK(!scan_keep(TB, TN, false, true));   // (idle: pruned on even)
    CHECK(scan_keep(TB, TN, false, false));   // (recovered on odd: its marks survive in the host watch)
    CHECK(scan_keep(TB, TN, true, true));   // (mirrored pools never pruned)
    stamp_page((HB >> 12) + 5, ++gen_now);   // (a hot range: stamped again, as each submit's take stamps it)
    CHECK(scan_keep(HB, HN, false, true));   // (kept: take stamps are newer than the lagging base)
    prune_seen = gen_now;   // (the prune records its base)
    CHECK(!scan_keep(TB, TN, false, true));   // (idle again: still pruned with the newer base)
    stamp_page((HB >> 12) + 5, ++gen_now);   // (the next submit's take stamps it again)
    CHECK(scan_keep(HB, HN, false, true));   // (still kept: the base lags the takes by design)
    std::puts("PASS: origins classify CPU/GPU/both/clean across two submissions; the cull keeps pending and "
        "GPU-dirty pages; the prune drops idle ranges on even submits and recovers them on odd ones");
}
'''
    with tempfile.TemporaryDirectory(prefix='wb-origin-split-') as tmp:
        src = Path(tmp) / 'origin.cpp'
        exe = Path(tmp) / 'origin.exe'
        src.write_text(fixture, encoding='utf-8')
        subprocess.run([args.cxx, '-std=c++20', '-O1', '-Wall', '-Wextra', '-Werror', str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
