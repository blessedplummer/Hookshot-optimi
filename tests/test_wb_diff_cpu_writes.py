"""The write-back diff's shadows hear of every CPU write of guest memory (gpu/vk/vk_memory.cpp, "the write-back's diff").

A shadow page the diff knows must equal guest memory: a CPU write it never heard of left the CPU's value where the whole copy
wrote the GPU's, whenever the GPU's result equalled the shadow's old word (PR 1109's review: an in-stream clear of a counter the
game's conditions read, counted to last frame's value again, stayed cleared). These checks read the sources:

- every host->gpu_write in gpu/vk is guest_cpu_write's own, or marked "shadows hear of" with a guest_cpu_wrote after it
- guest_cpu_write writes and reports; guest_cpu_wrote reports into the replay's list, which shadows_forget_written reads
- memory_collect_written reports every page it collects (the only report of the game's writes with RX_VK_CAPTURE=0)
- the in-stream clear (NOP_CLEAR_BUF) and the report words write through guest_cpu_write
- a written binding given the guest buffer of a mirrored pool forgets its pages (shadow_forget)

Dirty-origin, Phase 1 (extends #1629's RX_VK_WB_MAX_KB gate): GPU writes stamp gpu_gen (memory_stamp_gpu,
rb_hazard_locked), never page_gen; mirror_sync skips GPU-dirty-only pages; large both-dirty bindings still queue;
the capture prunes GPU-only tex/small-pool ranges; the end culls to unknown-or-GPU-dirty-or-pending pages. Each
behavior has a check below reading the source.

tests/test_wb_diff_stale_shadow.py runs the sequence itself.

python -I -B tests/test_wb_diff_cpu_writes.py
"""
from pathlib import Path
import re
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


