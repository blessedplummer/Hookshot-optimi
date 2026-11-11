"""Compile and run the dGPU policy's rules (gpu/vk/dgpu_policy.h; no game/GPU required), and check that vk_memory.cpp
still wires them the way the policy states them: RX_VK_DGPU gates the whole policy (a pool's first binding makes no
mirror under it and marks it refused - in guest memory, nothing synced; unset, the policy costs nothing, not even its
counters), the promotion trials run one at a time on the GPU time the recordings collect, a verdict keeps a mirror
only when the trial measured it faster by the percent and the floor (whichever asks more), a loss is added to
hookshot_mirror_skip.txt beside the game's own files, and a candidate needs reads above the noise and not
write-dominated.

python -I -B tests/test_dgpu_policy.py [--cxx g++]
"""
import argparse
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "g++"]

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


def check_source():
    mem = (ROOT / "gpu/vk/vk_memory.cpp").read_text(encoding="utf-8")
    replay = (ROOT / "gpu/vk/vk_replay.cpp").read_text(encoding="utf-8")
    internal = (ROOT / "gpu/vk/vk_internal.h").read_text(encoding="utf-8")
    docs = (ROOT / "docs/development.md").read_text(encoding="utf-8")
    # (the policy is opt-in, one switch, and says so once - only when it is on: unset, the log is stock's)
    need("vk_memory.cpp: dgpu_on()", block(mem, "bool dgpu_on()"),
         ['getenv("RX_VK_DGPU")', "if (v) logf(\"vk: dGPU policy on (RX_VK_DGPU)"])
    # (a pool's first binding makes no mirror and queues none under the policy: it is marked refused, so its later
    # read binds take the lock-free fast path, and its writes land in guest memory itself)
    bind = block(mem, "BufferRef gpu_buffer_bind(")
    need("vk_memory.cpp: gpu_buffer_bind()", bind,
         ["!defer && p->direct_write_epoch != mirror_epoch &&",   # (the make, only for a trial-won pool under the policy)
          "(!dgpu_on() || p->dgpu_mirror) && !par && mirror_make(*p)",
          "if (dgpu_on() && !p->dgpu_mirror) {   // (the policy's default: in guest memory until a trial says otherwise)",
          "p->mstate = Pool::MIRROR_REFUSED;",
          "if (dgpu_on() && dgpu_trial.lo == p->lo) p->dgpu_binds++;",
          # (the lock-free fast path counts the window's guest-memory bytes too, under the switch: unset, this path
          # is an iGPU run's main read path and would pay two map touches per bind for nothing)
          "if (dgpu_on() && !par) direct_bytes_by_pool[p->lo] += size;"])
    # (the trials: one at a time, on the game's frames and the recordings' GPU time; the make happens at the pool's
    # next binding, where the uploads are open - a copy recorded at a submission's end would land behind it; serial
    # replay only, and a losing verdict also leaves the pool out of the wanted queue)
    trials = block(mem, "void dgpu_trials() {")
    need("vk_memory.cpp: dgpu_trials()", trials,
         ["dgpu_trial.step(f, g)", "gpu_time_total_ns()", "p->mstate = Pool::MIRROR_NONE;",
          "if (p->mstate == Pool::MIRROR_ON) mirror_free(*p);",
          "dgpu::mirror_worth(direct, mirrored, dgpu_gain_pct(), dgpu_floor_ms())",
          "const auto wanted = std::find(mirror_wanted.begin(), mirror_wanted.end(), p);",
          "mirror_wanted.erase(wanted);"])
    # (the candidate: this window's heaviest pool in guest-memory bytes, trial_candidate's floor and write check,
    # not parallel, not decided; picked while the window's bytes still exist)
    pick = block(mem, "void dgpu_pick(const std::unordered_map<uint64_t, uint64_t>& read_bytes) {")
    need("vk_memory.cpp: dgpu_pick()", pick,
         ["getenv(\"RX_VK_DGPU_TRIALS\")", "par_active.load(std::memory_order_relaxed)",
          "dgpu_trial.phase != dgpu::Trial::Idle", "dgpu::trial_candidate(bytes, p->dgpu_wrote)",
          "std::find(dgpu_skip().begin(), dgpu_skip().end(), lo)"])
    stats = block(mem, "void memory_end_submission(")
    need("vk_memory.cpp: memory_end_submission()", stats,
         ["if (dgpu_on()) {", "dgpu_pick(direct_bytes_by_pool);", "p.dgpu_wrote = 0;", "if (dgpu_on()) dgpu_trials();"])
    # (the GPU time the trials read: the recordings' own timestamps, and a total that the 120-frame reset of
    # st_gpu_ns does not touch)
    need("vk_replay.cpp: gpu_time_collect()", block(replay, "void gpu_time_collect(Recording& r)"),
         ["gpu_ns_total += ns;"])
    need("vk_internal.h: gpu_time_total_ns()", internal, ["int64_t gpu_time_total_ns();"])
    # (a losing verdict is kept beside the game's own files, as hookshot_options.ini and hookshot_programs.bin are,
    # and the verdict really writes it: the skip list is the policy's memory across runs)
    skip = block(mem, "std::string dgpu_skip_path() {")
    need("vk_memory.cpp: dgpu_skip_path()", skip,
         ["game_module_path(exe, MAX_PATH)", "hookshot_mirror_skip.txt", "if (!n) return {};"])
    need("vk_memory.cpp: the verdict's skip line", block(mem, "case dgpu::Act::Decide:"),
         ["dgpu_skip_add(p->lo);"])
    # (the sub-knobs, each read once)
    need("vk_memory.cpp: dgpu_gain_pct()", block(mem, "double dgpu_gain_pct() {"), ['getenv("RX_VK_DGPU_GAIN")'])
    need("vk_memory.cpp: dgpu_floor_ms()", block(mem, "double dgpu_floor_ms() {"), ['getenv("RX_VK_DGPU_FLOOR")'])
    # (documented where the surrounding switches are)
    need("docs/development.md: the GPU switches", docs,
         ["`RX_VK_DGPU=1`", "`hookshot_mirror_skip.txt`", "`RX_VK_DGPU_TRIALS=0`", "at least 1 MB of it in guest memory"])


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
