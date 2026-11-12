"""The uniform buffer's per-binding snapshot (issue 1795's in-place hazard, keyed per pool state): a binding that IS
the pool's guest memory - the pool has no mirror at the read - is copied as submitted, while a mirror's binding
(synced at the submit) and a bind outside a recording stay in place. Compiles the production selection hunk
(vk_draw.cpp's uniform-buffer block) with the environment stubbed, and checks vk_memory.cpp still tags the bindings
that are guest memory (BufferRef::live on the three direct paths of gpu_buffer_bind).

python -I -B tests/test_ub_snapshot_per_pool.py [--cxx g++]
"""
import argparse
import os
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]
# (env_on, the switches' rule, before the code cut out of the sources: gpu/vk/compat/hookshot_env.h)
ENV_ON_H = str(Path(__file__).resolve().parents[1] / "gpu" / "vk" / "compat" / "hookshot_env.h")


def block(text, marker):
    start = text.index(marker)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def cut(text, start, end):
    at = text.index(start)
    return text[at:text.index(end, at)]


def need(where, text, needles):
    for needle in needles:
        if needle not in text:
            raise SystemExit(where + " no longer has: " + needle)


# (one scenario a process: the selection's snapshot switch is a function-local static, read once)
# the bind spec: "buffer,offset,size,address,live" as gpu_buffer_bind would return it, or "outside" (no pool)
CASES = [
    ("unset_live",     None, "1,64,16,0,1",  "stream"),
    ("unset_mirror",   None, "1,64,16,0,0",  "bind"),
    ("unset_outside",  None, "outside",      "stream"),
    ("unset_misalign", None, "1,63,16,0,1",  "stream"),
    ("snap1_mirror",   "1",  "1,64,16,0,0",  "stream"),
    ("snap1_live",     "1",  "1,64,16,0,1",  "stream"),
    ("snap0_live",     "0",  "1,64,16,0,1",  "stream"),
]

FIXTURE = r'''
// The uniform buffer's per-binding snapshot decision, as vk_draw.cpp's block makes it: the production hunk below
// (cut from the source) with gpu_buffer_bind, the stream and the captures stubbed. One scenario a process.
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <cstdlib>

#define VK_NULL_HANDLE nullptr
using VkBuffer = void*;
using VkDeviceAddress = uint64_t;
#define FINE_N(a, b)
static void logf(const char*, ...) {}

#include "ub_bufferref.inc"

struct nvn_p_bind_buf { uint64_t addr, size; };
struct Recording { uint8_t* mapped; VkBuffer stream; };

static VkBuffer stream_buf = (VkBuffer)0x5175ea;
static int stream_allocs = 0, bind_calls = 0;
static uint64_t stream_alloc(uint64_t, uint32_t) { stream_allocs++; return 4096; }
static bool memory_read_as_submitted(uint64_t, void* dst, uint64_t n) { std::memset(dst, 0xAB, n); return true; }

// (the case's bind: what gpu_buffer_bind would return, live flag and all)
static BufferRef the_bind;
static bool bind_outside = false;
static BufferRef gpu_buffer_bind(uint64_t, uint64_t, bool) {
    bind_calls++;
    return bind_outside ? BufferRef{} : the_bind;
}

static uint8_t mapped_storage[65536];
static Recording rec{mapped_storage, stream_buf};
static Recording* cur = &rec;

static BufferRef ub_bind(const nvn_p_bind_buf& u, uint64_t n, uint32_t align, BufferRef bi) {
#include "ub_select.inc"
}

int main(int argc, char** argv) {
    (void)argc;
    const char* fail = nullptr;
    const char* snap = std::getenv("RX_VK_UB_SNAPSHOT");
    if (std::strcmp(argv[2], "outside") == 0) {
        bind_outside = true;
    } else {
        unsigned long long f[4] = {0, 0, 0, 0};
        int live = 0;
        if (std::sscanf(argv[2], "%llu,%llu,%llu,%llu,%d", &f[0], &f[1], &f[2], &f[3], &live) != 5) {
            std::printf("FAIL %s: bad bind spec\n", argv[1]);
            return 1;
        }
        the_bind = BufferRef{(void*)(uintptr_t)f[0], f[1], f[2], f[3], live != 0};
    }
    BufferRef r = ub_bind({0x1234'5000ull, 4096}, 256, 16, BufferRef{});
    const bool streamed = r.buffer == stream_buf;
    const bool expect_stream = std::strcmp(argv[1], "stream") == 0;
    if (snap && std::strcmp(snap, "1") == 0 && bind_calls != 0)
        fail = "RX_VK_UB_SNAPSHOT=1 must not ask gpu_buffer_bind (the switch copies everything, as before)";
    if (expect_stream != streamed)
        fail = expect_stream ? "expected a stream copy, got the in-place bind"
                             : "expected the in-place bind, got a stream copy";
    if (fail) { std::printf("FAIL %s: %s\n", argv[1], fail); return 1; }
    std::printf("PASS %s\n", argv[1]);
    return 0;
}
'''


