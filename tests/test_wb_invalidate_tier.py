"""Invalidate's wait tier (Phase 2, gpu/vk/vk_internal.h): URGENT, with a one-time full-table fallback.

A compiled fixture driving the production piece: wb_invalidate_tier (an invalidate waits on the URGENT value when the
urgent table ever reached the range; a region so far written back bulk-only — large, never read back — waits on the
full value once and notes the readback, so the next submission's write-back goes urgent and later reads hit urgent;
no wait when neither table ever reached it). This pins the first-read classification lag: the first read of a
bulk-only region cannot be urgent yet, so it falls back instead of reading stale guest memory.

CPU-compiled, no game/GPU.

python -I -B tests/test_wb_invalidate_tier.py [--cxx g++]
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

RUN_ALL_ARGS = ["--cxx", "g++"]

ROOT = Path(__file__).resolve().parents[1]


def block(text, marker):
    start = text.index(marker)
    end = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cxx', default='g++')
    parser.add_argument('--source', type=Path, default=ROOT / 'gpu/vk/vk_internal.h')
    args = parser.parse_args()
    text = args.source.read_text(encoding='utf-8')
    enum = block(text, 'enum class WbWaitTier') + ';'
    tier = block(text, 'inline WbWaitTier wb_invalidate_tier(')
    fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
''' + enum + '\n' + tier + r'''
int main() {
    CHECK(wb_invalidate_tier(true, false) == WbWaitTier::Urgent);   // (readback pages, small results: urgent)
    CHECK(wb_invalidate_tier(true, true) == WbWaitTier::Urgent);   // (urgent wins when both reached it)
    // (the lag, pinned: a first read of a bulk-only region falls back to the full value once and notes the readback;
    // the note makes the next write-back urgent, so the second read is Urgent, not FullThenNote again)
    CHECK(wb_invalidate_tier(false, true) == WbWaitTier::FullThenNote);
    CHECK(wb_invalidate_tier(false, false) == WbWaitTier::None);   // (never written back: no wait, no note)
    std::puts("PASS: urgent hits wait urgent; a first read of a bulk-only region waits full once and notes it; "
        "never-written-back ranges wait on nothing");
}
'''
    with tempfile.TemporaryDirectory(prefix='wb-invalidate-tier-') as tmp:
        src = Path(tmp) / 'tier.cpp'
        exe = Path(tmp) / 'tier.exe'
        src.write_text(fixture, encoding='utf-8')
        subprocess.run([args.cxx, '-std=c++20', '-O1', '-Wall', '-Wextra', '-Werror', str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
