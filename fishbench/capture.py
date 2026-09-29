"""The tank cam — headless WebGL capture of what the model actually built.

FishBench-2 does not parse the model's HTML and guess whether it works.
It RUNS it: the submission HTML is written to a temp file, opened in
headless chromium with software WebGL (SwiftShader — the same "GPU" for
every model on every machine, so fps numbers are comparable), filmed at
a fixed sample rate, and measured.

Determinism laws baked in:
  - three.js comes from the vendored copies in assets/three/, not the
    network — any CDN url for three is intercepted and served locally
    (module or UMD by url shape), and every OTHER network request is
    aborted. A tank that needs the internet to run dies here, as the
    brief warns.
  - one viewport, one run window, one sample rate, one diff threshold —
    all from tankspec, all sealed into the card's scoring digest.

The output is a measurements dict (the keys tankspec.MEASUREMENT_KEYS)
plus the film: jpeg frames on disk ready for the ffmpeg tape.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops, ImageStat

from . import tankspec

_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "assets", "three")

_THREE_URL = re.compile(
    r"(unpkg\.com|cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com|esm\.sh|"
    r"cdn\.skypack\.dev|threejs\.org)", re.IGNORECASE)


def _local_three(url: str) -> tuple[str, bytes] | None:
    """Map a CDN three.js request to the vendored file, or None."""
    if not _THREE_URL.search(url):
        return None
    low = url.lower()
    wants_module = ("module" in low or low.endswith(".mjs")
                    or "esm.sh" in low or "skypack" in low)
    name = ("three-0.170.0.module.min.js" if wants_module
            else "three-0.128.0.min.js")
    path = os.path.join(_ASSETS, name)
    try:
        with open(path, "rb") as f:
            return f"application/javascript; charset=utf-8", f.read()
    except OSError:
        return None


@dataclass
class TankCapture:
    html: str
    run_seconds: float = tankspec.RUN_SECONDS
    sample_hz: float = tankspec.SAMPLE_HZ
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    canvas_present: bool = False
    raf_frames: int = 0
    duration_s: float = 0.0
    frame_files: list[str] = field(default_factory=list)  # jpeg paths
    frame_times: list[float] = field(default_factory=list)  # capture t
    workdir: str = ""
    _page: Any = None
    _context: Any = None
    _browser: Any = None

    # ------------------------------------------------------------- capture

    def run(self) -> "TankCapture":
        from playwright.sync_api import sync_playwright

        os.makedirs(self.workdir, exist_ok=True)
        html_path = os.path.join(self.workdir, "tank.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(self.html)

        with sync_playwright() as pw:
            self._browser = pw.chromium.launch(
                headless=True,
                args=["--enable-unsafe-swiftshader",
                      "--disable-dev-shm-usage",
                      "--mute-audio"])
            self._context = self._browser.new_context(
                viewport={"width": tankspec.VIEWPORT[0],
                          "height": tankspec.VIEWPORT[1]})

            def _route(route: Any) -> None:
                url = route.request.url
                if url.startswith("file://"):
                    route.continue_()
                    return
                local = _local_three(url)
                if local is not None and "three" in url.lower():
                    ctype, body = local
                    route.fulfill(status=200, content_type=ctype, body=body)
                    return
                route.abort()

            self._context.route("**/*", _route)
            page = self._context.new_page()
            self._page = page
            page.on("console", lambda m: self._on_console(m))
            page.on("pageerror", lambda e: self.page_errors.append(str(e)[:300]))

            page.add_init_script(
                "(() => { let n = 0; const orig = window.requestAnimationFrame;"
                " window.requestAnimationFrame = cb => orig(t => { n++; cb(t); });"
                " window.__fbRaf = () => n; })();")

            try:
                page.goto(f"file://{html_path}", wait_until="load",
                          timeout=20_000)
            except Exception as e:  # navigation itself failed — dead tank
                self.page_errors.append(f"navigation: {e}")
                self._teardown()
                return self

            try:
                self.canvas_present = bool(
                    page.evaluate("!!document.querySelector('canvas')"))
            except Exception:
                self.canvas_present = False

            raf0 = self._raf()
            t0 = time.monotonic()
            period = 1.0 / max(0.5, self.sample_hz)
            i = 0
            while True:
                now = time.monotonic() - t0
                if now >= self.run_seconds:
                    break
                shot = os.path.join(self.workdir, f"f{i:04d}.jpg")
                try:
                    page.screenshot(path=shot, type="jpeg", quality=80,
                                    timeout=5_000)
                    self.frame_files.append(shot)
                    self.frame_times.append(time.monotonic() - t0)
                except Exception:
                    pass  # a dropped frame is a dropped frame
                i += 1
                nxt = t0 + i * period
                sleep_for = nxt - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self.duration_s = time.monotonic() - t0
            self.raf_frames = max(0, self._raf() - raf0)
            self._teardown()
        return self

    def _raf(self) -> int:
        try:
            return int(self._page.evaluate("window.__fbRaf ? "
                                           "window.__fbRaf() : 0") or 0)
        except Exception:
            return 0

    def _on_console(self, msg: Any) -> None:
        try:
            if msg.type == "error":
                self.console_errors.append(str(msg.text)[:300])
        except Exception:
            pass

    def _teardown(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass

    # ------------------------------------------------------------ analysis

    def _gray_frames(self) -> list[Image.Image]:
        frames = []
        for p in self.frame_files:
            try:
                with Image.open(p) as im:
                    frames.append(im.convert("L").resize(tankspec.ANALYSIS_SIZE))
            except OSError:
                continue
        return frames

    def measurements(self, n: int) -> dict[str, Any]:
        """The sealed evidence block — tankspec.MEASUREMENT_KEYS."""
        frames = self._gray_frames()
        pairs = list(zip(frames, frames[1:]))
        changed_frac = [
            self._changed_fraction(a, b) for a, b in pairs
        ] if pairs else []

        # blank: a page that never drew anything distinguishable. Software
        # WebGL takes ~1s to boot, so early frames can be the bare page —
        # the tank counts as drawn if ANY sampled frame has content.
        uniform = not frames or all(
            ImageStat.Stat(f).stddev[0] < 2.0 for f in frames)
        ever_moved = (bool(changed_frac)
                      and max(changed_frac) > tankspec.MOTION_FLOOR)
        canvas_drew = (self.canvas_present and not uniform and ever_moved)

        # motion windows (the last window absorbs the remainder)
        total = tankspec.WINDOWS
        alive = 0
        if changed_frac:
            per = max(1, len(changed_frac) // total)
            for w in range(total):
                lo = w * per
                hi = None if w == total - 1 else (w + 1) * per
                chunk = changed_frac[lo:hi]
                if chunk and sum(chunk) / len(chunk) > tankspec.MOTION_FLOOR:
                    alive += 1
        else:
            total = 0

        motion_coverage = (sum(changed_frac) / len(changed_frac)
                           if changed_frac else 0.0)
        fish_estimate = (self._fish_estimate(frames)
                         if canvas_drew else 0)
        fps = (self.raf_frames / self.duration_s
               if self.duration_s > 0 else 0.0)

        return {
            "console_errors": len(self.console_errors),
            "page_errors": len(self.page_errors),
            "canvas_drew": canvas_drew,
            "fish_estimate": fish_estimate,
            "motion_coverage": round(motion_coverage, 5),
            "windows_alive": alive,
            "windows_total": total,
            "fps": round(fps, 2),
            "sampled_frames": len(frames),
            "duration_s": round(self.duration_s, 2),
            "tank_crash": (not canvas_drew) or alive == 0,
        }

    @staticmethod
    def _changed_fraction(a: Image.Image, b: Image.Image) -> float:
        diff = ImageChops.difference(a, b)
        mask = diff.point(lambda p: 255 if p > tankspec.DIFF_THRESH else 0)
        histo = mask.histogram()
        return histo[255] / (mask.width * mask.height)

    @staticmethod
    def _fish_estimate(frames: list[Image.Image]) -> int:
        """Median count of moving blobs over a few frame windows.

        Counting is only trusted for small N (tankspec.COUNT_SPLIT);
        at scale the blobs merge into schools and scoring switches to
        coverage — but the estimate is still sealed in the card as
        evidence.
        """
        if len(frames) < 4:
            return 0
        counts = []
        for start in (0, len(frames) // 3, 2 * len(frames) // 3):
            trio = frames[start:start + 3]
            if len(trio) < 3:
                continue
            union = Image.new("1", tankspec.ANALYSIS_SIZE, 0)
            for a, b in ((trio[0], trio[1]), (trio[1], trio[2])):
                diff = ImageChops.difference(a, b)
                mask = diff.point(
                    lambda p: 255 if p > tankspec.DIFF_THRESH else 0
                ).convert("1")
                union = ImageChops.lighter(union, mask)
            counts.append(TankCapture._count_blobs(union))
        counts = [c for c in counts if c > 0] or [0]
        counts.sort()
        return counts[len(counts) // 2]

    @staticmethod
    def _count_blobs(mask: Image.Image) -> int:
        """4-connected components ≥ MIN_COMPONENT_AREA, pure python."""
        w, h = mask.size
        px = mask.load()
        seen = bytearray(w * h)
        area_min = tankspec.MIN_COMPONENT_AREA
        blobs = 0
        for y0 in range(h):
            row = y0 * w
            for x0 in range(w):
                idx0 = row + x0
                if px[x0, y0] == 0 or seen[idx0]:
                    continue
                # BFS
                size = 0
                stack = [idx0]
                seen[idx0] = 1
                while stack:
                    idx = stack.pop()
                    size += 1
                    x, y = idx % w, idx // w
                    if x > 0 and px[x - 1, y] and not seen[idx - 1]:
                        seen[idx - 1] = 1
                        stack.append(idx - 1)
                    if x + 1 < w and px[x + 1, y] and not seen[idx + 1]:
                        seen[idx + 1] = 1
                        stack.append(idx + 1)
                    if y > 0 and px[x, y - 1] and not seen[idx - w]:
                        seen[idx - w] = 1
                        stack.append(idx - w)
                    if y + 1 < h and px[x, y + 1] and not seen[idx + w]:
                        seen[idx + w] = 1
                        stack.append(idx + w)
                if size >= area_min:
                    blobs += 1
        return blobs

    # ---------------------------------------------------------------- tape

    def write_tape(self, out_path: str) -> str:
        """Film → mp4 at the true sample rate. The tape is honest time."""
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH — cannot cut tape")
        if not self.frame_files:
            raise RuntimeError("no frames captured — nothing to film")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".",
                    exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".mp4",
                                   dir=os.path.dirname(out_path) or ".")
        os.close(fd)
        enc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "image2", "-pattern_type", "glob", "-r",
             str(tankspec.SAMPLE_HZ), "-i",
             os.path.join(self.workdir, "f*.jpg"),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp],
            stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _, err = enc.communicate(timeout=120)
        if enc.returncode != 0:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RuntimeError(f"ffmpeg failed rc={enc.returncode}: "
                               f"{(err or b'')[:300]!r}")
        os.replace(tmp, out_path)
        return out_path


def strip_code_fences(text: str) -> str:
    """Models wrap HTML in ```fences despite the rules; forgive it once."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
