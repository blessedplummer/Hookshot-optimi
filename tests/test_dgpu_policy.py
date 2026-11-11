"""The dGPU PC-like policy's gate, classification and sparse write-back clip (gpu/vk/vk_memory.cpp).

RX_VK_DGPU turns on three things that answer each other: static pools direct-mapped in guest memory
(no mirror, no sync, no write-back), GPU-written pools writing back only the pages the CPU is seen
reading (no diff pass), and one pool at a time trialed in the other state. Unset, every helper
returns false and the stock paths stand - this test is that claim, checked at each site the policy
touches, plus the two pure pieces compiled and driven: the classification's decision and the clip.

python -I -B tests/test_dgpu_policy.py [--cxx g++]
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
    replay = (VK / "vk_replay.cpp").read_text(encoding="utf-8")
    # (the gate: read once, default off, says so in the log - unset is stock, not "should be")
    gate = block(mem, "bool dgpu_on() {")
    for needle in ('getenv("RX_VK_DGPU")', "e ? atoi(e) != 0 : 0", 'logf("vk: dGPU PC-like policy'):
        if needle not in gate:
            raise SystemExit("the gate does not read RX_VK_DGPU and default off: " + needle)
    # (every site the policy changes is behind the gate)
    for sig in ("BufferRef gpu_buffer_bind(uint64_t gpu, uint64_t size, bool written) {",
                "void memory_begin_submission() {",
                "void memory_end_submission(VkCommandBuffer cmd) {",
                "void memory_stamp_gpu(uint64_t guest, uint64_t size) {",
                "void memory_note_readback(uint64_t guest, uint64_t size) {",
                "void dgpu_trial_run() {",
                "char dgpu_keep_verdict(uint64_t lo) {",
                "void dgpu_mark_wrote(uint64_t guest, uint64_t size) {"):
        if "dgpu_on()" not in block(mem, sig):
            raise SystemExit("the dGPU gate is missing in " + sig)
    # (the write-back queue gate: an unread pool queues nothing, and the clip runs where the diff used to)
    bind = block(mem, "BufferRef gpu_buffer_bind(uint64_t gpu, uint64_t size, bool written) {")
    if "p->dgpu_read_n" not in bind or "st_dgpu_wb_skipped" not in bind:
        raise SystemExit("the queue gate is missing: a written binding must check the CPU-read pages")
    end = block(mem, "void memory_end_submission(VkCommandBuffer cmd) {")
    for needle in ("dgpu_clip_reads(*p, runs)", "const bool diff = !dgpu_on() && wb_diff_on()", "st_dgpu_wb_copied"):
        if needle not in end:
            raise SystemExit("the sparse write-back is missing in memory_end_submission: " + needle)
    # (rule 2's promotion path: the classification decides, the stock mirror machinery applies it)
    if "dgpu_classify(" not in bind:
        raise SystemExit("gpu_buffer_bind does not classify through dgpu_classify")
    if "Pool::MIRROR_REFUSED" not in bind or "Pool::MIRROR_NONE" not in bind:
        raise SystemExit("the classification must land in the stock states (REFUSED direct, NONE to be made)")
    # (the readbacks learn the pages even before the first write-back covered them, or the exposure
    # meter would read zeros forever: the note runs ahead of the 'ever' gate only under the policy)
    if "if (!off && dgpu_on()) memory_note_readback(" not in replay:
        raise SystemExit("vk_replay does not note readbacks ahead of the write-back gate under the policy")
    # (the keep file lives where the game folder's other state lives: beside image.bin, vk_paths.h)
    keep = block(mem, "std::string dgpu_keep_path() {")
    if "game_module_path(" not in keep or "hookshot_dgpu_mirrors.txt" not in keep:
        raise SystemExit("the keep file must be built with game_module_path (the game folder's rule)")
    # (the keep file's line format: a verdict letter and the pool's hex start, Triforce's bare hex read as 'M')
    loader = block(mem, "std::unordered_map<uint64_t, char>& dgpu_keep() {")
    for needle in ("strtoull", "'M'", "'D'", "fgets"):
        if needle not in loader:
            raise SystemExit("the keep file loader lost: " + needle)
    adder = block(mem, "void dgpu_keep_add(uint64_t lo, char v) {")
    if 'fopen(dgpu_keep_path().c_str(), "a")' not in adder or "%c %llx" not in adder:
        raise SystemExit("verdicts must be appended in the keep file's line format")
    # (the trial's windows and gates are constants, not magic numbers buried in the steps)
    for const in ("kDtGpuLimitMs = 6.0", "kDtPromotePct = 0.03", "kDtDemoteSlackPct = 0.20", "kDtDemoteClear = 1.5"):
        if const not in mem:
            raise SystemExit("the trial's measured bounds are missing: " + const)


def run_fixture(cxx, source):
    text = source.read_text(encoding="utf-8")
    classify = text[text.index("int dgpu_classify("):]
    classify = classify[:classify.index('\n}') + 2]
    clip_start = text.index("void dgpu_clip_reads(")
    clip = text[clip_start:text.index("\n}\n", clip_start) + 3]
    fixture = r'''
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
// (the policy's gate, as the fixture drives the ON side; the OFF side is the source check above)
static bool dgpu_on() { return true; }
constexpr int kPageShift = 12;
struct VkBufferCopy { uint64_t srcOffset, dstOffset, size; };
// (the Pool fields dgpu_clip_reads reads: the same names and types as the real struct's)
struct Pool {
    uint64_t lo = 0, hi = 0;
    std::vector<uint64_t> dgpu_read;
    uint32_t dgpu_read_n = 0;
};
namespace {
''' + classify + clip + r'''
} // namespace
int main() {
    // (classification: static pools direct, GPU-written pools promoted, trials and verdicts held)
    // mstate: NONE 0, WANTED 1, ON 2, REFUSED 3 (Pool's own enum values)
    CHECK(dgpu_classify(0, false, false, char(0), false) == 1);   // (first bind of a never-written pool)
    CHECK(dgpu_classify(0, true, false, char(0), false) == 0);    // (first bind is a write: stock makes the mirror)
    CHECK(dgpu_classify(3, true, true, char(0), false) == 2);     // (direct pool the GPU wrote: promoted)
    CHECK(dgpu_classify(3, true, true, 'D', false) == 0);         // (a kept 'D' verdict: stays direct)
    CHECK(dgpu_classify(0, false, false, 'M', false) == 0);       // (a kept 'M' verdict: stock mirrors it)
    CHECK(dgpu_classify(0, false, false, char(0), true) == 0);    // (a trial holds the pool where the trial put it)
    CHECK(dgpu_classify(2, true, false, char(0), false) == 0);    // (already mirrored: nothing to do)
    CHECK(dgpu_classify(3, false, true, char(0), false) == 0);    // (static and direct: stays)
    CHECK(dgpu_classify(1, false, false, char(0), false) == 0);   // (WANTED: stock's own machinery, left alone)
    // (the clip: only the pages the CPU read go back, page-granular and re-merged, gaps never bridged)
    {
        Pool p;
        p.lo = 0x1000000000;
        p.hi = p.lo + (16u << 12) * 4096;   // (16 pages)
        p.dgpu_read.assign(1, 0);
        auto mark = [&](uint64_t page) { p.dgpu_read[page >> 6] |= 1ull << (page & 63); p.dgpu_read_n++; };
        mark(1), mark(2);                   // (pages 1-2: one merged run)
        mark(5);                            // (a gap, then page 5: a second run)
        p.dgpu_read_n = 3;
        std::vector<VkBufferCopy> runs = {{0, 0, 16u << 12}};   // (the whole pool queued)
        dgpu_clip_reads(p, runs);
        CHECK(runs.size() == 2);
        CHECK(runs[0].srcOffset == 4096 && runs[0].size == 2 * 4096);   // (1-2 merged)
        CHECK(runs[1].srcOffset == 5 * 4096 && runs[1].size == 4096);   // (5 alone: the gap stayed a gap)
        // (a run wholly inside unread pages: nothing to copy)
        std::vector<VkBufferCopy> none = {{3 * 4096, 3 * 4096, 4096}};
        dgpu_clip_reads(p, none);
        CHECK(none.empty());
        // (a run that starts and ends mid-page keeps its own bytes on the kept page: the copy is the
        // written range the queue carried, on the pages the CPU read, never a page more)
        std::vector<VkBufferCopy> odd = {{4096 + 10, 4096 + 10, 100}};
        dgpu_clip_reads(p, odd);
        CHECK(odd.size() == 1 && odd[0].srcOffset == 4096 + 10 && odd[0].size == 100);
        // (a run crossing an unread page into read ones: the unread part drops, the rest stays)
        std::vector<VkBufferCopy> cross = {{3000, 3000, 8000}};   // (page 0 unread, pages 1-2 read)
        dgpu_clip_reads(p, cross);
        CHECK(cross.size() == 1 && cross[0].srcOffset == 4096 && cross[0].size == 8000 - (4096 - 3000));
        // (no read pages at all: everything clears - the queue gate keeps these out anyway)
        Pool q;
        q.lo = p.lo, q.hi = p.hi;
        std::vector<VkBufferCopy> all = {{0, 0, 4096}};
        dgpu_clip_reads(q, all);
        CHECK(all.empty());
        // (two adjacent kept pages across a queued-run boundary merge back into one copy)
        std::vector<VkBufferCopy> two = {{4096, 4096, 4096}, {2 * 4096, 2 * 4096, 4096}};
        dgpu_clip_reads(p, two);
        CHECK(two.size() == 1 && two[0].srcOffset == 4096 && two[0].size == 2 * 4096);
    }
    std::puts("PASS: gate defaults off at every site; classification direct/promote/verdicts/trial; "
        "clip keeps only CPU-read pages, page-granular, gaps intact, merges neighbours");
}
'''
    with tempfile.TemporaryDirectory(prefix="dgpu-policy-") as tmp:
        src = Path(tmp) / "policy.cpp"
        exe = Path(tmp) / "policy.exe"
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
