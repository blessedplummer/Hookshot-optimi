"""Compile and run the dGPU policy's rules (gpu/vk/dgpu_policy.h; no game/GPU required), and check that vk_memory.cpp
still wires them the way the policy states them: RX_VK_DGPU gates the whole policy (a pool's first binding makes no
mirror under it and marks it refused - in guest memory, nothing synced), the promotion trials run one at a time on
the GPU time the recordings collect, a verdict keeps a mirror only when the trial measured it clearly faster, a
loss goes to hookshot_mirror_skip.txt beside the game's own files, and write-dominated pools are no candidates.

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
    # (the policy is opt-in, one switch, and says so once)
    need("vk_memory.cpp: dgpu_on()", block(mem, "bool dgpu_on()"),
         ['getenv("RX_VK_DGPU")', "atoi(getenv(\"RX_VK_DGPU\")) != 0 ? \"on\" : \"off"])
    # (a pool's first binding makes no mirror and queues none under the policy: it is marked refused, so its later
    # read binds take the lock-free fast path, and its writes land in guest memory itself)
    bind = block(mem, "BufferRef gpu_buffer_bind(")
    need("vk_memory.cpp: gpu_buffer_bind()", bind,
         ["!defer && p->direct_write_epoch != mirror_epoch &&",   # (the make, only for a trial-won pool under the policy)
          "(!dgpu_on() || p->dgpu_mirror) && !par && mirror_make(*p)",
          "if (dgpu_on() && !p->dgpu_mirror) {   // (the policy's default: in guest memory until a trial says otherwise)",
          "p->mstate = Pool::MIRROR_REFUSED;",
          "if (dgpu_on() && dgpu_trial.lo == p->lo) p->dgpu_binds++;"])
    # (the trials: one at a time, on the game's frames and the recordings' GPU time; the make happens at the pool's
    # next binding, where the uploads are open - a copy recorded at a submission's end would land behind it)
    trials = block(mem, "void dgpu_trials() {")
    need("vk_memory.cpp: dgpu_trials()", trials,
         ["dgpu_trial.step(f, g)", "gpu_time_total_ns()", "p->mstate = Pool::MIRROR_NONE;",
          "if (p->mstate == Pool::MIRROR_ON) mirror_free(*p);", "dgpu::mirror_worth(direct, mirrored, dgpu_gain_pct(), dgpu_floor_ms())"])
    # (the candidate: this window's heaviest pool in guest-memory reads, not write-dominated, not decided; picked
    # while the window's reads still exist)
    pick = block(mem, "void dgpu_pick(const std::unordered_map<uint64_t, uint64_t>& read_bytes) {")
    need("vk_memory.cpp: dgpu_pick()", pick,
         ["getenv(\"RX_VK_DGPU_TRIALS\")", "dgpu_trial.phase != dgpu::Trial::Idle",
          "p->dgpu_wrote >= bytes", "std::find(dgpu_skip().begin(), dgpu_skip().end(), lo)"])
    stats = block(mem, "void memory_end_submission(")
    need("vk_memory.cpp: memory_end_submission()", stats,
         ["if (dgpu_on()) {", "dgpu_pick(direct_bytes_by_pool);", "p.dgpu_wrote = 0;", "if (dgpu_on()) dgpu_trials();"])
    # (the GPU time the trials read: the recordings' own timestamps, and a total that the 120-frame reset of
    # st_gpu_ns does not touch)
    need("vk_replay.cpp: gpu_time_collect()", block(replay, "void gpu_time_collect(Recording& r)"),
         ["gpu_ns_total += ns;"])
    need("vk_internal.h: gpu_time_total_ns()", internal, ["int64_t gpu_time_total_ns();"])
    # (a losing verdict is kept beside the game's own files, as hookshot_options.ini and hookshot_programs.bin are)
    skip = block(mem, "std::string dgpu_skip_path() {")
    need("vk_memory.cpp: dgpu_skip_path()", skip,
         ["game_module_path(exe, MAX_PATH)", "hookshot_mirror_skip.txt"])
    # (the sub-knobs, each read once)
    need("vk_memory.cpp: dgpu_gain_pct()", block(mem, "double dgpu_gain_pct() {"), ['getenv("RX_VK_DGPU_GAIN")'])
    need("vk_memory.cpp: dgpu_floor_ms()", block(mem, "double dgpu_floor_ms() {"), ['getenv("RX_VK_DGPU_FLOOR")'])
    # (documented where the surrounding switches are)
    need("docs/development.md: the GPU switches", docs,
         ["`RX_VK_DGPU=1`", "`hookshot_mirror_skip.txt`", "`RX_VK_DGPU_TRIALS=0`"])


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
