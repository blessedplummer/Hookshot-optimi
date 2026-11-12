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
    t.start(0x1234ull, 1000, 0);           // (frame 1000, present total 0 ns)
    CHECK(t.phase == P::Base && t.lo == 0x1234ull);
    // (the base window runs kWindow frames; nothing before that)
    CHECK(t.step(1000 + Trial::kWindow - 1, 1) == Act::None);
    // (over the window, 2.0 ms a frame: 240e6 ns over 120 frames)
    CHECK(t.step(1000 + Trial::kWindow, 240'000'000ll) == Act::Mirror);
    CHECK(t.phase == P::Settle && t.direct_ms == 2.0);
    // (the settle waits for the mirror to really be on: with the mirror not bound, it holds)
    CHECK(t.step(1000 + Trial::kWindow + Trial::kSettle, 0, false) == Act::None);
    CHECK(t.phase == P::Settle);
    // (a whole window of waiting with the mirror still not bound: no verdict, the pool is free to trial again)
    CHECK(t.step(1000 + Trial::kWindow + Trial::kSettle + Trial::kWindow, 0, false) == Act::Abort);
    CHECK(t.phase == P::Idle);
    // (a fresh trial: the mirror comes on in time, the settle turns into the measure once the make's copy is done)
    t.start(0x4242ull, 1000, 0);
    CHECK(t.step(1000 + Trial::kWindow, 240'000'000ll, false) == Act::Mirror);
    CHECK(t.step(1000 + Trial::kWindow + Trial::kSettle - 1, 0, true) == Act::None);   // (still settling)
    CHECK(t.step(1000 + Trial::kWindow + Trial::kSettle, 0, true) == Act::None);
    CHECK(t.phase == P::Measure);
    // (with the mirror, 1.0 ms a frame of present-to-present: 180e6 ns over the window)
    CHECK(t.step(1000 + Trial::kWindow + Trial::kSettle + Trial::kWindow, 180'000'000ll, true) == Act::Decide);
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

    // ---- a candidate, by its window: reads that could pay for the verdict's floor, not write-dominated ----
    CHECK(trial_candidate(6764ull << 20, 0));         // (the measured heavyweight: 6.7 GB a window of reads)
    CHECK(!trial_candidate(2'000'000, 0));            // (the measured marginal: 2 MB a window on a 48 MB pool began
                                                      //   three times a run and could never win - no verdict is
                                                      //   reachable below the floor's time budget)
    CHECK(trial_candidate(256ull << 20, 0));          // (the floor itself: in)
    CHECK(!trial_candidate((256ull << 20) - 1, 0));   // (a byte under it: out)
    CHECK(!trial_candidate(2'000'000'000, 2'000'000'000));   // (wrote at least what it read: render target)
    CHECK(!trial_candidate(2'000'000'000, 3'000'000'000));

    // ---- the scene's anchors: matched by guest-range overlap, not by pool id ----
    dgpu::Anchor anchors[] = {{0x1000'0000ull, 0x2000'0000ull}, {0x4000'0000ull, 0x4100'0000ull}};
    CHECK(anchor_hit(0x1080'0000ull, 0x1180'0000ull, anchors, 2));   // (a recycled range: the allocator re-imported
                                                                     //   the first anchor's memory as a new pool at a
                                                                     //   shifted, smaller size - the same anchor)
    CHECK(anchor_hit(0x1000'0000ull, 0x2000'0000ull, anchors, 2));   // (the same pool, same range)
    CHECK(!anchor_hit(0x3000'0000ull, 0x3800'0000ull, anchors, 2));  // (a genuinely different range: a new scene's pool)
    CHECK(!anchor_hit(0x4100'0000ull, 0x4200'0000ull, anchors, 2));  // (touching, not overlapping)

    // ---- the A/B's validity: a substantial fraction of the anchors moved, or the trial pool's own bind volume -
    // counted on every serving path (the mirror's included) - moved over 2x. Straddlers do not void ----
    CHECK(!ab_voided(24000, 24000, 0, 30));    // (the measured scene, honestly counted: the mirror serves the reads
                                               //   and the binds continue)
    CHECK(!ab_voided(24000, 24000, 1, 30));    // (one anchor moved of 30: a streaming guest - measured "1 pools left
                                               //   the scene, 2 entered" on every void of K/L/M)
    CHECK(!ab_voided(24000, 24000, 2, 30));    // (two of 30: still streaming, not a scene change)
    CHECK(!ab_voided(1000, 1900, 0, 30));      // (1.9x the binds: under the line)
    CHECK(ab_voided(1000, 2500, 0, 30));       // (2.5x the binds: a menu or a LOD switch moved the workload)
    CHECK(ab_voided(1000, 300, 0, 30));        // (honestly 3x down: the workload moved, void even with the same scene)
    CHECK(ab_voided(24000, 24000, 20, 30));    // (most of the anchors moved: a scene change voids at any volume)
    CHECK(!ab_voided(1000, 1000, 2, 30));      // (2 of 30: 6.7%, under the quarter)
    CHECK(ab_voided(1000, 1000, 2, 3));        // (2 of 3: most of a small set - the >= 2 minimum does not save it)
    CHECK(!ab_voided(1000, 1000, 1, 3));       // (1 of a small set: the minimum keeps a lone straddler from voiding)
    // (and the recycled pool does not void: its range hits an anchor, so it is not counted as a change at all -
    // anchor_hit above; the measured case: 0x109a's range back as 0x7fd3, binds 23814 -> 24009, was voided by id)

    // ---- a skip-list line: one hex start, one a line, '#' a note; a '-' start or trailing junk is refused ----
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
    CHECK(!keep_line("-1a2b\n", lo));                              // (strtoull would read a wrap-around number)
    CHECK(!keep_line("1a2bxyz\n", lo));                            // (the junk would file a pool that was never measured)

    // ---- a trial that started over records its own numbers cleanly ----
    t.start(0x42ull, 500, 1'000'000'000ll);
    CHECK(t.lo == 0x42ull && t.phase == P::Base && t.direct_ms == 0 && t.mirror_ms == 0);
    t.stop();
    CHECK(t.phase == P::Idle && t.lo == 0);

    std::printf("PASS: the dGPU policy's rules\n");
    return 0;
}
