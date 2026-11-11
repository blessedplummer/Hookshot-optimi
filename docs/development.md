# Hookshot (oot-recomp)

**Hookshot** is the port's name (as Skybound is TOTKPort's and BlueShell is mkw-recomp's); the maintainer's repository
folder stays `oot-recomp`. The game builds as `hookshot.exe` + `hookshot_vk.dll` (`hookshot` + `hookshot_vk.so` on Linux).

A static recompilation of the Switch 2 *The Legend of Zelda: Ocarina of Time* remake to native x86-64 Windows and Linux:
the game's code lifted to C (NotRexed's a64-kit, Rexcomp's AArch64 kit), the Horizon OS and NVN2 implemented
natively, the GPU work replayed on Vulkan. It is a fork of mkw-recomp (`..\mkw-recomp`, Mario Kart World), whose
paths it keeps so MKW's fixes apply here.

**No game files are in this repository.** The dump is read where it lies (`work/dump.txt`); everything made from it
(modules, image, lifted C, builds, runs) goes to `work/`, which git ignores.

Status: the game plays; the README's [status table](../readme.md#project-status) says how far. The design, its stages and
the lessons carried over from MKW: [design.md](design.md).

## Use

Requirements: Python 3 with `pip install -r requirements.txt` (`lz4`, `numpy`, `capstone`, `unicorn` 2.1,
`zstandard`, and the button-prompt packages); a compiler: on Windows llvm-mingw
(`HOOKSHOT_MINGW`, or an `llvm-mingw` folder in the clone), else emsdk's clang at `C:\emsdk` (or `EMSDK`) with Visual
Studio's C++ tools; on Linux clang and lld. For the Vulkan back end: distro Vulkan / SPIR-V / VMA headers under
`/usr`, or `HOOKSHOT_VULKAN_HEADERS` / `HOOKSHOT_SPIRV_HEADERS` / `HOOKSHOT_VMA_HEADERS`, or a CPM package cache
(`RX_CPM`, else `cpm` in the clone, else the old caches in mkw-recomp's `work/eg` or `..\Eglantine\.cache\cpm`).
Without those the standalone build stops. [CONTRIBUTING.md](../CONTRIBUTING.md#development-setup) has the rest.

Run everything with `python -I` from this folder.

| Command | What |
|---|---|
| `python -I nx2/nup.py <file.nup> --extract work/dump` | the `.nup` dump package to `work/dump/exefs` + `romfs` (newest title; ZBIC NSOs made plain by `nx2/unpack.py`) |
| `python -I nx2/dump.py --dump <folder with exefs/ and romfs/> --set` | what the dump is (Switch 1 or 2, ids, modules, SDK, NVN); `--set` records it in `work/dump.txt` |
| `python -I nx2/lift.py [--write]` | image + discovery + coverage (`work/image/coverage.txt`); `--write` emits the C to `work/gen` |
| `python -I nx2/census.py --nx2` | instruction census per module, after the Switch 2 forms |
| `python -I nx2/test_insn.py [--per 6 --states 12]` | every instruction form the game uses, Unicorn vs lifted C |
| `python -I nx2/test_leaf.py [--count 3000 --states 4] [--calls]` | random whole functions (or call trees), Unicorn vs recompiled native code on the real image |
| `python -I nx2/lift.py --standalone --write` then `python -I nx2/build.py --standalone --jobs 4` | the standalone game: `work/sa/build/hookshot.exe` + `hookshot_vk.dll` |
| `powershell -File scripts/run-vk.ps1 [-Window] [-Seconds 120] [-Name x]` | a muted test run on Vulkan, with screenshots in `work/runs/<name>/shots` |
| `python -I scripts/prof.py <stacks file>` | a profile sampled with `HOS_PROFILE=<seconds>` + `HOS_SAMPLE=<thread>` + `HOS_SAMPLE_FILE=<stacks file>` (the sampler runs only with `HOS_PROFILE` set), named from the linker maps |

The dump can also come from `OOT_DUMP` (its folder) or `OOT_EXEFS`; `OOT_WORK` moves the work folder.

The built game's own arguments (`image.bin`, `--romfs`, `--user`, `--trace`, `--prepare-shaders`) are in the README's
[Running the game from the command line](../readme.md#running-the-game-from-the-command-line).

Scripted test input (`HOS_INPUT_SCRIPT` and `HOS_INPUT_LIVE`) is applied before the window-focus check. A log saying
the game window is not in front does not mean those presses were discarded. `HOS_HEADLESS` and
`HOS_WINDOW_NOACTIVATE` change window creation, not this input routing. The game must still reach a state that reads
and responds to the supplied controller input.

Physical keyboard and mouse input stays with the foreground window. Physical controllers can also play in the
background when the existing "Controller in background" option or `HOS_INPUT_BACKGROUND=1` enables them;
`HOS_INPUT_BACKGROUND=0` overrides that option. With `HOS_INPUT_EXCLUSIVE=1` the script
alone drives the game until 5 s after its last press; without it the player's devices add to the script's presses. It
is not a focus override.

## Switches for testing and debugging

The running game reads its switches from the environment or from `hookshot_env.txt` beside `image.bin` (one
`NAME=VALUE` a line, `#` for notes; `HOS_ENV_FILE=<path>` reads that file instead). The environment goes first: a name
already set when the game starts keeps its value and the file's line for it is left out (the log says so). A switch
that stands in for a menu row lasts for its run; the options file keeps the player's value (`env_override` in
`gpu/vk/vk_menu.cpp`). Switches the shader translation reads are part of the program cache's stamp
(`disk_stamp` in `gpu/vk/vk_shader.cpp`; `tests/test_program_stamp_knobs.py` checks the translator's). The build reads its own from the
environment. The README has the ones players are asked to try (`RX_VK_ASTC_RGBA8`, `RX_VK_LOW_SPEC`,
`RX_VK_MOVIE_UPSCALE`, `RX_VK_SKIP_CODE`, `HOS_OOT_CHEATS`). There are several hundred more, most of them one-off
experiments: `grep -rnE '(getenv|env_on)\("' host gpu/vk` lists them, and the comment where each is read says what it
does. An on/off switch ("(set)" below) is on for `1` or any other value, and off when it is unset, empty, `0`, `false`,
`off` or `no` (`env_on` in `gpu/vk/compat/hookshot_env.h`, `kit.env_on` in Python); it used to be that any value turned
one on, `0` included. A switch that takes a number or a list reads its value as it says.

`RX_VK_MESH_NOCULL=<prefix,...>` turns off the culling in the task shaders whose native code key starts with one of
the prefixes. On a Steam Deck, `RX_VK_MESH_NOCULL=2c56dc1006942f74` avoids the same hang without leaving anything out:
that task shader walks a coarse depth grid cell by cell for every meshlet and ends with
`visible = (result & 1) | (c3[2320] & 4)`, bit 4 being the game's own "draw everything". The switch makes that answer
true, and dead code elimination removes the grid walk, so every meshlet is drawn and the rasterizer clips what is off
screen. It costs some GPU time on cards that cull quickly, so it is off unless set. The log says
"culling off, every meshlet drawn" for each task shader it changed.

### Running a test

| Switch | What it does |
|---|---|
| `HOS_HEADLESS` (set) | No window: the renderer draws offscreen, for screenshots and frame dumps. The menu's Save my pipeline list then opens nothing (no browser, folder or clipboard) |
| `HOS_WINDOW_SIZE=<w>x<h>` | The window's size in pixels (1280x720 by default, wider for an ultrawide picture) |
| `HOS_WINDOW_NOACTIVATE` (set) | The window opens behind the others and does not take the focus (Windows) |
| `HOS_FULLSCREEN=1` | Starts in fullscreen; the Insert menu's Fullscreen row is then left alone |
| `HOS_AUDIO=0` | No sound device is opened (the mixer still runs); `scripts/run-vk.ps1` sets it unless `-Sound` |
| `HOS_LANGUAGE=<code>` | The game's language for this run, a code from `language.txt` |
| `HOS_OUTPUT_RES=<width>x<height>`, `HOS_HANDHELD=1`, `HOS_ASPECT=<ratio>` | Over the options file: the output resolution (a size, as `output_res`), the handheld profile, a picture wider than 16:9 (2.3889 for 3440x1440) |
| `HOS_WIDE_MAX_PX=<pixels>` | The most pixels a picture wider or narrower than 16:9 takes (`host/hos_wide_size.h`): 5120x2160's by default; 8294400 is 3840x2160's, the cap before |
| `HOS_DYNRES=0`, `HOS_REALTIME_STEP=0`, `HOS_TEXT_SPEED=<0..3>` | Over the options file: Resolution 100% (non-zero: Auto), Game speed Per frame (1: Real time), Text speed |
| `RX_VK_FRAMEGEN=0`, `RX_VK_FRAMEGEN_MULT=<2..8>`, `RX_VK_PACE=0`, `RX_VK_UPSCALE=<0..2>` | No frame generation whatever the menu says; over the options file: its multiplier, frame pacing, the upscaling filter |
| `RX_VK_DEVICE=<n>` | The graphics card listed as `vk: device <n>` in the log, over the menu's GPU device |
| `HOOKSHOT_NO_UPDATE_CHECK=1` | No look for a newer release at the start |
| `HOS_TRACE=1` | Every Horizon call logged, as `--trace` does |
| `RX_BACKEND=<name>` | Unset or `vulkan`: `hookshot_vk.dll`; any other value: the WebGPU renderer, when the exe was built with one |
| `HOS_GPU_ALLOC_RETRY=0` | A model's material-block allocations from a full heap (the OptimizeChangeBank thread's two CPU buffers and two GPU-memory calls in f_804566e0) not tried in the heap's parents, as shipped (`host/oot_heap_retry.c`, issue 1697) |
| `HOS_GPU_ALLOC_TEST_FAIL=<n>` or `<gpu>,<cpu>` | A test: the first n of those allocations of each kind (or that many of the GPU memory and of the CPU buffers) refused by the heap they name, as a full heap would, so the retry runs |

### Input for scripted runs

| Switch | What it does |
|---|---|
| `HOS_INPUT_SCRIPT=<presses>` | Presses timed from the game's first controller read |
| `HOS_INPUT_LIVE=<file>` | A file the test appends lines of presses to, read every 30 ms; each line's times count from when it is read. A line `!cheat <n>` sends a Cheats page action, `!synclog` logs the NVN syncs (`!dump`, `!shots`, `!rdoc` and `!flasharm` are the WebGPU renderer's) |
| `HOS_INPUT_FRAME_CLOCK=1` | The script's time runs at 60 game frames a second instead of the wall clock |
| `HOS_INPUT_EXCLUSIVE`, `HOS_INPUT_BACKGROUND`, `HOS_INPUT_SCRIPT_BACKGROUND` (set) | As above; the last lets every device count while the window is behind another one |
| `RX_VK_MENU_SCRIPT=<seconds>:<key>,...` | Timed Insert menu keys: `INS`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `ESC`, `ENTER`, `TABL`, `TABR` |
| `RX_VK_UI_PROMPTS=pad` or `keys` | The menu's button prompts for a controller or the keys, whatever was used last (screenshots) |
| `RX_VK_UI_GAME_FONT=0` | The menu, the counter and the start-up screen in the included Libertinus, not the game's own font |

The presses are `<seconds>:<button>[+<button>...][/<ms>]`, separated by commas, for example `2:A,5.5:LUP/1500,9:ZL+R`.
The buttons are `A B X Y L R ZL ZR PLUS MINUS LS RS LEFT UP RIGHT DOWN`, and the sticks pushed to their edge `LUP
LDOWN LLEFT LRIGHT RUP RDOWN RLEFT RRIGHT`. A press is held for 200 ms unless `/<ms>` says otherwise. The names are
case-sensitive, and an unknown one is ignored.

### Pictures and frames

| Switch | What it does |
|---|---|
| `RX_VK_SHOTS=<dir>` | A PNG every `RX_VK_SHOT_EVERY` presents (300) as `vk_<present>.png`, at `RX_VK_SHOT_W` x `RX_VK_SHOT_H` (1280x720) |
| `RX_VK_FLASH=1` | The flash detector, with `RX_VK_SHOTS` and `RX_VK_SHOT_EVERY=1`: writes the frames around a flash and dumps the next one |
| `RX_VK_FRAMELOG=<file>` | Every frame shown, as pictures at 320x180, while `<file>.go` exists; `python -I scripts/framelog.py <file>` finds the flashes. Not `RX_VK_FRAME_LOG` (with the underscore: frame times, below) |
| `RX_VK_DUMP=<present>[+<count>][,...]` | Every step of the frame after that present as PNGs, in `dump_<present>` under `RX_VK_DUMP_DIR` (or `RX_VK_SHOTS`, or the current folder); `RX_VK_DUMP_STEPS=<lo>-<hi>` and `RX_VK_DUMP_PROGS=<key>,...` narrow it |
| `RX_VK_PASS_TRACE=1` | The passes of the last frames, logged at a flash or a dark frame (`RX_VK_PASS_TRACE_AT=<n>`: every n-th present) |
| `RX_VK_PROBE=1` or `2` | The centre texel after every pass: `probe_<frame>.txt` at a flash (1), or `probe_all_<n>.txt` at speed (2); `python -I scripts/probe.py` reads them |
| `RX_VK_DUMP_FAILED=<dir>` | The native code of every program that does not translate, for `nx2/lower_test.py` (game data: keep it local) |

### Time and profiles

| Switch | What it does |
|---|---|
| `HOS_PROFILE=<seconds>` | Every interval, the busiest threads in the log; it also starts the samplers below |
| `HOS_SAMPLE=<thread>` + `HOS_SAMPLE_FILE=<file>` | One thread's stacks, sampled every millisecond (needs `HOS_PROFILE`); `python -I scripts/prof.py <file>` reads them |
| `HOS_SAMPLE_ALL=<file>` | Every game and renderer thread every 2 ms while `<file>.go` exists (needs `HOS_PROFILE`); `scripts/crit.py`, `scripts/vkprof.py`, `scripts/hotlist.py` and `scripts/cpu_report.py` (the [CPU profile](cpu-profile.md): CPU time by category, thread and function, and what limits the frame) read it (on Linux with `RX_MAPS=<game folder>`: they name frames from its `hookshot` and `hookshot_vk.so`) |
| `HOS_CHAIN=<file>` | While `<file>.go` exists, every wait of a game thread that blocks, every signal that can end one, every sleep and every present, with the time, the thread, the object and the caller; written to `<file>` when `<file>.go` goes away (about 2 million records at most). `python -I scripts/chain.py <file> [<HOS_SAMPLE_ALL file>]` walks each frame back from its present (the thread that ran, the wait that stopped it, the thread whose signal ended it) and prints the frame's critical path by thread: running, wake-ups, sleeps, and, with a sample file of the same run, the functions on it |
| `HOS_THREAD_TIMES=<seconds>` | Every interval, the threads that used over 5% of a core |
| `HOS_FPS_LOG` (set), `HOS_PRESENT_TRACE=<csv>` | The game's frame limiter, VRR flags and nominal rate every 300 presents, and the game clock's speed (1.000: real time); a CSV line per game present with host-clock times before queue flushing, around the renderer call, after virtual-core reacquisition, and at the return counter. These are CPU wall times, not GPU completion or scanout; async rendering measures the enqueue call. Unset/empty trace adds no clock reads |
| `HOS_FPS_MAX=<61-1000>` | A frame rate cap for one run, over the Game frame rate cap row (1000 is Uncapped's) |
| `RX_VK_FRAME_LOG=<csv>` | A CSV line per present: frame, seconds, frame time in ms. Also works with `HOS_HEADLESS`: CPU intervals after submitting the offscreen copy, without swapchain/display waits. Not `RX_VK_FRAMELOG` (no underscore: pictures, above) |
| `RX_VK_PRESENTS_AHEAD=<1-8>` | How many presents the game may hand over ahead of the replay (2). Not `RX_VK_PRESENT_AHEAD` |
| `RX_VK_PRESENT_AHEAD=0` | Frame generation's copies into the window submitted each at its own time, as before #1702 (default on: a set's copies go to the GPU together). Not `RX_VK_PRESENTS_AHEAD` |
| `RX_VK_PASS_TIMING=1` or `2` | GPU time per pass (2: per program too), logged every 600 presents |
| `RX_VK_SLOW_LOG=<ms>` | The replay's times for each frame that took longer than this |
| `RX_VK_PRESENT_THREAD=1` | On (off by default: the game's thread presents). Where the device has a second queue that can present to the window (a second queue of the replay's family, else a compute family's): each present is made on a thread and queue of its own once the frame's copy into the window is done, and the game's thread goes on at once (with NVIDIA's driver on Wayland the present otherwise waits ~2.4 ms a frame for the GPU there). One present out at most; under V-Sync On (FIFO) with `VK_KHR_present_wait`, until it is shown. The log has the fence's and the present's time (and how long after it the frame was shown) every 600 presents |
| `HOS_PGO=<file>` | In a run of an `RX_PGO=gen` build: `<file>.reset` clears the counters, `<file>.dump` writes them to `<file>`. It is read before `hookshot_env.txt`, so set it in the environment |

### The GPU

| Switch | What it does |
|---|---|
| `RX_VK_VALIDATION` (set) | The Khronos validation layer |
| `RX_VK_CHECKPOINTS=0` | No NVIDIA checkpoints (on by default; after a lost device they name the draw it stopped at) |
| `RX_VK_HANG_DUMP=<file>` | Every thread's stack to the file once no frame was shown for `RX_VK_HANG_SEC` seconds (20) (Windows) |
| `RX_VK_GPU_HANG_SEC=<s>` | Work sent to the GPU that finishes nothing for this long (30) is a hang: the lost-device report, then the game ends. `0`: the stall is logged and the game waits |
| `RX_VK_NOOPT` (set) | Pipelines built without the driver's optimisations |
| `RX_VK_SKIP_PROG=<prefix>,...` | Leaves out the draws of these programs (default `53509044401d78d2`; `-` leaves out none) |
| `RX_VK_PIPE_SKIP=0`, `1` or `2` | Default/`1`: wait for shader translation; a pending pipeline can be skipped only for attachment-only draws whose targets hold an earlier frame. `0`: wait for every pipeline too. `2`: restore the old skip for untranslated draws with persistent targets; this can omit writes before the shader is known. Translated memory-writing draws still wait |
| `RX_VK_PRECOMPILE=0` or `1` | The shader preparation before the title off or on (on in builds made with `RX_RELEASE=1`, as the builders' are) |
| `RX_VK_TASK_MESH_FALLBACK=0` or `1` | Task + mesh draws through the compute fallback (1) or the driver's mesh shaders (0). By default the fallback is on for AMD's Navi 21 and Navi 22 on Windows (RX 6800, 6800 XT, 6900 XT, 6950 XT, 6700 XT), where the native path timed out the driver (issue 996) |
| `RX_VK_DRIVER_FAULTS=0` or `2` | 0: programs the driver's compiler faulted on are asked of it again. By default they are left out for the rest of the run and, through `hookshot_driver_faults.txt` beside the executable, on later runs with the same driver, and on AMD one program's builds go to the driver one at a time (issue 996). 2: one at a time on every driver |
| `RX_VK_GM_LOOP=0` or `1` | The storage buffer tables of a global memory access searched in a loop (1) or written out entry by entry (0). On by default on Intel, whose compiler did not finish the culling programs with the entries written out |
| `RX_VK_NO_PROGRAM_CACHE` (set) | Neither reads nor writes `hookshot_programs.bin` and the pipeline notes |
| `RX_VK_FETCH_BOUNDS=0` or `1`, `RX_VK_NAN_BITS=0` or `1`, `RX_VK_GATHER_OFFSET=0` or `1` | The translation's vendor and driver workarounds (`gpu/vk/vk_vendor.h`) off or on for one run, whatever the GPU; the log's `vk: vendor fixes` line says which are on. By default: out-of-bounds texel fetches answered with 0 on every GPU but NVIDIA's; NaN tests on a value's bits on AMD's older Windows shader compiler (RX 5000 and RX 6000); gathers nudged by 1/512 of a texel on AMD's and Intel's drivers. They change the shaders' translation (one preparation of the shaders) |
| `RX_VK_UB_WORD_HASH=0` | A/B test: use the old low-address-bit index for cached uniform words (default: mix upper address bits to reduce aligned-buffer collisions) |
| `RX_VK_MIRROR=0`, `RX_VK_MIRROR_MB=<MB>` | No VRAM mirrors of the game's memory pools (on for discrete cards); their budget |
| `RX_VK_DGPU=1` | The dGPU policy: every pool binds in guest memory - the GPU reads and writes the game's memory where it is, with no mirror made, no mirror sync and no write-back - and a pool the GPU reads heavily earns a mirror back through a measured trial (one at a time, the GPU's ms a frame before and after, one verdict a pool; a loss is kept in `hookshot_mirror_skip.txt` beside the game's own files, so later runs do not repeat it). Unset: stock mirrors |
| `RX_VK_DGPU_TRIALS=0`, `RX_VK_DGPU_GAIN=<pct>`, `RX_VK_DGPU_FLOOR=<ms>` | Under `RX_VK_DGPU=1`: no promotion trials (every pool stays in guest memory); the trial's verdict needs the mirror faster by this percent (10) and this many ms a frame (0.25) at once |
| `RX_VK_VRAM_MB=<MB>` | The texture cache's budget (default 70% of what the driver allows, at most 6 GB) |
| `RX_VK_COMPACT=0`, `RX_VK_COMPACT_PCT=<50..90>`, `RX_VK_COMPACT_MB=<MB>` | VRAM compaction (on by default; `=0` turns it off): the textures that are not render targets in a pool of their own, moved out of sparsely used blocks a few a present so that the blocks go back to the driver, and the texture budget holding the driver's count at that share of what it allows (85) by letting go of the render targets drawn over since and of textures unused for ten seconds, with a reserve five points under it (80) where those unused for thirty seconds go (what the budget's size, the 90% rule and the driver's limit ask for on their own still goes from textures unused for a second, as with it off); the free space that starts a run (512; 192 while the count is over 90%) |
| `RX_VK_REACTIVE=1`, `RX_VK_REACTIVE_SCALE=<0..64>`, `RX_VK_REACTIVE_MAX=<0..1>`, `RX_VK_REACTIVE_DEBUG=1` | A reactive mask for DLSS and FSR 3.1 only, not FSR 4 (`gpu/vk/vk_reactive.h`, off by default: not yet checked in the game): the scene's colour is copied before its blended draws that test depth without writing it (water, smoke, fire, glows), and where the scene differs from the copy at the upscale the upscaler leans on the current frame, so they smear and ghost less. The mask is the difference times the scale (1), at most the maximum (0.9); the debug switch shows it in the picture in magenta. The mask is the whole difference since the copy, so a blended draw after the last transparent run (a full-screen fade) is in it too. The log's `vk: reactive mask:` lines say whether one is made |
| `RX_VK_SKIP_COMPRESSED_OVER_RT=1` | A compressed (ASTC, BC, ETC2/EAC) texture made over storage render targets used is not uploaded, as before: bosses and new enemies all blue the first time |
| `RX_VK_NO_WRITEBACK` (set) | GPU writes to mirrored pools are not copied back to guest memory (an experiment, for A/B) |
| `RX_VK_WB_DIFF=0` | Write-backs copy every byte again instead of only what changed against a VRAM shadow (the diff is on by default; v2.0.9 and v2.0.10 had it off, issue 1161) |
| `RX_VK_WB_OBSERVE=1` | Opt-in diagnostics for actual mirror/shadow differences in the write-back diff pass. Reports completed chunks and bytes covered by changed chunks every 600 submissions; does not change copy decisions. See [write-back observations](writeback-observations.md) for interpretation and tests |
| `RX_VK_DLSS=0/1/2`, `RX_VK_DLSS_PRESET=<letter>`, `RX_VK_DLSS_LOG=1`, `RX_VK_REFLEX`, `RX_VK_REFLEX_LOG=1`, `RX_VK_ANTILAG=0/1` | NVIDIA DLSS and Reflex, and AMD Anti-Lag 2, for one run (`=-` on Reflex or Anti-Lag 2: its extensions not even enabled): the readme's NVIDIA DLSS section. `RX_VK_DLSS_TRACE=<frames>` logs one frame's large draws every that many frames, `RX_VK_DLSS_DEBUG=motion` shows the motion vectors, `=alternate` the game's own upscale every other frame. `RX_VK_DLSS_HDR=0` keeps DLSS's LDR feature and clamped colour with Native HDR too, for comparisons |
| `RX_VK_FSR4=0/1/2`, `RX_VK_FSR_DIR=<folder>`, `RX_VK_FSR4_HDR=0` | AMD FSR 4 for one run (0 never, 1 FSR 4, 2 its Native AA), and another folder for AMD's DLLs (not from `hookshot_env.txt`): the readme's AMD FSR 4 section. `RX_VK_FSR4=1/2` and `RX_VK_DLSS=1/2` go over the Upscaler row, a saved FSR 3.1 too where the engine they ask for runs; `RX_VK_FSR=1` goes over them (one upscaler at a time). The DLSS trace and debug switches above (and `RX_VK_DLSS_JITTER=0`) apply to FSR 4 too, and to FSR 3.1 on the chain, on the frame chain they share. `RX_VK_FSR4_HDR=0`: with HDR on Native, FSR 4 keeps its LDR context and the colour clamped at the paper white, as before (by default it takes the game's HDR colour as linear light while the game presents PQ) |
| `RX_VK_FSR3_CHAIN=0` | AMD FSR 3.1 upscales with inputs of its own (`vk_fsr3_inputs.cpp`: the camera's motion alone) instead of as an engine of `vk_temporal.cpp`'s chain (the skinned draws' own motion, as DLSS and FSR 4 get it), for comparisons. The log says `vk: fsr3 on the chain: N frames drawn` on the chain, `vk: fsr3 ... with assembled depth and camera motion` without |
| `RX_VK_ASYNC=1` | The replay on a thread of its own instead of the game's submitting thread |
| `RX_VK_DESC_REUSE=0/1/never` | Over the Descriptor reuse row for a run, with its values: 0 its Off (a draw keeps the descriptors of the draw before it while no registration at all was made), 1 its On (through registrations of slots it does not sample, the default); `never`: every draw builds its own |
| `RX_VK_NAN_GUARD=0` | No NaN guard on float colour outputs (on by default on every card: one NaN texel spreads through bloom into glowing blocks) |
| `RX_VK_FIFO=1`, `RX_VK_IMMEDIATE=1` | V-Sync On (FIFO) or Off (immediate) for one run, over the menu's V-Sync row |
| `HOS_VCORES=0`, `HOS_CORES=<1..8>`, `HOS_RESERVE_CORE=0` | No virtual cores; the cores the game sees (6); no core kept for the replay (one is, with 8 or more logical processors) |

### Building

A build reads and writes tens of gigabytes. When the game folder or the build files folder is on a hard drive,
`make_game.py` warns that an SSD is much faster and builds anyway (the builders ask before they start).

| Switch | What it does |
|---|---|
| `HOOKSHOT_JOBS=<n>` (or `--jobs`) | Compilers at once (default one per CPU thread, each started when the free memory holds it; `HOOKSHOT_MEM_GATE=0` turns that check off) |
| `HOOKSHOT_LIFT_JOBS=<n>` | Processes that write the lifted C and make its edits (default the cores, at most 16, one per 2 GB free) |
| `HOOKSHOT_KEEP_BUILD_FILES=1` | Keeps the lifted C and the objects after a build, so that the next one compiles only what changed |
| `HOOKSHOT_MINGW=<dir>`, `HOOKSHOT_MSVC=1` | The llvm-mingw to build with; the Visual Studio route instead |
| `RX_CPM=<dir>`, `WGPU_NATIVE=<dir>` | The CPM cache (Vulkan and SPIR-V headers, VMA, FFmpeg) and wgpu-native |
| `RX_TARGET=windows` | On Linux: the Windows game, with llvm-mingw's Linux build in `HOOKSHOT_MINGW` |
| `RX_RELEASE=1` | A release build (`make_game.py` sets it): no console window, the shaders prepared before the title |
| `RX_PGO=gen` or `RX_PGO=<file>` | The hot code counting its branches for a profile, or built with one (`make_game.py` passes the shipped profile) |
| `RX_HOT_OPT=<level>` | The hot pieces' optimisation level (default `O2`); `O1` for [a faster build for development](#a-faster-build-for-development) |
| `RX_MARCH=<level>` | The CPU level to build for (default this PC's: `x86-64-v3`, else `x86-64-v2`, else `x86-64`) |
| `RX_VK_ONLY=1` | Only the Vulkan back end, with no lift |
| `OOT_WORK`, `OOT_DUMP`, `OOT_EXEFS`, `OOT_ROMFS` | The work folder and the dump's folders, over `work/dump.txt` |

#### A faster build for development

The eight hot pieces (`code_hot_*.c`, the functions the game spends its time in) are built at `-O2` with the play
profile, their hot callees inlined. They are 81 MB of the 1.4 GB of lifted C but about a third of its compile time,
and each is one of the longest single compiles, so a build after a new lift cannot end before the slowest of them.
When you are checking that something works (the game starts, a menu, a screenshot, a log) and not how fast it runs,
build them as every other lifted file is built, at `-O1` and without the profile:

```
RX_HOT_OPT=O1 python -I nx2/build.py --standalone --jobs 8
```

`build.py` run on its own uses no profile unless `RX_PGO` is set. `make_game.py` always passes the shipped profile
(`work/hookshot_pieces.prof`), so `RX_HOT_OPT=O1` through `make_game.py` gives `-O1` with the profile, which saves less.
Run on its own, `build.py` also lacks the rest of `make_game.py`'s environment (`RX_RELEASE`, `A64_HOT_CHUNK`), so its
runtime objects differ from a `make_game.py` build's.

The objects are cached by their flags, so `-O1` and `-O2` objects can sit side by side, and `make_game.py` leaves
`RX_HOT_OPT` and `RX_PGO` out of its lift key. In the checkout's own `work/` folder, or with
`HOOKSHOT_KEEP_BUILD_FILES=1`, switching back and forth therefore neither translates the game again nor recompiles
anything built before. In any other `--work` folder, `make_game.py`'s clean-up keeps only the last build's objects and a
translation that fits them, so each switch translates the game again and recompiles the hot pieces.

What it saves, from the per-file compile times (`build_times.json`) of six earlier full builds on one 24-thread
desktop, with other work running (not a measured `-O1` build):

| | Hot pieces (`-O2`, profile) | The 72 other lifted files (`-O1`) |
|---|---|---|
| Compile time, all of them | 1,270-1,810 s | 2,820-3,750 s |
| Per MB of C | 16-22 s | 2.2-2.9 s |
| Longest single file | 300-800 s | ~360 s (`code_021_1.c`, giant functions) |
| Peak memory per compiler | 0.8-1.7 GB | 1.4-1.6 GB for a 24 MB file |

At the other files' rate the hot pieces should take about 200 s instead of about 1,500 s of compile time, about
a quarter less in all, and the build's longest file becomes an ordinary one, so a build after a new lift can end several
minutes sooner on a CPU with many threads. The peak memory hardly changes: the ordinary files set it already.
The hot code runs slower at `-O1`; the game still plays (the profile-counting build, `RX_PGO=gen`, already builds
the hot pieces at `-O1` and is played through to record the profile), but check that the frame rate is good enough for
what you test.

**Never use it for numbers:** no fps or frame-time measurements, build timings, hitch or stutter checks, profiles,
and no release or player build. Build those with the defaults.

## The pipeline state list

Every release ships a list of pipelines, merged from the sources in `assets/pipeline_states/`:

- `tour.bin.xz`, which only the pipeline tour (`scripts/pipeline_tour.py`) writes, replaced whole each time;
- `contrib/<name>.bin.xz`, one per contributed list, which people add.

Each has a note beside it (`<name>.md`) that says who recorded it, on what GPU, driver, build and settings, and the
route. [`assets/pipeline_states/README.md`](../assets/pipeline_states/README.md) has how to record and add a list. Each
record is a pipeline the game asked for: its shaders' hashes, the program variant's hash, and the target formats,
vertex formats and draw state it was made with (`nx2/pipeline_states.py` has the layout). It holds no game code: a
player's build turns the hashes back into shaders from their own `hookshot_shaders.bin`.

`nx2/make_game.py` merges the sources at every build into the game folder's `hookshot_pipeline_states_shipped.bin`. It
takes the tour's list first, then the contributed ones by name, and keeps each pipeline once. The same sources always
give the same bytes, and the first start's shader preparation builds those pipelines before the title. Without them a
new install builds them in play, and draws left out while they compile wash the first field load white for seconds
(v2.0.8 to v2.0.14 shipped none). The sources are xz-compressed, written with fixed settings, so that the repository's
history stays small. The current ones hold 11527 pipelines in about 175 KB.

Every release carries them:

- the AppImage with `assets/`: `scripts/linux/appimage.sh` stops without them, and `scripts/linux/check_player_build.sh`
  checks them with the image's Python;
- the Windows builder's `tools/assets/pipeline_states`: `scripts/release_parts.py package` stops when there are none or
  one does not read.

xz rather than zstd: the AppImage's Python is 3.12, whose standard library has `lzma` but not `compression.zstd`.

A game run writes `hookshot_pipeline_states.bin` beside the executable, adding each pipeline it makes in play. To add a
list, play on a build of the current source and add that file as a contributed list. `add` keeps only the pipelines no
other contributed list holds:

```sh
python -I nx2/pipeline_states.py add <name> <game folder>/hookshot_pipeline_states.bin --who ... --gpu ... --build ... --settings ... --route ...
python -I nx2/pipeline_states.py tour <tour lists>... --gpu ... --build ... --settings ... --route ...   # the tour's, replaced
python -I nx2/pipeline_states.py check                      # every source, and the merge, as CI and the release checks read them
python -I nx2/pipeline_states.py merge --out merged.bin     # the list make_game writes beside the game
```

A list depends on the settings it was recorded with: its records hold the target formats and draw state, so one recorded
at 1440p with DLSS shares few pipelines with one recorded at 360p.

Players send theirs to issue #2034 with **Save my pipeline list** (the menu's Debug tab, under Developer: `gpu/vk/pipeline_share.h` and
`vk_menu.cpp` `share_pipeline_list`; the builders: `nx2/pipeline_share.py`, which the Linux builder imports and the Windows
one runs with `--json`). It checks the list as `pipeline_states.py --check` does and writes
`hookshot-pipelines-<date>.zip` to the Downloads folder with exactly two files, the list and `hookshot_pipeline_report.txt`
(the issue's template filled in from the run or from the game folder's log and `hookshot_options.ini`), then copies the
template, shows the folder and opens the issue; never with `HOS_HEADLESS`. `tests/test_pipeline_share.py` checks the zip,
the template and the refusals for both. By hand: `python -I nx2/pipeline_share.py <game folder> [--out-dir <folder>]`.

Refresh the tour's list before each release, and after a change to the shaders' translation or to the state a pipeline
is made from. A record that no longer matches what play asks for builds a pipeline nothing uses, and the one play wants
compiles in play. When the record's format changes (its magic and record size, `MAGIC` and `SIZE` in
`nx2/pipeline_states.py`), the old lists no longer read and the release checks stop. Convert each source in place, or
record it again, and make a new tour list. The folder's layout stays the same.

## The options file

`hookshot_options.ini`, in the game folder, holds the Insert menu's rows as `<key>=<number>`: the row's name in lower
case with `_` for spaces (`frame_generation=2`, `invert_camera_x=1`), except Output resolution, which is `output_res`.
`gpu/vk/vk_menu.cpp` writes the whole file at every change (`save`), with `options_version=5`,
`microphone_device=<name>` when a microphone is chosen by name, and the controls (`pad_<button>` and `key_<button>`:
the README's [Controls](../readme.md#controls)). A value outside a row's range is clamped when it is read. Where the
number is not the obvious one:

| Key | Values |
|---|---|
| `quality_preset` | 0 Custom, 1 Minimum (4GB), 2 Low (6GB), 3 Balanced (8GB), 4 High (12GB), 5 Ultra (16GB+) |
| `gpu_device` | 0 Auto, n the n-th card with Vulkan 1.3 as the row lists them |
| `frame_generation` | 0 Off, 2 to 8 the multiplier |
| `resolution` | 0 Auto, 1 100%, 2 75%, 3 67%, 4 50% |
| `output_res` | A size, `<width>x<height>`: 2048x1152 (the game's own), 1920x1080, 2560x1440, 3840x2160, 5120x1440, 5120x2880, or any other with each side a multiple of 8 (rounded down), at least 640x360, at most 5120x2880's pixels, 5:4 to 32:9; anything else is the game's own. The numbers saved before (0 2048x1152, 1 2560x1440, 2 3840x2160, 3 1920x1080, 4 5120x1440, 5 5120x2880) are still read |
| `upscaling` | 0 Off, 1 Catmull-Rom, 2 Lanczos |
| `sharpness`, `rumble_strength`, `motion_sensitivity_x`, `motion_sensitivity_y` | percent |
| `mouse_sensitivity` | hundredths: 100 is 1.00 |
| `field_of_view` | 50 to 150 percent of the current vertical gameplay FOV; 100 is the original. Applies immediately, before projection and culling. The resulting vertical angle stays within 5 to 140 degrees |
| `robust_buffers` | 0 Auto (on), 1 Off, 2 On |
| `game_speed` | 0 Per frame, 1 Real time |
| `text_speed` | 0 Normal, 1 2x, 2 4x, 3 Instant |
| `microphone` | 0 Off, 1 Default, 2 and up a microphone (the name in `microphone_device` decides which) |
| `mouse_camera` | 0 As the stick, 1 Native |
| `layout_preset` | 0 Nintendo, 1 Xbox / PC, 2 Swap Face, 3 Custom |

The other rows are 0 Off and 1 On. Some are read at the start, before the menu exists, by the code that needs them:
`handheld_profile` and `output_res` (`host/nn_applet.c`), `ultrawide` (`host/hos_main.c`), `robust_buffers`
(`gpu/vk/vk_main.cpp`) and the controls (`host/nn_hid.c`).

`HOS_CONTROL=<socket path>` turns on the control bridge: a local socket a tool or coding agent drives the running game
through (inputs timed in frames, screenshots, waits for the game's state, the Insert menu's settings, quit), with
`scripts/hookshot_ctl.py` as its client. Off unless set; see [control.md](control.md).

## Layout

| Path | What |
|---|---|
| `docs/design.md` | why mkw-recomp's pipeline, the architecture, MKW's lessons as rules, stages, testing |
| `nx2/` | the lift pipeline and its tests (mkw-recomp's, made game-neutral; `kit.py` has the paths), and `make_game.py`, which the builders run |
| `host/` | the standalone Horizon runtime (TOTKPort's `hos/` as adapted by mkw-recomp), with the native NVN2 layer `nvn_native.c`; `host/posix` has the Win32 calls on Linux |
| `gpu/vk/` | the Vulkan back end (`hookshot_vk.dll`, `hookshot_vk.so`), with Eden's shader recompiler and sirit |
| `gpu/render/`, `gpu/third_party/` | the WebGPU renderer the Windows exe falls back to when it was built with wgpu-native, its op stream (`rx_nvn.h`), suyu's decoders |
| `builder/` | Hookshot Builder: the Windows app (.NET 8 WPF), its setup and MSI, and the Linux builder and launcher scripts |
| `scripts/` | test runs, the profile and log readers, the save copier (`oot_saves.py`), release tooling, `scripts/linux` (dependencies, AppImage) |
| `tests/` | the game-free checks, all run by `tests/run_all.py` |
| `third_party/` | a64-kit (Hookshot's few changes marked `(hookshot)`), libopus, zbic (Atmosphere's zstd with ZBIC, for unpacking only), SDL2's licence, Dear ImGui and its Vulkan backend, zstd's decoder, the Libertinus fonts |

License: GPL-3.0-or-later, as mkw-recomp: a64-kit's translator is suyu's (GPL-2.0-or-later) and Eden's code is
GPL-3.0.
