// The dGPU policy's decisions (RX_VK_DGPU=1, gpu/vk/vk_memory.cpp): rules apart from Vulkan so tests/test_dgpu_*.py
// can run them, as vram_pressure.h and movie_upscale_policy.h do for theirs.
//
// A PC game engine owns its GPU: it uploads what the GPU reads often into VRAM once, renders into device-local
// targets, and reads results back only where it must. Hookshot instead mirrors whole guest pools into VRAM by
// default and keeps the copies coherent around every submission, because the console's unified memory says the CPU
// may read anything at any time. On a dGPU at 4K that coherence is the frame's cost (600 submits: 3879 write-back
// diff runs, 3344.4 MB compared; mirror syncs 782 dirty pages from captures against 173553 from earlier; 199 of
// 3000 readbacks waited 16.78 ms each; the GPU sat near idle while 1080p held 60 fps and 4K fell to 35.6). The
// policy turns that around: every pool starts bound in guest memory - the GPU reads and writes it where it is,
// nothing copied, nothing compared - and a pool earns a mirror only when a measured trial proves the mirror buys
// GPU time. What a PC engine does by hand, measured pool by pool.
#pragma once

#include <cstdint>
#include <cstdlib>

namespace dgpu {

// A promotion trial for one pool, phases on the game's own frames:
// Base - the pool in guest memory, its window's GPU time kept as the before;
// Mirror - the caller gives the pool its mirror (made at the pool's next binding, as stock makes one) and the
//          phase settles: the make copies the whole pool into it, and the first frames on new buffer addresses are
//          the make's own cost, not the pool's;
// Measure - the same GPU time with the mirror, the after.
// One trial at a time (a second's numbers would read against a frame the first was still moving; Triforce's
// demotion ran one the same way). The caller feeds every submission's frame number and the GPU time total
// (gpu_time_total_ns, vk_replay.cpp: the recordings' own TOP/BOTTOM timestamps, always on, and never reset - the
// 120-frame status line resets st_gpu_ns, the trials' deltas must not see that).
enum class Act : uint8_t {
    None,     // the phase runs on
    Mirror,   // the before is measured: give the pool its mirror now
    Decide,   // the after is measured: compare with mirror_worth and keep the mirror or release it
};

struct Trial {
    enum Phase : uint8_t { Idle, Base, Settle, Measure };
    Phase phase = Idle;
    uint64_t lo = 0;          // the pool in trial (its guest start; 0 while none)
    uint64_t f0 = 0;          // the frame the phase began at
    int64_t g0 = 0;           // the GPU time total when it began
    double direct_ms = 0;     // the GPU's ms a frame with the pool in guest memory
    double mirror_ms = 0;     // the same with its mirror

    // The windows, in frames. 120 is some 3.5 s at 35 fps - long enough that a momentary stall in one does not
    // decide; 12 of settle covers the make's copy (a pool of a few hundred MB copies in a few ms of GPU time) and
    // the first frames' pipeline warm-up. Triforce measured with 10 and 90; the wider windows cost a few seconds
    // per trial and buy a verdict that holds.
    static constexpr uint32_t kSettle = 12, kWindow = 120;

    void start(uint64_t pool_lo, uint64_t frame, int64_t gpu_ns) {
        lo = pool_lo, phase = Base, f0 = frame, g0 = gpu_ns, direct_ms = 0, mirror_ms = 0;
    }
    void stop() { phase = Idle, lo = 0; }

    // The GPU's ms a frame over the phase so far (a frame count of zero: nothing measured yet).
    double ms(uint64_t frame, int64_t gpu_ns) const {
        return frame > f0 ? (double)(gpu_ns - g0) / (double)(frame - f0) / 1e6 : 0.0;
    }

    Act step(uint64_t frame, int64_t gpu_ns) {
        if (phase == Idle) return Act::None;
        if (phase == Base && frame >= f0 + kWindow) {
            direct_ms = ms(frame, gpu_ns);
            phase = Settle, f0 = frame, g0 = gpu_ns;
            return Act::Mirror;
        }
        if (phase == Settle && frame >= f0 + kSettle) {
            phase = Measure, f0 = frame, g0 = gpu_ns;
            return Act::None;
        }
        if (phase == Measure && frame >= f0 + kWindow) {
            mirror_ms = ms(frame, gpu_ns);
            phase = Idle;   // (the verdict is the caller's: mirror_worth below; stop() clears lo)
            return Act::Decide;
        }
        return Act::None;
    }
};

// The verdict: the mirror keeps the pool only when it is clearly faster, a gain both in percent and in
// milliseconds, so a noise-level 1% win does not buy a pool a mirror and the sync around it for good. The knobs
// (RX_VK_DGPU_GAIN, a percent, 10; RX_VK_DGPU_FLOOR, ms, 0.25) are read in vk_memory.cpp and passed here, so this
// stays pure. Why both, as whichever is larger: the percent alone keeps a mirror for 0.05 ms of 5 (noise), the
// floor alone for 0.25 ms of 20 (nothing a frame would miss) - and neither should gate the other: 1.09 ms saved
// of a 10.32 ms frame (10.6%) is a real win at the 10% knob even though it is under 10% + 0.25 ms. Measured in
// the game: that pool's verdict is in the race's logs. The window's own noise (a load, a menu) is held off by the
// caller, which starts a trial only on a sane baseline.
inline bool mirror_worth(double direct_ms, double mirror_ms, double gain_pct, double floor_ms) {
    const double saved = direct_ms - mirror_ms;
    return saved > direct_ms * gain_pct / 100.0 && saved > floor_ms;
}

// A trial candidate, by its window: enough guest-memory reads to be worth measuring (a pool the GPU barely reads
// would only ever measure noise, and its verdict would follow the pool for good in the skip list), and not
// write-dominated (its writes went through guest memory too: a pool the GPU renders into lives best where it is,
// its writes land in the pool the CPU reads, and out of the mirrors such pools cost only GPU write time -
// Triforce measured ~1 ms a frame and no gain, while a mirror would buy the write-back's diff back). One
// megabyte a window, of the several hundred MB a heavy pool gathers: above the noise, under everything real.
// The caller has already filtered retired pools and pools tried before.
inline bool trial_candidate(uint64_t read_bytes, uint64_t wrote_bytes) {
    return read_bytes >= (1ull << 20) && wrote_bytes < read_bytes;
}

// A skip-list line: one hex pool start, one a line, as Triforce's keep list keeps them; anything from '#' on is a
// note. False for a blank or commented line, so the reader skips it without stopping.
inline bool keep_line(const char* s, uint64_t& lo) {
    while (*s == ' ' || *s == '\t' || *s == '\r') s++;
    if (*s == '\0' || *s == '\n' || *s == '#') return false;
    char* end = nullptr;
    const unsigned long long v = strtoull(s, &end, 16);
    if (end == s || v == 0) return false;
    lo = (uint64_t)v;
    return true;
}

}   // namespace dgpu
