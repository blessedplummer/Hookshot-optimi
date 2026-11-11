"""The dGPU PC-like policy's gate, classification and pool-level write-back gate (gpu/vk/vk_memory.cpp).

RX_VK_DGPU turns on three things that answer each other: static pools direct-mapped in guest memory
(no mirror, no sync, no write-back), GPU-written pools writing back only where the CPU looks (no diff
pass), and one pool at a time trialed in the other state. Unset, every helper returns false and the
stock paths stand - this test is that claim, checked at each site the policy touches, plus the two
pure pieces compiled and driven: the classification's decision and the write-back gate.

The gate is POOL-level on purpose: the game reads GPU results outside the readback API too (the
replay's record-time reads of indirect and index data), and a page-sparse copy broke exactly that -
with all copies skipped the same 4K scene issued 3941 draws against 1364 (deepseek's measurement;
draw-count parity OFF vs ON is the detector). Where the CPU looks, the whole pool's writes go back.

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
                "bool memory_pool_gpu_written(uint64_t guest, uint64_t size) {",
                "void dgpu_trial_run() {",
                "char dgpu_keep_verdict(uint64_t lo) {",
                "void dgpu_mark_wrote(uint64_t guest, uint64_t size) {"):
        if "dgpu_on()" not in block(mem, sig):
            raise SystemExit("the dGPU gate is missing in " + sig)
    # (rule 1: the drain drops a whole pool's write-backs where the CPU never looks - pool-level,
    # never page-level - and the readback itself queues its range's copy and settles the evidence)
    end = block(mem, "void memory_end_submission(VkCommandBuffer cmd) {")
    for needle in ("dgpu_pool_writes_back(p->dgpu_read)", "st_dgpu_wb_skipped", "st_dgpu_wb_copied",
                   "const bool diff = !dgpu_on() && wb_diff_on()"):
        if needle not in end:
            raise SystemExit("the pool-level write-back gate is missing in memory_end_submission: " + needle)
    note = block(mem, "void memory_note_readback(uint64_t guest, uint64_t size) {")
    for needle in ("p.dgpu_read = true, p.dgpu_wrote = true",
                   "writebacks.push_back({&p, guest - p.lo, guest - p.lo + size})"):
        if needle not in note:
            raise SystemExit("the readback does not queue its own range / settle the evidence: " + needle)
    # (rule 2's promotion path: the classification decides, the stock mirror machinery applies it)
    bind = block(mem, "BufferRef gpu_buffer_bind(uint64_t gpu, uint64_t size, bool written) {")
    if "dgpu_classify(" not in bind:
        raise SystemExit("gpu_buffer_bind does not classify through dgpu_classify")
    if "Pool::MIRROR_REFUSED" not in bind or "Pool::MIRROR_NONE" not in bind:
        raise SystemExit("the classification must land in the stock states (REFUSED direct, NONE to be made)")
    # (the readbacks learn the pool before the write-back gate could have covered it, and a GPU-written
    # pool's first read waits for the copy-back stock's value cannot name yet)
    if "if (policy) memory_note_readback(" not in replay or "memory_pool_gpu_written(host->gpu_guest(gpu), size)" not in replay:
        raise SystemExit("vk_replay does not note readbacks / wait on the first read under the policy")
    # (the keep file lives where the game folder's other state lives: beside image.bin, vk_paths.h)
    keep = block(mem, "std::string dgpu_keep_path() {")
    if "game_module_path(" not in keep or "hookshot_dgpu_mirrors.txt" not in keep:
        raise SystemExit("the keep file must be built with game_module_path (the game folder's rule)")
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
    gate_start = text.index("inline bool dgpu_pool_writes_back(")
    gate = text[gate_start:text.index("\n}", gate_start) + 3]
    fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
namespace {
''' + classify + gate + r'''
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
    // (the write-back gate: all of a CPU-read pool's writes go back, none of an unread pool's - pool
    // granularity, the measured safe shape: a page-level cull dropped the game's own reads of GPU results)
    CHECK(dgpu_pool_writes_back(true));      // (a readback read it: whole pool coherent where the CPU looks)
    CHECK(!dgpu_pool_writes_back(false));    // (never read: no copy, no diff, no wait - the sync that was cut)
    std::puts("PASS: gate defaults off at every site; classification direct/promote/verdicts/trial; "
        "write-back gate pool-level: CPU-read pool kept whole, unread pool dropped whole");
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
