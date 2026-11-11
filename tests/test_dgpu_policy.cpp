// The dGPU policy's rules (gpu/vk/dgpu_policy.h): tests/test_dgpu_policy.py compiles and runs this.
#include "../gpu/vk/dgpu_policy.h"

#include <cstdio>
#include <cstring>

#define CHECK(x) do { if (!(x)) { std::printf("FAIL line %d: %s\n", __LINE__, #x); std::exit(1); } } while (0)

int main() {
    using namespace dgpu;
    using P = Trial::Phase;

    // ---- the trial state machine: one at a time, phases on the frames the caller feeds ----
    Trial t;
    CHECK(t.phase == P::Idle);
    CHECK(t.step(1000, 0) == Act::None);   // (nothing in trial: no act, whatever the frames)
    t.start(0x1234ull, 1000, 0);           // (frame 1000, GPU time 0 ns)
    CHECK(t.phase == P::Base && t.lo == 0x1234ull);
    // (the base window runs kWindow frames; nothing before that)
    CHECK(t.step(1000 + Trial::kWindow - 1, 1) == Act::None);
    // (over the window, 2.0 ms a frame of GPU time: 240e6 ns over 120 frames)
    CHECK(t.step(1000 + Trial::kWindow, 240'000'000ll) == Act::Mirror);
    CHECK(t.phase == P::Settle && t.direct_ms == 2.0);
    // (the settle does not measure; it waits out the make and the first frames on the new addresses)
    CHECK(t.step(1000 + Trial::kWindow + Trial::kSettle - 1, 0) == Act::None);
    CHECK(t.step(1000 + Trial::kWindow + Trial::kSettle, 0) == Act::None);
    CHECK(t.phase == P::Measure);
    // (with the mirror, 1.0 ms a frame: 180e6 ns over the window)
    CHECK(t.step(1000 + Trial::kWindow + Trial::kSettle + Trial::kWindow, 180'000'000ll) == Act::Decide);
    CHECK(t.phase == P::Idle);   // (the verdict is the caller's, the trial already idle)
    CHECK(t.mirror_ms == 1.5);   // (180e6 over 120 frames)

    // ---- the verdict: a gain in percent AND in milliseconds, whichever asks more ----
    CHECK(mirror_worth(2.0, 1.5, 10.0, 0.25));        // (25% and 0.5 ms saved: over both)
    CHECK(!mirror_worth(2.0, 1.85, 10.0, 0.25));      // (7.5% and 0.15 ms: under both)
    CHECK(!mirror_worth(20.0, 19.6, 10.0, 0.25));     // (2% and 0.4 ms of a long frame: the percent says no)
    CHECK(mirror_worth(20.0, 16.0, 10.0, 0.25));      // (20% and 4 ms: keeps it)
    CHECK(mirror_worth(10.32, 9.23, 10.0, 0.25));     // (the measured case: 10.6% and 1.09 ms, both over the line;
                                                      //   the additive form wrongly refused this one)
    CHECK(!mirror_worth(2.0, 1.79, 10.0, 0.25));      // (10.5% but 0.21 ms: under the floor)
    CHECK(mirror_worth(2.0, 1.70, 10.0, 0.25));       // (15% and 0.30 ms: over both)
    CHECK(!mirror_worth(2.0, 2.0, 10.0, 0.25));       // (no faster: no)
    CHECK(!mirror_worth(2.0, 1.99, 10.0, 0.25));      // (0.5% and 0.01 ms: under both)
    CHECK(mirror_worth(2.0, 1.7, 0.0, 0.25));         // (gain 0: the floor alone can still say yes)
    CHECK(!mirror_worth(2.0, 1.79, 0.0, 0.25));
    CHECK(mirror_worth(2.0, 1.74, 10.0, 0.25));       // (13% and 0.26 ms: over both, kept)

    // ---- a candidate, by its window: reads above the noise floor, not write-dominated ----
    CHECK(trial_candidate(2'000'000, 0));             // (2 MB read, none written)
    CHECK(!trial_candidate(500'000, 0));              // (under 1 MB: noise, and its verdict would follow it for good)
    CHECK(!trial_candidate(2'000'000, 2'000'000));    // (wrote at least what it read: render target)
    CHECK(!trial_candidate(2'000'000, 3'000'000));

    // ---- a skip-list line: one hex start, one a line, '#' a note ----
    uint64_t lo = 0;
    CHECK(keep_line("1a2b3c4d\n", lo) && lo == 0x1a2b3c4dull);
    CHECK(keep_line("0xdeadbeef\n", lo) && lo == 0xdeadbeefull);   // (an 0x prefix reads the same)
    CHECK(keep_line("  ff00  # the scene's light constants\n", lo) && lo == 0xff00ull);
    CHECK(keep_line("ff00\r\n", lo) && lo == 0xff00ull);
    CHECK(!keep_line("\n", lo));
    CHECK(!keep_line("# a note\n", lo));
    CHECK(!keep_line("   \n", lo));
    CHECK(!keep_line("0\n", lo));                                  // (0 is no pool's start)
    CHECK(!keep_line("nothex\n", lo));

    // ---- a trial that started over records its own numbers cleanly ----
    t.start(0x42ull, 500, 1'000'000'000ll);
    CHECK(t.lo == 0x42ull && t.phase == P::Base && t.direct_ms == 0 && t.mirror_ms == 0);
    t.stop();
    CHECK(t.phase == P::Idle && t.lo == 0);

    std::printf("PASS: the dGPU policy's rules\n");
    return 0;
}