class ShadowReports(unittest.TestCase):
    def setUp(self):
        self.memory = (VK / "vk_memory.cpp").read_text(encoding="utf-8")
        self.replay = (VK / "vk_replay.cpp").read_text(encoding="utf-8")

    def test_every_raw_guest_write_is_reported(self):
        wrapper = body(self.memory, "int guest_cpu_write(")
        for path in sorted(VK.glob("*.cpp")):
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"host->gpu_write\(", text):
                line_start = text.rfind("\n", 0, m.start()) + 1
                line = text[line_start:text.find("\n", m.start())]
                if path.name == "vk_memory.cpp" and wrapper.find(line.strip()) >= 0:
                    continue
                where = "%s:%d" % (path.name, text.count("\n", 0, m.start()) + 1)
                self.assertIn("shadows hear of", line, where + ": a CPU write of guest memory the write-back diff's shadows "
                              "never hear of; write it through guest_cpu_write")
                self.assertIn("guest_cpu_wrote(", text[m.start():m.start() + 4000], where + ": marked, but no guest_cpu_wrote after it")

    def test_the_wrapper_writes_and_reports(self):
        wrapper = body(self.memory, "int guest_cpu_write(")
        self.assertIn("host->gpu_write(", wrapper)
        self.assertIn("guest_cpu_wrote(", wrapper)
        reports = body(self.memory, "void guest_cpu_wrote(")
        self.assertIn("shadow_written_by_replay", reports)
        self.assertIn("host->gpu_guest(", reports)   # (by GPU address: each page's guest page)
        forget = body(self.memory, "void shadows_forget_written(")
        self.assertIn("shadow_written_by_replay", forget)
        self.assertIn("shadow_written_pages", forget)
        self.assertIn("shadow_written_collected_list", forget)

    def test_the_collection_reports_every_page(self):
        collect = body(self.memory, "void memory_collect_written(")
        self.assertRegex(collect, r"shadow_written_collected\(pages, n\)")

    def test_a_written_binding_past_the_mirror_forgets_its_pages(self):
        bind = body(self.memory, "BufferRef gpu_buffer_bind(uint64_t gpu, uint64_t size, bool written) {")
        direct = bind[bind.index("p->mstate == Pool::MIRROR_REFUSED) {"):]
        direct = direct[:direct.index("return remember(")]
        self.assertIn("if (written) shadow_forget(*p, off,", direct)   # (the GPU writes guest memory itself there)

    def test_the_replays_own_writes_go_through_the_wrapper(self):
        clear = body(self.replay, "case NOP_CLEAR_BUF:")
        self.assertIn("guest_cpu_write(c0.dst, words, ", clear)   # (the in-stream clear of the conditions' counters)
        self.assertNotIn("host->gpu_write(", clear)
        self.assertRegex(self.replay, r"guest_cpu_write\(c\.addr, rec, sizeof rec\)")   # (the report words)

    def test_gpu_writes_stamp_gpu_gen_not_page_gen(self):
        stamp = body(self.memory, "void memory_stamp_gpu(uint64_t guest, uint64_t size) {")
        self.assertIn("if (dirty_origin_on()) stamp_gpu_range(guest, size);", stamp)   # (on: gpu_gen only, never page_gen)
        self.assertIn("else memory_stamp(guest, size);", stamp)   # (off: page_gen for all writes, as before)
        hazard = body(self.memory, "void rb_hazard_locked(uint64_t lo, uint64_t hi) {")
        self.assertIn("stamp_gpu_range(lo, hi - lo)", hazard)   # (every range bound for GPU write, same submission)

    def test_mirror_sync_skips_gpu_dirty_only_pages(self):
        sync = body(self.memory, "void mirror_sync(Pool& p) {")
        self.assertIn("dirty_origin_of(", sync)   # (re-keyed on the page's origin, not page_gen alone)
        self.assertIn("st_sync_gpu_only", sync)   # (GPU-dirty-only skipped: the mirror holds the truth)
        self.assertIn(", 0, true}", sync)   # (skipped pages marked GPU-origin in CapEntry)
        self.assertIn("!e.gpu && e.gen && e.gen == pg", sync)   # (GPU-origin marks hold no copy)
        read = body(self.memory, "bool memory_read_as_submitted(uint64_t gpu, void* dst, uint64_t n) {")
        self.assertIn("e.gpu ||", read)
        entry = body(self.memory, "struct CapEntry {")
        self.assertIn("bool gpu", entry)

    def test_large_both_dirty_bindings_still_queue(self):
        bind = body(self.memory, "BufferRef gpu_buffer_bind(uint64_t gpu, uint64_t size, bool written) {")
        self.assertIn("RX_VK_WB_MAX_KB", bind)   # (#1629's coarse size gate stays)
        self.assertIn("memory_writeback_value(", bind)   # (GPU results the CPU read back before: both-dirty)
        self.assertIn("memory_written_since(", bind)   # (a page the CPU wrote since the pool's sync: both-dirty)
        self.assertIn("st_wb_large_kept", bind)   # (still queued; the end narrows them instead of copying them whole)
        self.assertIn("rb_hazard_locked(", bind)   # (the hazard is noted whether the binding queues or not)

    def test_capture_prunes_gpu_only_scan_ranges(self):
        make = body(self.memory, "std::unique_ptr<Capture> capture_make() {")
        self.assertIn("mirrored_full", make)
        self.assertIn("mirrored_cap", make)   # (mirrored pools always stay: their captures fix the flashes)
        self.assertIn("memory_written_since(", make)   # (tex/small-pool ranges: only those with any CPU-dirty page)
        self.assertIn("st_cap_pruned", make)
        self.assertIn("prune_seen", make)   # (the lagging base: hot ranges stay scanned; odd submits scan everything)
        take = body(self.memory, "bool take_capture(std::unique_ptr<Capture> c, uint64_t value, uint64_t completed) {")
        self.assertIn("CapEntry{g, c->slots[i], false}", take)   # (a CPU copy clears any GPU-origin mark)

    def test_end_culls_to_unknown_or_gpu_dirty_or_pending(self):
        end = body(self.memory, "void memory_end_submission(VkCommandBuffer cmd) {")
        self.assertIn("wbd_keep_page(", end)   # (unknown-to-shadow pages, GPU-dirty ones, pending ones: kept)
        self.assertIn("wb_pending_pages", end)   # (a page with a write-back still in flight is never culled)
        self.assertIn("binary_search(wb_pending_pages", end)
        self.assertIn("p->gpu_synced", end)   # (GPU-dirty against the pool's own baseline)
        self.assertIn("p->synced_gen = gen_now, p->gpu_synced = gpu_now", end)   # (both baselines advance together)

    def test_kill_switch_covers_all_split_sites(self):
        on = body(self.memory, "bool dirty_origin_on() {")
        self.assertIn('getenv("RX_VK_DIRTY_ORIGIN")', on)
        self.assertIn("atoi(e) != 0", on)   # (default on: off only when set to 0)
        stamp = body(self.memory, "void memory_stamp_gpu(uint64_t guest, uint64_t size) {")
        self.assertIn("else memory_stamp(guest, size)", stamp)   # (off: page_gen for all writes, as before)
        for sig in ("void rb_hazard_locked(uint64_t lo, uint64_t hi) {",
                    "void mirror_sync(Pool& p) {",
                    "BufferRef gpu_buffer_bind(uint64_t gpu, uint64_t size, bool written) {",
                    "std::unique_ptr<Capture> capture_make() {",
                    "void memory_end_submission(VkCommandBuffer cmd) {"):
            self.assertIn("dirty_origin_on()", body(self.memory, sig), sig)

    def test_bare_table_writes_are_stamped_through_the_hazard(self):
        prot = body(self.memory, "void memory_protect_gpu(uint64_t guest, uint64_t size) {")
        self.assertNotIn("stamp_gpu_range(", prot)   # (no separate stamp: the hazard range stamps gpu_gen)
        self.assertIn("rb_hazard_locked", prot)   # (the covering path is documented, not assumed)
        draw = (VK / "vk_draw.cpp").read_text(encoding="utf-8")
        self.assertIn("memory_protect_gpu(guest, n)", draw)
        self.assertIn("memory_note_gpu_write(base, n)", draw)   # (the same bind's range, hazard-noted unconditionally)


if __name__ == "__main__":
    unittest.main()
