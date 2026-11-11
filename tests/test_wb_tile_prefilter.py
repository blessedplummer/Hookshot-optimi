"""GPU tile-dirty prefilter (Phase 2, gpu/vk/vk_memory.cpp): 64 KB tiles, the sparse dirty set, the tile index and the
chunk-keep predicate.

A compiled fixture driving the production pieces: WbdTileSet (absolute guest 64 KB tiles marked over byte ranges,
 dirtied tiles read back, unknown tiles clean), wbd_tile_of (the address to tile index shift), wbd_tile_keep (the
intersect predicate: fallback keeps everything, pending pages and unknown tiles are always kept, otherwise only dirty
tiles become chunks) and wbd_tile_prefilter_on (the kill switch, then the subgroup-vote gate's fallback).

CPU-compiled, no game/GPU.

python -I -B tests/test_wb_tile_prefilter.py [--cxx g++]
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
    parser.add_argument('--source', type=Path, default=ROOT / 'gpu/vk/vk_memory.cpp')
    args = parser.parse_args()
    text = args.source.read_text(encoding='utf-8')
    tile_set = block(text, 'struct WbdTileSet {') + ';'
    tile_of = block(text, 'inline uint64_t wbd_tile_of(')
    keep = block(text, 'inline bool wbd_tile_keep(')
    gate = block(text, 'inline bool wbd_tile_prefilter_on(')
    fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <set>
#define CHECK(ok) do { if (!(ok)) { std::fprintf(stderr, "failed line %d: %s\n", __LINE__, #ok); std::exit(1); } } while (0)
''' + tile_set + '\n' + tile_of + '\n' + keep + '\n' + gate + r'''
int main() {
    // (tiles are 64 KB: wbd_split's max vector chunk; the index is the address shifted)
    CHECK(WbdTileSet::kTile == (64u << 10) && WbdTileSet::kShift == 16);
    CHECK(wbd_tile_of(0) == 0 && wbd_tile_of(0xFFFF) == 0);
    CHECK(wbd_tile_of(0x10000) == 1 && wbd_tile_of(0x1FFFF) == 1 && wbd_tile_of(0x20000) == 2);
    WbdTileSet m;
    CHECK(!m.dirty(7));   // (empty: nothing dirty)
    m.mark_range(0x10000, 0x10000);   // (exactly one tile)
    CHECK(!m.dirty(0) && m.dirty(1) && !m.dirty(2));
    m.mark_range(0x1FFF0, 0x20);   // (a range straddling two tiles dirties both)
    CHECK(m.dirty(1) && m.dirty(2));
    m.mark_range(0x50000, 0);   // (empty: nothing)
    CHECK(!m.dirty(5));
    CHECK(!m.full());   // (three tiles: nowhere near the runaway)
    m.clear();
    CHECK(!m.dirty(1) && !m.dirty(2));
    // (the keep predicate: the fallback keeps everything; pending pages are never culled; unknown tiles still need
    // their fill and compare; otherwise only dirty tiles become chunks)
    CHECK(wbd_tile_keep(false, true, false, true));
    CHECK(wbd_tile_keep(false, false, false, true));
    CHECK(wbd_tile_keep(false, true, true, false));   // (pending: kept even when clean and known)
    CHECK(wbd_tile_keep(true, true, true, false));
    CHECK(wbd_tile_keep(false, false, false, false));   // (unknown: kept even when clean)
    CHECK(wbd_tile_keep(true, false, false, false));
    CHECK(wbd_tile_keep(true, true, false, false));   // (dirty and known: kept)
    CHECK(!wbd_tile_keep(false, true, false, false));   // (clean and known: skipped)
    // (the gate: the kill switch first, then the subgroup-vote fallback)
    CHECK(wbd_tile_prefilter_on(true, true));
    CHECK(!wbd_tile_prefilter_on(false, true));   // (RX_VK_DIRTY_ORIGIN=0: off even with votes)
    CHECK(!wbd_tile_prefilter_on(true, false));   // (no subgroup votes: whole-chunk dispatch, as before)
    CHECK(!wbd_tile_prefilter_on(false, false));
    std::puts("PASS: 64 KB tiles marked per range; only dirty tiles kept, pending and unknown always; "
        "the kill switch and the vote gate fall back to whole chunks");
}
'''
    with tempfile.TemporaryDirectory(prefix='wb-tile-prefilter-') as tmp:
        src = Path(tmp) / 'tile.cpp'
        exe = Path(tmp) / 'tile.exe'
        src.write_text(fixture, encoding='utf-8')
        subprocess.run([args.cxx, '-std=c++20', '-O1', '-Wall', '-Wextra', '-Werror', str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
