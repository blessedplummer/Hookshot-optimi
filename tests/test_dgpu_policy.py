"""RX_VK_DGPU's policy decisions (gpu/vk/vk_memory.cpp), driven as a compiled fixture.

The switch has four decisions that do not need a GPU to check, and this cuts their block out of the source by text
(its markers) so the test reads the real code rather than a copy:
- the gate: the policy asked for, and the mirrors it tunes on (RX_VK_MIRROR=0 wins);
- a pool's class: a GPU-written pool is a render target and must be mirrored; a static (never-written) one stays
  direct in guest memory unless a measured trial kept it;
- the write-back gate: only a pool the CPU has read a page of is copied back to guest memory (a pure render target's
  GPU writes stay device-local);
- the trial: whether a window's GPU time says the mirror pays, and the phase clock (settle, measure, decide).

CPU-compiled, no game/GPU.

python -I -B tests/test_dgpu_policy.py [--cxx g++]
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

RUN_ALL_ARGS = ["--cxx", "g++"]

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cxx', default='g++')
    parser.add_argument('--source', type=Path, default=ROOT / 'gpu/vk/vk_memory.cpp')
    args = parser.parse_args()
    text = args.source.read_text(encoding='utf-8')
    start = text.index('// ---- (RX_VK_DGPU) policy decisions')
    end = text.index('// ---- end of the policy decisions ----') + len('// ---- end of the policy decisions ----')
    decisions = text[start:end]
    for name in ('dgpu_policy_on', 'dgpu_pool_mirrored', 'dgpu_pool_writes_back', 'dgpu_trial_keeps', 'dgpu_trial_step'):
        if name not in decisions:
            raise SystemExit('the policy-decisions block is missing %s: cut markers moved' % name)
    fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
''' + decisions + r'''
int main() {
    // (the gate: both the switch and the mirrors it tunes; RX_VK_MIRROR=0 leaves stock behavior)
    CHECK(!dgpu_policy_on(false, true));
    CHECK(!dgpu_policy_on(true, false));
    CHECK(!dgpu_policy_on(false, false));
    CHECK(dgpu_policy_on(true, true));
    // (a pool's class: render targets always, static data only with a trial's keep)
    CHECK(dgpu_pool_mirrored(true, false));    // (a GPU write: device-local is the authoritative copy)
    CHECK(dgpu_pool_mirrored(false, true));    // (a trial proved the mirror pays)
    CHECK(dgpu_pool_mirrored(true, true));
    CHECK(!dgpu_pool_mirrored(false, false));  // (static, no trial: direct, no mirror, no write-back)
    // (the write-back gate: a pool the CPU read keeps stock's write-backs; a pure render target's stay device-local)
    CHECK(dgpu_pool_writes_back(true));
    CHECK(!dgpu_pool_writes_back(false));
    // (the trial's decision: mirrored must be at least 3% and 0.05 ms faster)
    CHECK(dgpu_trial_keeps(10.0, 9.5, 0.03, 0.05));     // (9.5 * 1.03 + 0.05 = 9.835 <= 10)
    CHECK(!dgpu_trial_keeps(10.0, 9.9, 0.03, 0.05));    // (10.247 > 10)
    CHECK(!dgpu_trial_keeps(10.0, 10.5, 0.03, 0.05));
    CHECK(!dgpu_trial_keeps(0.0, 1.0, 0.03, 0.05));     // (no baseline: no answer)
    CHECK(!dgpu_trial_keeps(10.0, 0.0, 0.03, 0.05));
    CHECK(dgpu_trial_keeps(2.0, 1.0, 0.10, 0.05));      // (a clear win: the keep list's margin)
    // (the phase clock: settle one window, measure one, then decide)
    const uint64_t W = 240, S = 1000;
    CHECK(dgpu_trial_step(0, S, S, W) == 0);            // (idle)
    CHECK(dgpu_trial_step(1, S + W - 1, S, W) == 1);    // (still settling)
    CHECK(dgpu_trial_step(1, S + W, S, W) == 2);        // (settled: measure now)
    CHECK(dgpu_trial_step(2, S + W - 1, S, W) == 2);
    CHECK(dgpu_trial_step(2, S + W, S, W) == 3);        // (measured: decide now)
    std::puts("PASS: the gate needs the switch and the mirrors; static pools stay direct without a trial; only a pool "
              "the CPU read is copied back; the trial keeps only a measured win and its clock settles, measures and "
              "decides at the window's edge");
}
'''
    with tempfile.TemporaryDirectory(prefix='dgpu-policy-') as tmp:
        src = Path(tmp) / 'policy.cpp'
        exe = Path(tmp) / 'policy.exe'
        src.write_text(fixture, encoding='utf-8')
        subprocess.run([args.cxx, '-std=c++20', '-O1', '-Wall', '-Wextra', '-Werror', str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