def check_source():
    draw = (ROOT / "gpu/vk/vk_draw.cpp").read_text(encoding="utf-8")
    mem = (ROOT / "gpu/vk/vk_memory.cpp").read_text(encoding="utf-8")
    internal = (ROOT / "gpu/vk/vk_internal.h").read_text(encoding="utf-8")
    # (the binding says what it is: the live tag on BufferRef, set by the direct paths - the pool's guest memory,
    # read at execute)
    need("vk_internal.h: BufferRef::live", block(internal, "struct BufferRef {"),
         ["bool live = false;", "issue 1795", "no mirror at the read"])
    # (the three direct paths of gpu_buffer_bind tag their bindings; the mirror returns do not - the tag's presence
    # IS the per-binding key)
    bind = block(mem, "BufferRef gpu_buffer_bind(")
    need("vk_memory.cpp: the tagged direct paths", bind,
         ["return remember({p->buffer, off, size, 0, mirror_upload});"])
    if bind.count("mirror_upload});") < 3:
        raise SystemExit("vk_memory.cpp: gpu_buffer_bind tags %d direct paths, the three are the states without a "
                         "mirror (no mirrors at all, the budget's refusals, a make queued or deferred)"
                         % bind.count("mirror_upload});"))
    # (the selection: the switch still copies everything on =1; unset, the per-binding rule governs)
    need("vk_draw.cpp: the per-binding snapshot", draw,
         ['static const bool snapshot = env_on("RX_VK_UB_SNAPSHOT");',
          "BufferRef b = snapshot ? BufferRef{} : gpu_buffer_bind(u.addr, n, false);",
          "if (b && b.offset % align == 0 && !b.live) return {b.buffer, b.offset, n};"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="g++")
    args = parser.parse_args()
    check_source()
    draw = (ROOT / "gpu/vk/vk_draw.cpp").read_text(encoding="utf-8")
    internal = (ROOT / "gpu/vk/vk_internal.h").read_text(encoding="utf-8")
    inc = {
        "ub_bufferref.inc": block(internal, "struct BufferRef {") + ";\n",
        "ub_select.inc": cut(draw, 'static const bool snapshot = env_on("RX_VK_UB_SNAPSHOT");',
                             "return {cur->stream, at, n};") + "\n        return {cur->stream, at, n};\n",
    }
    compiler = os.environ.get("CXX") or args.cxx
    with tempfile.TemporaryDirectory(prefix="ub-snapshot-") as tmp:
        folder = Path(tmp)
        for name, text in inc.items():
            (folder / name).write_text(text + "\n", encoding="utf-8")
        (folder / "ub_fixture.cpp").write_text(FIXTURE, encoding="utf-8")
        binary = folder / "fixture.exe"
        failed = []
        for case, snap, bind, expect in CASES:
            exe = folder / ("fixture_" + case)
            subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-include", ENV_ON_H,
                            "-I", str(folder), str(folder / "ub_fixture.cpp"), "-o", str(exe)], check=True)
            env = dict(os.environ)
            env.pop("RX_VK_UB_SNAPSHOT", None)
            if snap is not None:
                env["RX_VK_UB_SNAPSHOT"] = snap
            # (one scenario a process: the switch's static reads the environment once)
            r = subprocess.run([str(exe), expect, bind], capture_output=True, text=True, timeout=60, env=env)
            print(r.stdout, end="")
            print(r.stderr, end="", file=__import__("sys").stderr)
            if r.returncode:
                failed.append(case)
        if failed:
            raise SystemExit("FAILED: " + ", ".join(failed))


if __name__ == "__main__":
    main()
