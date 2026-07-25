"""Render a terminal-style animated GIF demo from REAL slicegrep output.

Every number shown was measured, not mocked:
  core.py = 74,406 bytes ~= 18,602 tokens whole-file
  slicegrep --budget 600 -> ~350 tokens
"""
from PIL import Image, ImageDraw, ImageFont

W, H = 940, 600
PAD = 22
LH = 21                      # line height
FPS_MS = 70                  # ms per frame

BG = (13, 17, 23)
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
FT = ImageFont.truetype("C:/Windows/Fonts/consolab.ttf", 17)

frames = []
durations = []       # ms per frame; PIL drops duplicate frames, so pauses
                     # must be encoded as duration, not repeated frames
screen = []          # list of list[(text, color, bold)]


def draw():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # title bar
    d.rectangle([0, 0, W, 30], fill=(22, 27, 34))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([16 + i * 20, 11, 26 + i * 20, 21], fill=c)
    d.text((W // 2 - 52, 7), "slicegrep", font=F, fill=DIM)
    y = 30 + PAD // 2
    for line in screen[-24:]:
        x = PAD
        for text, color, bold in line:
            f = FB if bold else F
            d.text((x, y), text, font=f, fill=color)
            x += d.textlength(text, font=f)
        y += LH
    frames.append(img)
    durations.append(FPS_MS)


def hold(n=1):
    """Extend the last frame instead of emitting duplicates."""
    if not frames:
        draw()
    durations[-1] += n * FPS_MS


def type_cmd(cmd, prompt="$ "):
    screen.append([(prompt, GREEN, True)])
    step = 3
    for i in range(0, len(cmd) + step, step):
        screen[-1] = [(prompt, GREEN, True), (cmd[:i], WHITE, False)]
        draw()
    screen[-1] = [(prompt, GREEN, True), (cmd, WHITE, False)]
    hold(6)


def line(*spans, n=1):
    """Each span is (text,), (text, color) or (text, color, bold)."""
    norm = []
    for s in spans:
        text = s[0]
        color = s[1] if len(s) > 1 else FG
        bold = s[2] if len(s) > 2 else False
        norm.append((text, color, bold))
    screen.append(norm)
    hold(n)


def blank(n=1):
    screen.append([])
    hold(n)


# ---------------------------------------------------------------- scene 1
line(("# an agent needs to know how scoring and dedup work", DIM), n=10)
type_cmd('wc -c src/slicegrep/core.py')
line(("  74406", YELLOW, True), ("  bytes", FG), ("   ->  ", DIM),
     ("~18,602 tokens", RED, True), (" into the context window", FG))
hold(16)
blank()

# ---------------------------------------------------------------- scene 2
type_cmd('slicegrep src/slicegrep/core.py "class Scorer|def score|dedupe" --budget 600')
blank()
line(("=== slicegrep: 1 chunk(s), ", CYAN), ("~350 tokens", GREEN, True),
     (" / 600 budget ===", CYAN))
blank()
line(("[core.py:1831-1872 matches=1 patterns=dedupe score=8]", MAG))
for code in [
    "                    if used + c.tokens <= budget:",
    "                        picked.append(c)",
    "                        used += c.tokens",
    "                picked.sort(key=lambda c: c.score, reverse=True)",
]:
    line((code, FG))
blank()
line(("[DEDUPED: 4 near-duplicate chunk(s) removed]", YELLOW))
blank()
line(("NEGATIVE EVIDENCE:", CYAN, True))
line(("  - Pattern 'class Scorer' not found", DIM))
line(("  - Pattern 'def score' IS present but fell outside the budget", DIM))
hold(30)

# ---------------------------------------------------------------- scene 3
blank(2)
line(("  18,602 tokens", RED, True), ("   ->   ", DIM),
     ("350 tokens", GREEN, True),
     ("      98% smaller", WHITE, True), n=46)

# render
draw()
imgs = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in frames]
out = (r"C:\Users\Shadow\Desktop\slicegrep\docs\demo.gif")
import os
os.makedirs(os.path.dirname(out), exist_ok=True)
imgs[0].save(out, save_all=True, append_images=imgs[1:],
             duration=durations, loop=0, optimize=True)
print("frames:", len(imgs))
print("total seconds:", round(sum(durations) / 1000, 1))
print("size KB:", round(os.path.getsize(out) / 1024))
print(out)
