"""Render the terminal-style demo GIF from REAL slicegrep output.

Every figure on screen was captured from an actual run, not mocked up:
  core.py                       74,406 bytes ~= 18,602 tokens whole-file
  budgeted single-file query    ~350 tokens
  directory co-occurrence       ~579 tokens, 6 files searched, 3 matched
  natural-language query        ~638 tokens, auto-expanded
  PreToolUse hook on core.py    17,849 -> 2,195 tokens

PIL drops duplicate consecutive frames, so pauses are encoded as per-frame
durations rather than repeated frames. That keeps the file small AND is the
only way holds survive the encoder.
"""
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 940, 620
PAD = 22
LH = 21
TICK = 70                    # ms per animation frame

BG = (13, 17, 23)
BAR = (22, 27, 34)
FG = (201, 209, 217)
DIM = (110, 118, 129)
GREEN = (63, 185, 80)
CYAN = (86, 182, 194)
YELLOW = (210, 168, 82)
RED = (248, 113, 113)
MAG = (188, 140, 255)
WHITE = (240, 246, 252)

F = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 15)
FB = ImageFont.truetype("C:/Windows/Fonts/consolab.ttf", 15)

frames, durations = [], []
screen = []


def reset():
    del frames[:], durations[:], screen[:]


def draw():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 30], fill=BAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([16 + i * 20, 11, 26 + i * 20, 21], fill=c)
    d.text((W // 2 - 52, 7), "slicegrep", font=F, fill=DIM)
    y = 30 + PAD // 2
    for spans in screen[-26:]:
        x = PAD
        for text, color, bold in spans:
            f = FB if bold else F
            d.text((x, y), text, font=f, fill=color)
            x += d.textlength(text, font=f)
        y += LH
    frames.append(img)
    durations.append(TICK)


def hold(seconds):
    """Extend the last frame. Pauses must be duration, not duplicate frames."""
    if not frames:
        draw()
    durations[-1] += int(seconds * 1000)


def line(*spans, pause=0.0):
    norm = []
    for s in spans:
        norm.append((s[0],
                     s[1] if len(s) > 1 else FG,
                     s[2] if len(s) > 2 else False))
    screen.append(norm)
    draw()
    if pause:
        hold(pause)


def blank(pause=0.0):
    line(("", FG), pause=pause)


def clear(pause=0.0):
    screen.clear()
    draw()
    if pause:
        hold(pause)


def type_cmd(cmd, pause=1.1):
    """Typewriter, deliberately unhurried so it can be read while typing."""
    screen.append([("$ ", GREEN, True)])
    for i in range(0, len(cmd) + 2, 2):
        screen[-1] = [("$ ", GREEN, True), (cmd[:i], WHITE, False)]
        draw()
    screen[-1] = [("$ ", GREEN, True), (cmd, WHITE, False)]
    draw()
    hold(pause)


def caption(text, pause=2.2):
    line((text, DIM), pause=pause)


def save(name):
    imgs = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in frames]
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    imgs[0].save(out, save_all=True, append_images=imgs[1:],
                 duration=durations, loop=0, optimize=True)
    print(f"{name:18s} frames={len(imgs):4d}  "
          f"runtime={sum(durations)/1000:5.1f}s  "
          f"size={os.path.getsize(out)//1024:4d} KB")


# ===================================================================== short
# For the top of the README: the token collapse and the hook, nothing else.
# Same measured numbers as the full demo, just fewer scenes.
def build_short():
    reset()
    caption("# an agent needs to understand scoring and dedup", 1.5)
    type_cmd("wc -c src/slicegrep/core.py", pause=0.6)
    line(("  74406", YELLOW, True), ("  bytes", FG), ("   ->  ", DIM),
         ("~18,602 tokens", RED, True), (" of context burned", FG), pause=2.6)

    clear(0.3)
    caption("# ask for just the slices that matter", 1.4)
    type_cmd('slicegrep src/slicegrep/core.py "def score|dedupe" --budget 600',
             pause=0.7)
    blank()
    line(("=== slicegrep: ", CYAN), ("~350 tokens", GREEN, True),
         (" / 600 budget ===", CYAN), pause=1.0)
    line(("[DEDUPED: 4 near-duplicate chunk(s) removed]", YELLOW), pause=0.8)
    line(("NEGATIVE EVIDENCE: 'class Scorer' not found", DIM), pause=2.8)

    clear(0.3)
    caption("# or let the hook do it, with no agent cooperation", 1.5)
    line(("agent:", DIM), ("  Read(src/slicegrep/core.py)", WHITE), pause=1.2)
    line(("hook: ", CYAN, True), (" intercepted, map + slices returned", FG),
         pause=1.4)
    blank()
    line(("  17,849 tokens", RED, True), ("  ->  ", DIM),
         ("2,195 tokens", GREEN, True), pause=3.0)

    clear(0.3)
    blank()
    line(("  slicegrep", WHITE, True), pause=0.6)
    line(("  ranked slices, not whole files", FG), pause=0.8)
    blank()
    line(("  pip install git+https://github.com/haxo98098/slicegerp", CYAN),
         pause=4.5)
    save("demo-short.gif")


# ===================================================================== intro
build_short()
reset()

caption("# a coding agent needs to understand scoring and dedup", 1.6)
caption("# option 1: read the whole file", 1.6)
type_cmd("wc -c src/slicegrep/core.py")
line(("  74406", YELLOW, True), ("  bytes", FG), ("   ->  ", DIM),
     ("~18,602 tokens", RED, True), (" of context burned", FG), pause=3.2)

# ================================================================== task one
clear(0.4)
caption("# option 2: ask for just the slices that matter", 1.8)
type_cmd('slicegrep src/slicegrep/core.py "class Scorer|def score|dedupe" --budget 600')
blank()
line(("=== slicegrep: 1 chunk(s), ", CYAN), ("~350 tokens", GREEN, True),
     (" / 600 budget ===", CYAN), pause=1.4)
blank()
line(("[core.py:1831-1872 matches=1 patterns=dedupe score=8]", MAG), pause=0.5)
for code in ["                    if used + c.tokens <= budget:",
             "                        picked.append(c)",
             "                        used += c.tokens",
             "                picked.sort(key=lambda c: c.score, reverse=True)"]:
    line((code, FG))
hold(1.2)
blank()
line(("[DEDUPED: 4 near-duplicate chunk(s) removed]", YELLOW), pause=3.4)

# ================================================================== task two
clear(0.4)
caption("# rank across a whole directory, concepts that co-occur", 1.8)
type_cmd('slicegrep src/ "retry|timeout|backoff" --budget 700')
blank()
line(("=== slicegrep recursive: 3 chunk(s), ", CYAN),
     ("~579 tokens", GREEN, True), (" / 700 budget,", CYAN),
     (" 6 files searched, 3 matched ===", CYAN), pause=1.4)
blank()
line(("RANKING:", CYAN, True), pause=0.4)
line(("  1. core.py:80 ", FG),
     ("- multi_match(3), co_occurrence, all_patterns, rare_terms", YELLOW),
     pause=1.0)
line(("  2. cli.py:1 ", FG), ("- semantic-recall", YELLOW), pause=0.4)
line(("  3. core.py:792 ", FG), ("- semantic-recall", YELLOW), pause=1.2)
blank()
caption("# it tells you WHY each slice ranked where it did", 3.4)

# ================================================================ task three
clear(0.4)
caption("# or just ask in plain English", 1.8)
type_cmd('slicegrep src/ "how does budget packing guarantee definitions"')
blank()
line(("QUERY (auto-expanded):", CYAN, True), pause=0.5)
line(("  budget|packing|guarantee|definitions|budget_packing|", MAG), pause=0.3)
line(("  packing_guarantee|guarantee_definitions", MAG), pause=1.3)
blank()
line(("=== 5 chunk(s), ", CYAN), ("~638 tokens", GREEN, True),
     (" / 700 budget, 5 matched ===", CYAN), pause=1.2)
line(("  2. core.py:628 ", FG),
     ("- semantic-recall, region-history", YELLOW), pause=3.2)

# ================================================================= task four
clear(0.4)
caption("# an empty result is a real answer, so it says which kind", 1.8)
line(("NEGATIVE EVIDENCE:", CYAN, True), pause=0.6)
line(("  - Pattern 'class Scorer' ", FG), ("not found", RED, True), pause=1.4)
line(("  - Pattern 'def score' ", FG), ("IS present", GREEN, True),
     (" but fell outside the budget", FG), pause=1.6)
blank()
caption("# raise the budget, or change the query. no guessing.", 3.4)

# ================================================================= task five
clear(0.4)
caption("# and it can run without the agent choosing it at all", 1.8)
type_cmd("/plugin install slicegrep@slicegrep", pause=1.4)
blank()
line(("agent:", DIM), ("  Read(src/slicegrep/core.py)", WHITE), pause=1.4)
line(("hook: ", CYAN, True),
     (" whole-file read intercepted", FG), pause=1.2)
line(("       file map + ranked slices returned instead", FG), pause=1.6)
blank()
line(("  17,849 tokens", RED, True), ("  ->  ", DIM),
     ("2,195 tokens", GREEN, True), ("   on one read", FG), pause=3.6)

# =================================================================== closing
clear(0.4)
blank()
line(("  slicegrep", WHITE, True), pause=0.8)
blank()
line(("  ranked slices, not whole files", FG), pause=0.7)
line(("  tells you what it could not find", FG), pause=0.7)
line(("  fits whatever token budget you set", FG), pause=1.0)
blank()
line(("  pip install git+https://github.com/haxo98098/slicegerp", CYAN),
     pause=6.0)

save("demo.gif")
