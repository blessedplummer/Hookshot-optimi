"""The dGPU policy's mirror trial: its pure state machine (gpu/vk/vk_memory.cpp).

Rule 3 says a mirror exists only where a measured per-pool A/B proves it pays. The trial picks one
pool, measures the GPU's time a frame in its state now, flips it to the other state, measures
again, and either keeps the flip (with a clear margin) or re-measures the base state to check the
first window before putting it back - Triforce's demotion shape, credited, with the promotion
direction added for static pools. Verdicts that are clear go to hookshot_dgpu_mirrors.txt; windows
under a load and trials where the GPU is not the limit (>= 6 ms a frame) abort without one. The
whole machine is pure over the frame/gpu-time fed in, so this drives it through every path.

python -I -B tests/test_dgpu_trial.py [--cxx g++]
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

RUN_ALL_ARGS = ["--cxx", "g++"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"


def check_source():
    mem = (VK / "vk_memory.cpp").read_text(encoding="utf-8")
    # (the machine is where the executor can call it, and the executor is behind the gate)
    for needle in ("int dgpu_trial_step(DgpuTrial& t, uint64_t frame, int64_t gpu_ns) {",
                   "void dgpu_trial_run() {"):
        if needle not in mem:
            raise SystemExit("the trial machine is missing: " + needle)
    run = mem[mem.index("void dgpu_trial_run() {"):]
    run = run[:run.index("\n}") + 2]
    for needle in ("dgpu_trial_step(", "dgpu_keep_add(", "mirror_make(", "mirror_free(", "dgpu_trial_fails"):
        if needle not in run:
            raise SystemExit("the executor does not carry out " + needle)
    if "dgpu_on()" not in run or 'getenv("RX_VK_DGPU_TRIAL")' not in run:
        raise SystemExit("the executor is not behind RX_VK_DGPU / RX_VK_DGPU_TRIAL")
    # (a verdict never lands without a clear margin: only the two keep acts call dgpu_keep_add)
    if run.count("dgpu_keep_add(") != 2:
        raise SystemExit("verdicts must be written only by the two keep acts")
    # (the trial holds its pool from the baseline on, and gives it back on every finish)
    if "p.dgpu_trial = true" not in run or run.count("dgpu_trial = false") < 3:
        raise SystemExit("the trial must hold and release its pool's classification lock")


def run_fixture(cxx, source):
    text = source.read_text(encoding="utf-8")
    start = text.index("struct DgpuTrial {")
    end = text.index("DgpuTrial dgpu_trial;")
    machine = text[start:end]
    fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
namespace {
''' + machine + r'''
} // namespace
// (feed the machine frames at a constant GPU ms a frame until it returns an act or the budget runs out)
static int feed(DgpuTrial& t, uint64_t& frame, int64_t& gpu_ns, double ms, int budget) {
    for (int i = 0; i < budget; i++) {
        frame++;
        gpu_ns += (int64_t)(ms * 1e6);
        const int act = dgpu_trial_step(t, frame, gpu_ns);
        if (act) return act;
    }
    return 0;
}
int main() {
    uint64_t frame = 0;
    int64_t gpu_ns = 0;
    // (the GPU-limit gate: a starved GPU (under 6 ms a frame) aborts at the baseline, no flip)
    {
        DgpuTrial t{0x1000, 1, +1, false, 0, 0, 0, 0};
        CHECK(feed(t, frame, gpu_ns, 4.0, 60) == DT_ABORT);
        CHECK(!t.flipped && t.phase == 0);
    }
    // (promotion kept: a mirror 10% and 1 ms a frame faster clears 3% and 0.05 ms)
    {
        frame = 0, gpu_ns = 0;
        DgpuTrial t{0x1000, 1, +1, false, 0, 0, 0, 0};
        CHECK(feed(t, frame, gpu_ns, 10.0, 60) == DT_FLIP);        // (baseline 40 frames at 10 ms)
        CHECK(t.flipped && t.base_ms > 9.9);
        CHECK(feed(t, frame, gpu_ns, 10.0, 20) == 0);              // (settle 15: nothing to do yet)
        CHECK(feed(t, frame, gpu_ns, 9.0, 100) == DT_KEEP_VARIANT); // (measure 90 at 9 ms)
        CHECK(t.phase == 0 && t.after_ms > 8.9 && t.after_ms < 9.1);
    }
    // (promotion clearly worse: the mirror at 14 ms against direct's 10 - revert, the confirm window
    // agrees, and direct mapping keeps the pool: the 'D' verdict's path)
    {
        frame = 0, gpu_ns = 0;
        DgpuTrial t{0x2000, 1, +1, false, 0, 0, 0, 0};
        CHECK(feed(t, frame, gpu_ns, 10.0, 60) == DT_FLIP);
        CHECK(feed(t, frame, gpu_ns, 10.0, 20) == 0);
        CHECK(feed(t, frame, gpu_ns, 14.0, 100) == DT_REVERT);     // (not a win: 14 is not under 9.7)
        CHECK(!t.flipped);
        CHECK(feed(t, frame, gpu_ns, 14.0, 15) == 0);              // (confirm settle)
        CHECK(feed(t, frame, gpu_ns, 10.0, 50) == DT_KEEP_BASE);   // (re-measured base: 10 ms, the mirror lost)
        CHECK(t.phase == 0);
    }
    // (the first window lied: the mirror's window was the load's, and against the base re-measured at
    // 20 ms the 14 ms mirror WINS - DT_KEEP_VARIANT from the confirm, flipped again so the executor
    // re-makes the mirror it had just taken back)
    {
        frame = 0, gpu_ns = 0;
        DgpuTrial t{0x2800, 1, +1, false, 0, 0, 0, 0};
        CHECK(feed(t, frame, gpu_ns, 10.0, 60) == DT_FLIP);
        CHECK(feed(t, frame, gpu_ns, 10.0, 20) == 0);
        CHECK(feed(t, frame, gpu_ns, 14.0, 100) == DT_REVERT);     // (14 is not under 9.7: not a win)
        CHECK(feed(t, frame, gpu_ns, 14.0, 15) == 0);              // (confirm settle)
        CHECK(feed(t, frame, gpu_ns, 20.0, 50) == DT_KEEP_VARIANT); // (base re-measured at 20: the mirror wins)
        CHECK(t.phase == 0 && t.flipped);
    }
    // (demotion kept: direct mapping within Triforce's slack - 20% + 0.5 ms - is a keep, no confirm)
    {
        frame = 0, gpu_ns = 0;
        DgpuTrial t{0x3000, 1, -1, false, 0, 0, 0, 0};
        CHECK(feed(t, frame, gpu_ns, 8.0, 60) == DT_FLIP);
        CHECK(feed(t, frame, gpu_ns, 8.0, 20) == 0);
        CHECK(feed(t, frame, gpu_ns, 8.4, 100) == DT_KEEP_VARIANT);
        CHECK(t.phase == 0);
    }
    // (demotion clearly costly: direct at 14 ms against a mirrored 8 (over 1.5x) - the mirror comes
    // back, and the pool is kept mirrored)
    {
        frame = 0, gpu_ns = 0;
        DgpuTrial t{0x4000, 1, -1, false, 0, 0, 0, 0};
        CHECK(feed(t, frame, gpu_ns, 8.0, 60) == DT_FLIP);
        CHECK(feed(t, frame, gpu_ns, 8.0, 20) == 0);
        CHECK(feed(t, frame, gpu_ns, 14.0, 100) == DT_REVERT);
        CHECK(feed(t, frame, gpu_ns, 14.0, 15) == 0);
        CHECK(feed(t, frame, gpu_ns, 8.0, 50) == DT_KEEP_BASE);
        CHECK(t.phase == 0 && !t.flipped);
    }
    // (neither state clearly better: noise, nothing on disk, the pool left where it started)
    {
        frame = 0, gpu_ns = 0;
        DgpuTrial t{0x5000, 1, -1, false, 0, 0, 0, 0};
        CHECK(feed(t, frame, gpu_ns, 8.0, 60) == DT_FLIP);
        CHECK(feed(t, frame, gpu_ns, 8.0, 20) == 0);
        CHECK(feed(t, frame, gpu_ns, 11.0, 100) == DT_REVERT);     // (over the slack, under 1.5x)
        CHECK(feed(t, frame, gpu_ns, 11.0, 15) == 0);
        CHECK(feed(t, frame, gpu_ns, 8.0, 50) == DT_ABORT);
        CHECK(t.phase == 0);
    }
    // (a load inside the measure window: under 3 ms a frame is not a trial, abort without a verdict)
    {
        frame = 0, gpu_ns = 0;
        DgpuTrial t{0x6000, 1, -1, false, 0, 0, 0, 0};
        CHECK(feed(t, frame, gpu_ns, 8.0, 60) == DT_FLIP);
        CHECK(feed(t, frame, gpu_ns, 8.0, 20) == 0);
        CHECK(feed(t, frame, gpu_ns, 1.5, 100) == DT_ABORT);
        CHECK(t.phase == 0);
    }
    // (the windows are frame-counted: no act early, whatever the times say)
    {
        frame = 0, gpu_ns = 0;
        DgpuTrial t{0x7000, 1, +1, false, 0, 0, 0, 0};
        for (int i = 0; i < 39; i++) {
            frame++;
            gpu_ns += (int64_t)(10 * 1e6);
            CHECK(dgpu_trial_step(t, frame, gpu_ns) == DT_NONE);
        }
        CHECK(t.phase == 1);
    }
    // (the keep FILE's test: only a clear winner (10% and 0.05 ms either way) outlives the run - the uncapped
    // first cut wrote 32 verdicts from noisy gameplay windows)
    CHECK(dgpu_variant_clear(10.0, 8.5));    // (15% faster: clear)
    CHECK(!dgpu_variant_clear(10.0, 9.5));   // (5%: an in-run keep at best, not a file verdict)
    CHECK(!dgpu_variant_clear(10.0, 9.9));   // (1% and 0.1 ms: noise)
    CHECK(!dgpu_variant_clear(8.5, 10.0));   // (slower is never clear; KEEP_BASE reads it with the sides swapped)
    CHECK(!dgpu_variant_clear(0.0, 0.0));    // (a zero window proves nothing)
    std::puts("PASS: the trial gates on GPU time and loads, keeps flips only past the measured margins, "
        "confirms a failed window against the base state, and writes only clear winners to the keep file");
}
'''
    with tempfile.TemporaryDirectory(prefix="dgpu-trial-") as tmp:
        src = Path(tmp) / "trial.cpp"
        exe = Path(tmp) / "trial.exe"
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
