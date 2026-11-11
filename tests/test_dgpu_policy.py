"""Compile and run the dGPU policy's rules (gpu/vk/dgpu_policy.h; no game/GPU required), and check that vk_memory.cpp
still wires them the way the policy states them: RX_VK_DGPU gates the whole policy (a pool's first binding makes no
mirror under it and marks it refused - in guest memory, nothing synced; unset, the policy costs nothing, not even its
counters), the refused pools' uniform buffers are copied as submitted (issue 1795's in-place hazard), the promotion
trials run one at a time on the frame's present-to-present time, a verdict keeps a mirror only when the trial measured
it faster by the percent and the floor (whichever asks more), a loss is added to hookshot_mirror_skip.txt beside the
game's own files under a header naming the device and driver, and a candidate needs reads above the noise, not
write-dominated, a mirror that can come on, and a serial replay.

python -I -B tests/test_dgpu_policy.py [--cxx g++]
"""
import argparse
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]


def block(text, marker):
    start = text.index(marker)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def need(where, text, needles):
    for needle in needles:
        if needle not in text:
            raise SystemExit(where + " no longer has: " + needle)


def forbid(where, text, needle):
    if needle in text:
        raise SystemExit(where + " still has (it should be gone): " + needle)


def check_source():
    mem = (ROOT / "gpu/vk/vk_memory.cpp").read_text(encoding="utf-8")
    replay = (ROOT / "gpu/vk/vk_replay.cpp").read_text(encoding="utf-8")
    draw = (ROOT / "gpu/vk/vk_draw.cpp").read_text(encoding="utf-8")
    internal = (ROOT / "gpu/vk/vk_internal.h").read_text(encoding="utf-8")
    docs = (ROOT / "docs/development.md").read_text(encoding="utf-8")
    # (the policy is opt-in, one switch (env_on's rule since #2042), and says so once - only when it is on: unset,
    # the log is stock's)
    need("vk_memory.cpp: dgpu_on()", block(mem, "bool dgpu_on()"),
         ['env_on("RX_VK_DGPU")', "if (v) logf(\"vk: dGPU policy on (RX_VK_DGPU)"])
    # (a pool's first binding makes no mirror and queues none under the policy: it is marked refused, so its later
    # read binds take the lock-free fast path, and its writes land in guest memory itself)
    bind = block(mem, "BufferRef gpu_buffer_bind(")
    need("vk_memory.cpp: gpu_buffer_bind()", bind,
         ["!defer && p->direct_write_epoch != mirror_epoch &&",   # (the make, only for a trial-won pool under the policy)
          "(!dgpu_on() || p->dgpu_mirror) && !par && mirror_make(*p)",
          "if (dgpu_on() && !p->dgpu_mirror) {   // (the policy's default: in guest memory until a trial says otherwise)",
          "p->mstate = Pool::MIRROR_REFUSED;",
          "if (dgpu_on() && dgpu_trial.lo == p->lo) p->dgpu_binds++;",
          # (the lock-free fast path counts the window's guest-memory bytes too, under the switch, in the pool's own
          # field - unset, this path is an iGPU run's main read path and would pay a shared-map touch per bind for
          # nothing. Under a parallel replay neither counter runs: the fast path is lock-free there and the counters
          # are the trials' fodder, and the trials are a serial-replay feature - no data race, and nothing to count
          "if (dgpu_on() && !par) {",
          "p->dgpu_read += size;",
          "if (dgpu_trial.lo == p->lo) p->dgpu_binds++;"])
    need("vk_memory.cpp: the locked direct path", bind,
         ["if (dgpu_on() && !written) p->dgpu_read += size;",
          "else if (dgpu_on()) p->dgpu_read += size;"])
    # (issue 1795's hazard, per pool: the refused pools' uniform buffers are copied as submitted - a binding in
    # guest memory is read when the recording executes, after the game rewrote its constants. A trial-won pool's
    # mirror was synced at the submit and binds in place)
    need("vk_draw.cpp: the policy's UB snapshot", draw,
         ["BufferRef b = snapshot ? BufferRef{} : gpu_buffer_bind(u.addr, n, false);",
          "&& !dgpu_ub_in_place(b)"])
    need("vk_internal.h: dgpu_ub_in_place()", internal,
         ["bool dgpu_ub_in_place(const BufferRef& b);"])
    # (the trials: one at a time, on the game's frames and the frame's present-to-present time - the mirror's cost
    # is mostly CPU and in the upload buffer, which the recordings' GPU timestamps never time; the make happens at
    # the pool's next binding, where the uploads are open - a copy recorded at a submission's end would land behind
    # it; serial replay only, and a losing verdict also leaves the pool out of the wanted queue)
    trials = block(mem, "void dgpu_trials() {")
    need("vk_memory.cpp: dgpu_trials()", trials,
         ["present_time_total_ns()", "p->mstate = Pool::MIRROR_NONE;",
          "if (p->mstate == Pool::MIRROR_ON) mirror_free(*p);",
          "dgpu::mirror_worth(direct, mirrored, dgpu_gain_pct(), dgpu_floor_ms())",
          "const auto wanted = std::find(mirror_wanted.begin(), mirror_wanted.end(), p);",
          "mirror_wanted.erase(wanted);",
          # (the settle waits for the mirror to really be on; a whole window of waiting aborts without a verdict)
          "dgpu_trial.step(f, g, tp && tp->mstate == Pool::MIRROR_ON)",
          "case dgpu::Act::Abort:",
          # (the before must have measured the pool at work: binds on it)
          "if (!p->dgpu_binds) {",
          # (a give-up repeats otherwise - the same pool, every window: three and it sits out the session)
          "if (++p->dgpu_aborts >= 3) {",
          "p->dgpu_done = true;",
          "dgpu_trial.base_binds = p->dgpu_binds;",
          # (the two windows must have measured the same workload: over 2x either way voids the A/B, no verdict)
          "p->dgpu_binds > 2 * dgpu_trial.base_binds || 2 * p->dgpu_binds < dgpu_trial.base_binds",
          # (the settle-turned-measure counts the after window's binds from zero)
          "tp->dgpu_binds = 0;"])
    # (the candidate: this window's heaviest pool in guest-memory bytes, from the pools' own fields (not a shared
    # map off the lock); trial_candidate's floor and write check; only where a mirror can come on (an iGPU's trials
    # would start, wait out the settle, give up and start again, forever); not a parallel replay (its windows' bind
    # bytes freeze while the segments record - the policy runs trial-less there, every pool in guest memory); not
    # decided; picked while the window's bytes still exist. par_active only covers the segments' recording and is
    # false again by the time this runs, so the RX_VK_PARALLEL switch itself is the gate)
    pick = block(mem, "void dgpu_pick() {")
    need("vk_memory.cpp: dgpu_pick()", pick,
         ['getenv("RX_VK_DGPU_TRIALS") ? atoi(getenv("RX_VK_DGPU_TRIALS")) != 0 : true',
          "!mirror_on() || par_threads() >= 2 || dgpu_trial.phase != dgpu::Trial::Idle",
          "dgpu::trial_candidate(p.dgpu_read, p.dgpu_wrote)",
          "std::find(dgpu_skip().begin(), dgpu_skip().end(), lo)", "best->dgpu_read / 1048576.0",
          "dgpu_trial.start(best->lo, texture_frame_no(), present_time_total_ns())"])
    stats = block(mem, "void memory_end_submission(")
    need("vk_memory.cpp: memory_end_submission()", stats,
         ["if (dgpu_on()) {", "dgpu_pick();", "p.dgpu_read = 0; p.dgpu_wrote = 0;", "if (dgpu_on()) dgpu_trials();"])
    # (the frame time the trials read: present-to-present, one clock read a present, never reset - the 120-frame
    # status line resets st_gpu_ns, the trials' deltas must not see that; the GPU-only total is gone, its verdict
    # kept mirrors while the frame was CPU-bound)
    need("vk_replay.cpp: the present total", replay,
         ["int64_t present_ns_total = 0;", "int64_t present_time_total_ns() { return present_ns_total; }",
          "if (have) present_ns_total += std::chrono::duration_cast<std::chrono::nanoseconds>(t - prev).count();"])
    forbid("vk_replay.cpp", replay, "gpu_time_total_ns")
    need("vk_internal.h: present_time_total_ns()", internal, ["int64_t present_time_total_ns();"])
    forbid("vk_internal.h", internal, "gpu_time_total_ns")
    need("vk_internal.h: par_threads()", internal, ["int par_threads();"])
    # (a losing verdict is kept beside the game's own files, as hookshot_options.ini and hookshot_programs.bin are,
    # and the verdict really writes it: the skip list is the policy's memory across runs - headed by the device and
    # driver it was measured on, so another GPU's or driver's file does not hold pools out of their mirrors here)
    need("vk_memory.cpp: dgpu_skip_path()", block(mem, "std::string dgpu_skip_path() {"),
         ["game_module_path(exe, MAX_PATH)", "hookshot_mirror_skip.txt", "if (!n) return {};"])
    need("vk_memory.cpp: the skip file's header", block(mem, "std::string dgpu_skip_header() {"),
         ["dev.props.deviceName", "dev.props.driverVersion"])
    need("vk_memory.cpp: the skip list's load", block(mem, "std::vector<uint64_t>& dgpu_skip() {"),
         ["dgpu_skip_file_ok = first == dgpu_skip_header();", "its pools trial again"])
    need("vk_memory.cpp: the verdict's skip line", block(mem, "void dgpu_skip_add(uint64_t lo) {"),
         ['dgpu_skip_file_ok ? "a" : "w"', "dgpu_skip_header().c_str()"])
    need("vk_memory.cpp: the verdict's skip add", block(mem, "case dgpu::Act::Decide:"),
         ["dgpu_skip_add(p->lo);"])
    # (the sub-knobs, each read once)
    need("vk_memory.cpp: dgpu_gain_pct()", block(mem, "double dgpu_gain_pct() {"), ['getenv("RX_VK_DGPU_GAIN")'])
    need("vk_memory.cpp: dgpu_floor_ms()", block(mem, "double dgpu_floor_ms() {"), ['getenv("RX_VK_DGPU_FLOOR")'])
    # (documented where the surrounding switches are)
    need("docs/development.md: the GPU switches", docs,
         ["`RX_VK_DGPU=1`", "`hookshot_mirror_skip.txt`", "`RX_VK_DGPU_TRIALS=0`", "at least 1 MB of it in guest memory",
          "present-to-present", "issue 1795", "device and driver", "RX_VK_PARALLEL runs the policy trial-less"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="g++")
    args = parser.parse_args()
    check_source()
    with tempfile.TemporaryDirectory(prefix="dgpu-policy-") as tmp:
        exe = Path(tmp) / "dgpu-policy"
        subprocess.run([args.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                        str(ROOT / "tests/test_dgpu_policy.cpp"), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True, timeout=60)


if __name__ == "__main__":
    main()
