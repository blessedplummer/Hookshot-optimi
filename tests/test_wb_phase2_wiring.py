"""Phase 2 wiring pins (gpu/vk/vk_memory.cpp, vk_replay.cpp): the review's call sites, read from the sources.

Predicates alone are not coverage: these checks pin the wiring itself, and fail if it is removed or bypassed —

- the end's partition passes dirty_origin_on() (never a hardcoded true), and no tile intersect remains anywhere
- the bulk budget's sweep runs every end, observes carry the CPU-dirt zero, and retire/eviction invalidate histories
- the refresh/publish exclusion calls exist, and invalidate tiers urgent-first with the full-table fallback

python -I -B tests/test_wb_phase2_wiring.py
"""
from pathlib import Path
import unittest

VK = Path(__file__).resolve().parents[1] / "gpu" / "vk"


def body(text, signature):
    """The braces' contents of the first function or block opening after `signature`"""
    start = text.index(signature)
    i = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[i] == "{") - (text[i] == "}")
        i += 1
    return text[start:i]


class Phase2Wiring(unittest.TestCase):
    def setUp(self):
        self.memory = (VK / "vk_memory.cpp").read_text(encoding="utf-8")
        self.replay = (VK / "vk_replay.cpp").read_text(encoding="utf-8")

    def test_partition_threading_kill_switch(self):
        end = body(self.memory, "void memory_end_submission(VkCommandBuffer cmd) {")
        self.assertIn("wb_partition_runs(runs, rb, dirty_origin_on(), urgent, bulk)", end)   # (never hardcoded true)
        self.assertNotIn("wb_tier_of_gated(r.size, rb, odd_run, true)", end)

    def test_no_tile_prefilter_remains(self):
        end = body(self.memory, "void memory_end_submission(VkCommandBuffer cmd) {")
        self.assertNotIn("wbd_tile", end)   # (deleted: a bind-sourced mask culls nothing by construction)
        hazard = body(self.memory, "void rb_hazard_locked(uint64_t lo, uint64_t hi) {")
        self.assertNotIn("wbd_tile_mark_range", hazard)
        begin = body(self.memory, "void memory_begin_submission() {")
        self.assertNotIn("wbd_dirty_tiles", begin)

    def test_bulk_sweep_and_observe(self):
        end = body(self.memory, "void memory_end_submission(VkCommandBuffer cmd) {")
        self.assertIn("wbd_bulk_submit();", end)   # (every end: bump the epoch, sweep pools idle a whole submit)
        self.assertIn("memory_written_since(p->lo, psize, p->synced_gen)", end)   # (CPU dirt re-includes the pool)
        self.assertIn("wbd_pool_observe(p->lo, cpu_dirty ? 0 : run_bytes, psize)", end)

    def test_bulk_retire_invalidation(self):
        retire = body(self.memory, "void retire_pool(std::map<uint64_t, Pool>::iterator it) {")
        self.assertIn("wbd_pool_retired(old->lo);", retire)   # (a new pool at this address starts fresh)
        evict = body(self.memory, "void imports_give_back() {")
        self.assertIn("wbd_pool_retired(it->first);", evict)

    def test_bulk_exclusion_wiring(self):
        refresh = body(self.memory, "void refresh_pool_ranges_locked() {")
        self.assertIn("wbd_pool_excluded_from_capture(", refresh)   # (bulk pools leave the capture's scans)
        publish = body(self.memory, "void publish_cap_ranges() {")
        self.assertIn("wbd_pool_excluded_from_capture(", publish)

    def test_urgent_fallback_wiring(self):
        urgent = body(self.memory, "uint64_t memory_writeback_urgent_value(")
        self.assertIn("if (!dirty_origin_on()) return memory_writeback_value(", urgent)   # (off: today's wait)
        inv = body(self.replay, "void invalidate(uint64_t gpu, uint64_t size) {")
        self.assertIn("wb_invalidate_tier(ever, false)", inv)   # (urgent first)
        self.assertIn("wb_invalidate_tier(false, true)", inv)   # (then the one-time full fallback)
        self.assertIn("memory_writeback_value(host->gpu_guest(gpu), size, &ever)", inv)   # (the fallback read)
        self.assertIn("tier == WbWaitTier::Urgent", inv)   # (the re-read follows the tier that waited)
        self.assertIn("memory_note_readback(", inv)   # (the fallback notes the readback: next write-back goes urgent)


if __name__ == "__main__":
    unittest.main()
