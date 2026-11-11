"""Phase 2, MiMo: sparse mirrors, GPU-aware shadow inherit, biased eviction (gpu/vk/vk_memory.cpp).

Pools over threshold (a 4K surface, 32 MB) skip the full n-byte mirror_make copy when their
footprints already cover (almost) everything: CPU-dirty live runs plus a bounded tail fill
from guest memory, then retired-mirror inherit and the capture overlay overwrite with newer
truth. A fresh READ pool (or big growth tail / read-mostly pool) falls back to the full copy
so no byte of a new mirror is ever uninitialized. Shadows inherit across reallocs like
retired-mirror inherit, with the GPU-dirty term included: a page the GPU wrote post-sync keeps
its truth in the retired mirror, never in the shadow. Eviction is largest-first under pressure
while protecting small constant pools. RX_VK_DIRTY_ORIGIN=0 disables all of it.

A per-tile write-back cull was tried and DELETED as a proven no-op (review 2026-11-11): marks
set at the binds that queue the runs are a superset of the runs, so the cull could only drop
what the Phase-1 wbd_keep_page cull already dropped. Sourcing it to bite needs GPU-observed
dirt, which does not exist on the CPU. The honest annotation sits at the cull site.

python -I -B tests/test_wb_phase2_tiles.py [--cxx g++]
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

RUN_ALL_ARGS = ["--cxx", "g++"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"


def block(text, marker):
    start = text.index(marker)
    end = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def check_source():
    mem = (VK / "vk_memory.cpp").read_text(encoding="utf-8")
    # (no tile machinery left: the cull was a proven no-op and is deleted, not stubbed)
    for gone in ("tile_bits", "tile_buf", "tile_mark_range", "tile_keep_page", "tile_clear_all",
                 "tile_count_for", "st_wb_tile_culled"):
        if gone in mem:
            raise SystemExit("tile-mask machinery should be deleted: " + gone)
    if "no per-tile cull here, by decision" not in mem:
        raise SystemExit("the honest no-cull annotation is missing at the cull site")
    # (sparse decision helpers)
    for needle in ("constexpr uint64_t kSparseMirrorThreshold",
                   "inline bool mirror_sparse_for(",
                   "constexpr uint64_t kSparseTailMax",
                   "inline std::vector<std::pair<uint64_t, uint64_t>> runs_normalize(",
                   "inline std::vector<std::pair<uint64_t, uint64_t>> runs_uncovered(",
                   "inline uint64_t runs_bytes(",
                   "inline bool sparse_tail_ok("):
        if needle not in mem:
            raise SystemExit("sparse helper missing: " + needle)
    # (sparse: coverage preview, bounded tail, fallback — never uninitialized)
    make = block(mem, "bool mirror_make(Pool& pool) {")
    for needle in ("runs_uncovered(0, n, runs_normalize(", "sparse_tail_ok(uncovered_bytes)",
                   "sparse fallback", "tail_fill", "every byte of",
                   "the new mirror is written before the page table points at"):
        if needle not in make:
            raise SystemExit("sparse make lacks the coverage decision: " + needle)
    if "never read before a GPU write" in make:
        raise SystemExit("the backwards coverage assumption is back")
    # (shadow inherit is GPU-aware and the mirror coupling is explicit)
    for needle in ("uint32_t gpu_gen", "uint32_t gpu_synced",
                   "gpu_gen <= gpu_synced",
                   "truth lives in the retired MIRROR",
                   "shadow_inherit_keep(page_gen(page), synced,"):
        if needle not in mem:
            raise SystemExit("shadow inherit lost its GPU term: " + needle)
    # (a second dst overlapping one retired shadow is impossible: live pools are disjoint)
    if "at most one of them overlaps" not in mem:
        raise SystemExit("the single-dst inherit invariant is undocumented")
    # (eviction: helper-driven, no O(n^2) rescan, deterministic ties)
    give = block(mem, "void mirrors_give_back(uint64_t want_bytes) {")
    if "std::find(biased" in give or "std::find(lru" in give:
        raise SystemExit("eviction still rescans (O(n^2))")
    if "sort_evict_candidates(order, pressured)" not in give:
        raise SystemExit("eviction does not use the largest-first/stalest-first helper")
    if "resolve(true)" not in give or "resolve(false)" not in give:
        raise SystemExit("eviction does not order largest-first under pressure, stalest-first idle")
    if "evict_protect_small(" not in give:
        raise SystemExit("eviction does not protect small constant pools")
    # (kill switch covers every Phase-2 site)
    for sig in ("void mirror_sync(Pool& p) {",
                "BufferRef gpu_buffer_bind(uint64_t gpu, uint64_t size, bool written) {",
                "bool mirror_make(Pool& pool) {",
                "bool shadow_make(Pool& p) {",
                "void shadow_inherit_copy(Pool& dst, VkCommandBuffer cmd) {",
                "void mirrors_give_back(uint64_t want_bytes) {",
                "void memory_end_submission(VkCommandBuffer cmd) {"):
        if "dirty_origin_on()" not in block(mem, sig):
            raise SystemExit("kill switch missing in " + sig)
    if "std::unique_ptr<Capture> capture_make() {" in mem and \
            "dirty_origin_on()" not in block(mem, "std::unique_ptr<Capture> capture_make() {"):
        raise SystemExit("kill switch missing in capture_make")
    # (Phase-1 contracts still hold)
    sync = block(mem, "void mirror_sync(Pool& p) {")
    for needle in ("dirty_origin_of(", "st_sync_gpu_only", ", 0, true}",
                   "!e.gpu && e.gen && e.gen == pg", "if (origin == 2) {"):
        if needle not in sync:
            raise SystemExit("mirror_sync lost Phase-1 GPU-dirty-only skip: " + needle)
    if sync.index("append_capture_copy(") > sync.index("st_sync_live_pages++"):
        raise SystemExit("both-dirty order flipped: capture must stay before live (refresh-first)")
    end = block(mem, "void memory_end_submission(VkCommandBuffer cmd) {")
    if "wbd_keep_page(" not in end or "p->gpu_synced" not in end or \
            "p->synced_gen = gen_now, p->gpu_synced = gpu_now" not in end:
        raise SystemExit("the end lost Phase-1 cull/baseline discipline")


def run_fixture(cxx, source):
    text = source.read_text(encoding="utf-8")
    start = text.index("// ---- Phase 2: sparse mirrors, shadow inherit, biased eviction ----")
    end = text.index("} // namespace", text.index("inline bool evict_protect_small("))
    helpers = text[start:end]
    fixture = r'''
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <utility>
#include <vector>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
namespace {
''' + helpers + r'''
} // namespace
// (model of the sparse decision: covered = dirty-ever + capture overlay; inherit only adds coverage)
static bool decide(bool& sparse, uint64_t& tail, uint64_t pool_bytes,
                   const std::vector<std::pair<uint64_t, uint64_t>>& cover) {
    sparse = mirror_sparse_for(pool_bytes);
    tail = 0;
    if (!sparse) return true;   // (at/below threshold: full copy, as before)
    auto uncovered = runs_uncovered(0, pool_bytes, runs_normalize(cover));
    uint64_t ub = runs_bytes(uncovered);
    if (!sparse_tail_ok(ub)) { sparse = false; return true; }   // (fresh READ / big tail: fallback)
    tail = ub;
    return true;
}
int main() {
    // (threshold: pools over 4K surfaces (~32 MB), not at it)
    CHECK(!mirror_sparse_for(32u << 20) && mirror_sparse_for((32u << 20) + 1));
    CHECK(mirror_sparse_for(64u << 20) && !mirror_sparse_for(1u << 20));
    // (runs_normalize: sort, merge, drop empties/inverted)
    {
        std::vector<std::pair<uint64_t, uint64_t>> v = {{30, 10}, {0, 0}, {20, 30}, {0, 10}, {10, 20}, {100, 90}};
        auto m = runs_normalize(v);
        CHECK(m.size() == 1 && m[0].first == 0 && m[0].second == 30);
    }
    // (runs_uncovered: complement within [base, base+size), covered clipped)
    {
        std::vector<std::pair<uint64_t, uint64_t>> cover = {{0, 100}, {200, 300}, {10000, 20000}};
        auto u = runs_uncovered(0, 300, runs_normalize(cover));
        CHECK(u.size() == 1 && u[0].first == 100 && u[0].second == 200);
        CHECK(runs_uncovered(0, 50, {}).size() == 1);   // (nothing covered: everything uncovered)
        CHECK(runs_uncovered(0, 100, {{{0, 100}}}).empty());   // (fully covered: nothing to fill)
    }
    // (fresh READ pool: 64 MB, nothing ever written, nothing captured -> fallback to the full copy)
    {
        bool sparse = true;
        uint64_t tail = 0;
        CHECK(decide(sparse, tail, 64u << 20, {}));
        CHECK(!sparse);   // (uncovered == whole pool > tail max: full copy, never a hole)
    }
    // (covered write-mostly pool: dirty + capture cover all but 1 MB -> sparse with a bounded tail)
    {
        const uint64_t n = 64u << 20;
        std::vector<std::pair<uint64_t, uint64_t>> cover = {{0, n - (1u << 20)}, {n - (1u << 20), n}};
        bool sparse = true;
        uint64_t tail = 0;
        CHECK(decide(sparse, tail, n, cover));
        CHECK(sparse && tail == 0);   // (fully covered: no tail at all)
    }
    // (realloc growth tail: 48 MB pool, old 32 MB dirty, new 16 MB tail untouched -> fallback, not a hole)
    {
        const uint64_t n = 48u << 20;
        std::vector<std::pair<uint64_t, uint64_t>> cover = {{0, 32u << 20}};
        bool sparse = true;
        uint64_t tail = 0;
        CHECK(decide(sparse, tail, n, cover));
        CHECK(!sparse);   // (16 MB tail > 8 MB max: full copy covers the growth)
    }
    // (small growth tail: 40 MB pool, 32 MB dirty, 8 MB tail -> sparse with an 8 MB bounded tail fill)
    {
        const uint64_t n = 40u << 20;
        std::vector<std::pair<uint64_t, uint64_t>> cover = {{0, 32u << 20}};
        bool sparse = true;
        uint64_t tail = 0;
        CHECK(decide(sparse, tail, n, cover));
        CHECK(sparse && tail == (8u << 20) && sparse_tail_ok(tail));
    }
    // (shadow inherit: CPU-clean AND GPU-clean inherits; either side dirty does not)
    CHECK(shadow_inherit_keep(4, 4, false, 7, 9));   // (both clean)
    CHECK(shadow_inherit_keep(0, 9, false, 0, 9));
    CHECK(!shadow_inherit_keep(5, 4, false, 0, 9));   // (CPU wrote post-sync)
    CHECK(!shadow_inherit_keep(4, 4, false, 10, 9));  // (GPU wrote post-sync: truth lives in the retired mirror)
    CHECK(!shadow_inherit_keep(5, 4, false, 10, 9));  // (both wrote)
    CHECK(shadow_inherit_keep(9, 4, true, 3, 9));   // (bare-entry page the mirror kept, GPU clean: inherited)
    CHECK(!shadow_inherit_keep(9, 4, true, 10, 9));  // (bare-entry page but GPU-dirty: mirror-inherit owns it)
    // (eviction: largest-first under pressure, stalest-first idle, deterministic ties, small protected)
    CHECK(evict_protect_small((1u << 20) - 1) && !evict_protect_small(1u << 20));
    {
        std::vector<EvictCand> v = {{1, 1u << 20, 10, 9}, {2, 64u << 20, 50, 1}, {3, 8u << 20, 5, 7}};
        sort_evict_candidates(v, true);
        CHECK(v[0].bytes == (64u << 20) && v[1].bytes == (8u << 20) && v[2].bytes == (1u << 20));
        sort_evict_candidates(v, false);
        CHECK(v[0].synced_epoch == 5 && v[1].synced_epoch == 10 && v[2].synced_epoch == 50);
    }
    {   // (ties broken deterministically: same size -> stalest first -> lowest last_value)
        std::vector<EvictCand> v = {{1, 8u << 20, 10, 5}, {2, 8u << 20, 10, 3}, {3, 8u << 20, 5, 9}};
        sort_evict_candidates(v, true);
        CHECK(v[0].lo == 3 && v[1].lo == 2 && v[2].lo == 1);
    }
    std::puts("PASS: sparse falls back on fresh READ, stays sparse when covered, tails bounded; "
        "shadow inherit needs CPU- and GPU-clean; eviction largest-first with deterministic ties");
}
'''
    with tempfile.TemporaryDirectory(prefix="wb-phase2-tiles-") as tmp:
        src = Path(tmp) / "tiles.cpp"
        exe = Path(tmp) / "tiles.exe"
        src.write_text(fixture, encoding="utf-8")
        subprocess.run([cxx, "-std=c++20", "-O1", "-Wall", "-Wextra", "-Werror", str(src), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="g++")
    parser.add_argument("--source", type=Path, default=ROOT / "gpu/vk/vk_memory.cpp")
    args = parser.parse_args()
    check_source()
    run_fixture(args.cxx, args.source)


if __name__ == "__main__":
    main()
