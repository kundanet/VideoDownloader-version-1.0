import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import threading
import os
import re
import json
import queue
import time
import sys
import socket
import tempfile
import urllib.error
import urllib.request


# ============================================================
# VIDEO DOWNLOADER  (Tkinter + yt-dlp.exe)
# Modern redesign: resizable layout, filterable queue, live stats,
# rounded controls, thread-safe UI updates, retry / clear tools.
# ============================================================

APP_TITLE = "Video Downloader"

# Version of THIS application. Must match the GitHub release tag without the "v"
# (release tag v1.0.1 -> "1.0.1"). Not related to the yt-dlp version.
APP_VERSION = "1.0.1"
GITHUB_REPO = "kundanet/VideoDownloader-version-1.0"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
UPDATE_CHECK_TIMEOUT = 10  # seconds
APP_UPDATE_ASSET_NAME = "VideoDownloader-Windows.zip"
APP_UPDATE_DOWNLOAD_TIMEOUT = 60  # seconds per network read
APP_UPDATE_CHUNK_SIZE = 1024 * 256

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# When running the Python source from the project root, the portable
# dependencies may live in the PyInstaller output folder. When the app is
# packaged, they are beside VideoDownloader.exe, so BASE_DIR is used.
if getattr(sys, "frozen", False):
    RESOURCE_DIR = BASE_DIR
else:
    _resource_candidates = [
        BASE_DIR,
        os.path.join(BASE_DIR, "dist", "VideoDownloader"),
    ]
    RESOURCE_DIR = next(
        (p for p in _resource_candidates if os.path.exists(os.path.join(p, "yt-dlp.exe"))),
        BASE_DIR,
    )

YT_DLP = os.path.join(RESOURCE_DIR, "yt-dlp.exe")
FFMPEG = os.path.join(RESOURCE_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(RESOURCE_DIR, "ffprobe.exe")
DENO = os.path.join(RESOURCE_DIR, "deno.exe")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ------------------------------------------------------------
# Theme
# ------------------------------------------------------------

FONT = "Segoe UI"
MONO = "Consolas"

BG = "#0c0e11"
SURFACE = "#14171b"
SURFACE_2 = "#1a1e23"
SURFACE_3 = "#242a31"
SURFACE_HOVER = "#2e353e"
BORDER = "#2a3038"
TEXT = "#eef2f4"
MUTED = "#8b95a1"
ACCENT = "#3ddc84"
ACCENT_HOVER = "#2fc774"
ACCENT_TEXT = "#06210f"
BLUE = "#4d9cff"
RED = "#ff5c67"
ORANGE = "#ffb84d"

CHIPS = {
    "waiting":    ("Waiting",     MUTED,  "#242a31"),
    "active":     ("Downloading", BLUE,   "#14263d"),
    "processing": ("Processing",  ORANGE, "#3a2a10"),
    "done":       ("Completed",   ACCENT, "#12301f"),
    "failed":     ("Failed",      RED,    "#3a1519"),
    "stopped":    ("Stopped",     ORANGE, "#3a2a10"),
}

BAR_COLORS = {
    "waiting": ACCENT, "active": ACCENT, "processing": ORANGE,
    "done": ACCENT, "failed": RED, "stopped": ORANGE,
}


# ============================================================
# HELPERS (unchanged logic)
# ============================================================

def clean_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "", name or "")
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name or "Video"


def is_tiktok_url(url):
    return bool(re.search(r"(?:https?://)?(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/", url, re.I))


def dependency_status():
    return {
        "yt-dlp": os.path.exists(YT_DLP),
        "ffmpeg": os.path.exists(FFMPEG),
        "ffprobe": os.path.exists(FFPROBE),
        "deno": os.path.exists(DENO),
    }


def tiktok_options():
    return [
        "--sleep-requests", "1",
        "--sleep-interval", "2",
        "--max-sleep-interval", "5",
    ]


def is_generic_title(title):
    if not title:
        return True
    title = title.strip()
    patterns = [
        r"^TikTok video\s*#?\s*\d+$",
        r"^video\s*#?\s*\d+$",
        r"^untitled$",
        r"^unknown$",
        r"^unknown video$",
    ]
    return any(re.match(p, title, re.I) for p in patterns)


def parse_progress(line):
    m = re.match(r"^\[download\]\s+(\d+(?:\.\d+)?)%", line)
    if not m:
        return None
    speed = re.search(r"at\s+([0-9.]+\s*[KMG]?iB/s)", line)
    eta = re.search(r"ETA\s+([0-9:]+)", line)
    return (
        max(0.0, min(100.0, float(m.group(1)))),
        speed.group(1) if speed else "--",
        eta.group(1) if eta else "--",
    )


def trunc(text, n):
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt_duration(sec):
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


SITES = [
    (r"youtube\.com|youtu\.be", "YouTube", "#ff4d4d"),
    (r"tiktok\.com", "TikTok", "#25f4ee"),
    (r"instagram\.com", "Instagram", "#e1306c"),
    (r"twitter\.com|(?<![a-z])x\.com", "X", "#c5ccd3"),
    (r"facebook\.com|fb\.watch", "Facebook", "#4d8dff"),
    (r"vimeo\.com", "Vimeo", "#1ab7ea"),
    (r"twitch\.tv", "Twitch", "#a970ff"),
    (r"reddit\.com", "Reddit", "#ff6a2b"),
]


def site_info(url):
    low = url.lower()
    for pattern, name, color in SITES:
        if re.search(pattern, low):
            return name, color
    m = re.search(r"https?://(?:www\.)?([^/]+)", low)
    return (m.group(1) if m else "Link"), BLUE


# ------------------------------------------------------------
# App update check (GitHub Releases)
# ------------------------------------------------------------

_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+){0,3})$")


def parse_version(text):
    """'v1.2.3' / '1.2.3' -> (1, 2, 3).  Anything else (e.g. '1.2.0-beta') -> None."""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def compare_versions(a, b):
    """Return 1 if a > b, 0 if equal, -1 if a < b, None if either cannot be compared safely."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    n = max(len(pa), len(pb))
    pa += (0,) * (n - len(pa))
    pb += (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def evaluate_update(latest_tag, current=None):
    """'newer' | 'uptodate' | 'unknown' (latest tag has a format we cannot compare safely)."""
    result = compare_versions(latest_tag, current or APP_VERSION)
    if result is None:
        return "unknown"
    return "newer" if result > 0 else "uptodate"


class UpdateCheckError(Exception):
    """A problem checking for updates, with a message that is safe to show to the user."""


def fetch_latest_release(api_url=None, timeout=UPDATE_CHECK_TIMEOUT):
    """Ask the GitHub Releases API for the latest release.

    Returns {"tag": "v1.0.1", "url": "https://github.com/..."}.
    Raises UpdateCheckError with a friendly message on any failure.
    """
    req = urllib.request.Request(
        api_url or RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"VideoDownloader/{APP_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateCheckError("No published release was found on GitHub yet.")
        if exc.code in (403, 429):
            raise UpdateCheckError("GitHub is limiting requests right now. Please try again in a few minutes.")
        raise UpdateCheckError(f"GitHub returned an error (HTTP {exc.code}).")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise UpdateCheckError("The request timed out. Check your internet connection and try again.")
        raise UpdateCheckError("Could not connect to GitHub. Check your internet connection.")
    except (socket.timeout, TimeoutError):
        raise UpdateCheckError("The request timed out. Check your internet connection and try again.")
    except OSError:
        raise UpdateCheckError("Could not connect to GitHub. Check your internet connection.")

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise UpdateCheckError("GitHub sent a response this app could not read.")
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str) or not data["tag_name"].strip():
        raise UpdateCheckError("GitHub's response did not include a release version.")

    url = data.get("html_url")
    if not (isinstance(url, str) and url.startswith("https://github.com/")):
        url = RELEASES_PAGE_URL

    assets = data.get("assets")
    if not isinstance(assets, list):
        raise UpdateCheckError("GitHub's release did not include downloadable files.")

    asset = next(
        (a for a in assets
         if isinstance(a, dict) and a.get("name") == APP_UPDATE_ASSET_NAME),
        None,
    )
    asset_url = asset.get("browser_download_url") if isinstance(asset, dict) else None
    if not (isinstance(asset_url, str) and asset_url.startswith("https://github.com/")):
        raise UpdateCheckError(
            f"The release does not contain the expected {APP_UPDATE_ASSET_NAME} file.")

    size = asset.get("size") if isinstance(asset, dict) else None
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None

    return {
        "tag": data["tag_name"].strip(),
        "url": url,
        "asset_name": APP_UPDATE_ASSET_NAME,
        "asset_url": asset_url,
        "asset_size": size,
    }


MAX_YT_VIDEOS = 200

# Extra yt-dlp arguments tried in order when YouTube answers 403.
# Log analysis: metadata requests work but every googlevideo media URL (DASH and HLS)
# returns 403, and the URL is bound to an IPv6 address even with --force-ipv4.
# A mismatch between the IP that asked for the URL and the IP that downloads it gives 403,
# so forcing one IP family is tried first.
def yt_strategies(quality):
    heights = {"2160p (4K)": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480}
    h = heights.get(quality)
    hf = f"[height<={h}]" if h else ""
    # Prefer HLS (m3u8) streams, fall back to the normal selection.
    hls = (f"bv*[protocol^=m3u8]{hf}+ba[protocol^=m3u8]/b[protocol^=m3u8]{hf}"
           f"/bv*{hf}+ba/b{hf}")
    return [
        [],
        ["--force-ipv6"],
        ["--force-ipv4"],
        ["--force-ipv6", "-f", hls],
        ["--force-ipv4", "-f", hls],
        ["-f", hls],
    ]


def write_log(url, lines):
    try:
        with open(os.path.join(BASE_DIR, "download_log.txt"), "a", encoding="utf-8") as f:
            f.write(f"\n\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  {url}\n")
            f.write("\n".join(lines[-250:]))
    except OSError:
        pass


def is_youtube_url(url):
    return bool(re.search(r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/", url, re.I))


def normalize_youtube_url(url):
    # A bare channel link (no tab) would list Videos + Shorts + Live together.
    # This tool is for Shorts, so bare channel links are sent to the Shorts tab.
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    m = re.match(
        r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))/?(?:[?#].*)?$",
        url, re.I)
    if m:
        return m.group(1) + "/shorts"
    return url


def expand_playlist(url, limit):
    """List the videos behind a channel / playlist / Shorts-tab link (no download).

    Returns (items, error). Each item: {"url", "title", "duration"}.
    """
    command = [
        YT_DLP, "--flat-playlist", "--dump-single-json",
        "--playlist-end", str(limit),
        "--extractor-retries", "5",
        "--socket-timeout", "30",
    ]
    if os.path.exists(DENO):
        command += ["--js-runtimes", f"deno:{DENO}"]
    command.append(url)

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300, creationflags=NO_WINDOW)
    except Exception as exc:
        return [], str(exc)

    if result.returncode != 0 or not result.stdout.strip():
        err = ""
        for line in (result.stderr or "").splitlines():
            if "ERROR:" in line:
                err = line.replace("ERROR:", "", 1).strip()
        return [], err or "Could not read that link"

    try:
        data = json.loads(result.stdout)
    except ValueError:
        return [], "Could not read the channel data"

    # A single video link has no "entries".
    if data.get("entries") is None:
        return [{
            "url": data.get("webpage_url") or url,
            "title": data.get("title") or "",
            "duration": data.get("duration"),
        }], ""

    def flatten(entries):
        for e in entries or []:
            if not e:
                continue
            if e.get("entries"):
                yield from flatten(e["entries"])
            else:
                yield e

    items = []
    seen = set()
    for e in flatten(data["entries"]):
        vid = e.get("id")
        link = e.get("url") or e.get("webpage_url") or ""
        if not link.startswith("http"):
            if not vid:
                continue
            link = f"https://www.youtube.com/watch?v={vid}"
        if link in seen:
            continue
        seen.add(link)
        items.append({
            "url": link,
            "title": e.get("title") or "",
            "duration": e.get("duration"),
        })
        if len(items) >= limit:
            break

    if not items:
        return [], "No videos found at that link"
    return items, ""


def get_info(url, use_browser_cookies=None):
    attempts = []
    base = [
        YT_DLP,
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        "--extractor-retries", "5",
        "--socket-timeout", "30",
    ]
    if os.path.exists(DENO):
        base += ["--js-runtimes", f"deno:{DENO}"]
    if is_tiktok_url(url):
        base += tiktok_options()

    attempts.append(base + [url])
    if is_tiktok_url(url) and use_browser_cookies:
        attempts.append(base + ["--cookies-from-browser", use_browser_cookies, url])

    for command in attempts:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=NO_WINDOW,
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
        except Exception:
            pass
    return {}


def build_command(url, output_template, mode, quality, fmt, browser_cookies=None):
    command = [YT_DLP]

    if os.path.exists(DENO):
        command += ["--js-runtimes", f"deno:{DENO}"]

    command += [
        "--no-playlist",
        "--newline",
        "--verbose",
        "--retries", "10",
        "--fragment-retries", "10",
        "--file-access-retries", "5",
        "--extractor-retries", "5",
        "--socket-timeout", "30",
        "--windows-filenames",
        "--trim-filenames", "180",
    ]

    if is_youtube_url(url):
        command += ["--sleep-interval", "1", "--max-sleep-interval", "3"]
        if browser_cookies:
            command += ["--cookies-from-browser", browser_cookies]

    if is_tiktok_url(url):
        command += tiktok_options()
        if browser_cookies:
            command += ["--cookies-from-browser", browser_cookies]

    if os.path.exists(FFMPEG):
        command += ["--ffmpeg-location", RESOURCE_DIR]

    if mode == "Audio":
        command += [
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", "mp3" if fmt == "MP3" else "m4a",
        ]
    else:
        heights = {"2160p (4K)": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480}
        h = heights.get(quality)
        selector = f"bv*[height<={h}]+ba/b[height<={h}]" if h else "bv*+ba/b"
        command += ["-f", selector]

        if fmt == "MP4":
            command += ["--merge-output-format", "mp4"]
        elif fmt == "WEBM":
            command += ["--merge-output-format", "webm"]

    command += ["-o", output_template, url]
    return command


# ============================================================
# CUSTOM WIDGETS
# ============================================================

def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """Flat rounded button with hover + disabled states."""

    def __init__(self, parent, text, command=None, bg=SURFACE_3, fg=TEXT,
                 hover=SURFACE_HOVER, width=120, height=36, radius=10,
                 font=(FONT, 10, "bold")):
        super().__init__(parent, width=width, height=height, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self._text = text
        self._command = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover
        self._radius = radius
        self._font = font
        self._enabled = True
        self._hovering = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", self._on_click)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 4:
            w, h = int(self.cget("width")), int(self.cget("height"))
        if not self._enabled:
            fill, fg = SURFACE_3, MUTED
        else:
            fill, fg = (self._hover_bg if self._hovering else self._bg), self._fg
        round_rect(self, 1, 1, w - 1, h - 1, self._radius, fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _):
        self._hovering = True
        self._draw()

    def _on_leave(self, _):
        self._hovering = False
        self._draw()

    def _on_click(self, e):
        if self._enabled and self._command and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self._command()

    def set_text(self, text):
        self._text = text
        self._draw()

    def set_enabled(self, enabled):
        self._enabled = enabled
        self.config(cursor="hand2" if enabled else "arrow")
        self._draw()


class ProgressBar(tk.Canvas):
    """Rounded pill progress bar."""

    def __init__(self, parent, height=8, color=ACCENT, track=SURFACE_3):
        super().__init__(parent, height=height, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0)
        self._h = height
        self._color = color
        self._track = track
        self._percent = 0
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, percent, color=None):
        self._percent = max(0, min(100, percent))
        if color:
            self._color = color
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self._h
        if w < 4:
            return
        r = h / 2
        round_rect(self, 0, 0, w, h, r, fill=self._track, outline="")
        fw = w * self._percent / 100
        if fw > 0:
            fw = max(fw, h)
            round_rect(self, 0, 0, fw, h, r, fill=self._color, outline="")


class Segmented(tk.Frame):
    """Two-or-more option segmented toggle."""

    def __init__(self, parent, options, variable, command=None):
        super().__init__(parent, bg=SURFACE_3, padx=3, pady=3)
        self.var = variable
        self.command = command
        self.labels = {}
        for opt in options:
            lbl = tk.Label(self, text=opt, font=(FONT, 10, "bold"), padx=14, pady=7, cursor="hand2")
            lbl.pack(side="left", expand=True, fill="x")
            lbl.bind("<Button-1>", lambda e, o=opt: self.select(o))
            self.labels[opt] = lbl
        self.select(variable.get(), fire=False)

    def select(self, opt, fire=True):
        self.var.set(opt)
        for o, lbl in self.labels.items():
            sel = o == opt
            lbl.config(bg=ACCENT if sel else SURFACE_3, fg=ACCENT_TEXT if sel else MUTED)
        if fire and self.command:
            self.command()


class DownloadCard(tk.Frame):
    """One row in the queue."""

    def __init__(self, parent, number, url, on_remove):
        super().__init__(parent, bg=SURFACE_2, highlightbackground=BORDER, highlightthickness=1)
        self.number = number
        self.url = url
        self.status = "waiting"
        self.percent = 0
        self.columnconfigure(1, weight=1)

        site, color = site_info(url)

        icon = tk.Canvas(self, width=52, height=52, bg=SURFACE_2, highlightthickness=0)
        round_rect(icon, 0, 0, 52, 52, 14, fill=SURFACE_3, outline="")
        icon.create_text(26, 26, text=site[0].upper(), fill=color, font=(FONT, 17, "bold"))
        icon.grid(row=0, rowspan=3, column=0, padx=(14, 12), pady=14)

        self.title_lbl = tk.Label(self, text=trunc(url, 90), font=(FONT, 10, "bold"),
                                  fg=TEXT, bg=SURFACE_2, anchor="w")
        self.title_lbl.grid(row=0, column=1, sticky="w", pady=(14, 0))

        self.meta_lbl = tk.Label(self, text=f"{site}  •  Waiting in queue", font=(FONT, 9),
                                 fg=MUTED, bg=SURFACE_2, anchor="w")
        self.meta_lbl.grid(row=1, column=1, sticky="w", pady=(2, 0))

        self.bar = ProgressBar(self, height=6)
        self.bar.grid(row=2, column=1, sticky="ew", pady=(10, 14))

        right = tk.Frame(self, bg=SURFACE_2)
        right.grid(row=0, rowspan=3, column=2, padx=(14, 6))
        self.chip = tk.Label(right, font=(FONT, 8, "bold"), padx=10, pady=3)
        self.chip.pack(anchor="e")
        self.info = tk.Label(right, text="", font=(FONT, 9), fg=MUTED, bg=SURFACE_2,
                             width=26, anchor="e")
        self.info.pack(anchor="e", pady=(8, 0))

        remove = tk.Label(self, text="✕", font=(FONT, 11), fg=MUTED, bg=SURFACE_2, cursor="hand2")
        remove.grid(row=0, rowspan=3, column=3, padx=(4, 14))
        remove.bind("<Button-1>", lambda e: on_remove(self))
        remove.bind("<Enter>", lambda e: remove.config(fg=RED))
        remove.bind("<Leave>", lambda e: remove.config(fg=MUTED))

        self.update("waiting")

    def update(self, status, percent=None, speed="--", eta="--", title=None, meta=None):
        self.status = status
        if percent is not None:
            self.percent = percent

        text, fg, bg = CHIPS[status]
        self.chip.config(text=text, fg=fg, bg=bg)

        if status == "active":
            info = f"{self.percent:.0f}%  •  {speed}  •  ETA {eta}"
        elif status == "processing":
            info = "Finalizing…"
        elif status == "done":
            info = "100%"
        else:
            info = ""
        self.info.config(text=info)

        self.bar.set(self.percent, BAR_COLORS[status])

        if title:
            self.title_lbl.config(text=trunc(title, 90))
        if meta is not None:
            self.meta_lbl.config(text=trunc(meta, 120), fg=RED if status == "failed" else MUTED)


# ============================================================
# APP
# ============================================================

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_TITLE}  v{APP_VERSION}")
        root.geometry("1200x840")
        root.minsize(1060, 800)
        root.configure(bg=BG)

        self.download_folder = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.mode_var = tk.StringVar(value="Video")
        self.quality_var = tk.StringVar(value="Best quality")
        self.format_var = tk.StringVar(value="MP4")
        self.cookies_var = tk.StringVar(value="Off")
        self.net_var = tk.StringVar(value="Auto")
        self.platform_var = tk.StringVar(value="YouTube")
        self.yt_var = tk.StringVar(value="")
        self.limit_var = tk.StringVar(value=str(MAX_YT_VIDEOS))
        self.idle_btn_text = "⬇   DOWNLOAD SHORTS"

        self.cards = []
        self.is_downloading = False
        self.current_process = None
        self.stop_event = threading.Event()
        self.batch_id = 0
        self.ui_queue = queue.Queue()
        self.filter = "All"
        self.warned_optional = False
        self.app_update_checking = False

        self.setup_style()
        self.build_header()
        self.build_statusbar()
        self.build_body()

        self.on_mode_change()
        self.on_platform_change()
        self.refresh_list()
        self.show_dependency_status()
        threading.Thread(target=self.fetch_version, daemon=True).start()

        root.bind("<Control-Return>", lambda e: self.start_download())
        self.poll()

    # --------------------------------------------------------
    # Style
    # --------------------------------------------------------
    def setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "Dark.TCombobox",
            fieldbackground=SURFACE_3, background=SURFACE_3, foreground=TEXT,
            arrowcolor=MUTED, bordercolor=BORDER, lightcolor=SURFACE_3,
            darkcolor=SURFACE_3, padding=7, selectbackground=SURFACE_3,
            selectforeground=TEXT,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", SURFACE_3), ("disabled", SURFACE)],
            foreground=[("disabled", MUTED)],
            arrowcolor=[("disabled", BORDER), ("active", TEXT)],
            background=[("active", SURFACE_HOVER)],
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=SURFACE_3, troughcolor=SURFACE, bordercolor=SURFACE,
            arrowcolor=MUTED, lightcolor=SURFACE_3, darkcolor=SURFACE_3, gripcount=0,
        )
        style.map("Dark.Vertical.TScrollbar", background=[("active", SURFACE_HOVER)])

        self.root.option_add("*TCombobox*Listbox.background", SURFACE_3)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", ACCENT_TEXT)
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    # --------------------------------------------------------
    # Header / status bar
    # --------------------------------------------------------
    def build_header(self):
        header = tk.Frame(self.root, bg=SURFACE)
        header.pack(fill="x")
        inner = tk.Frame(header, bg=SURFACE)
        inner.pack(fill="x", padx=24, pady=14)

        logo = tk.Canvas(inner, width=44, height=44, bg=SURFACE, highlightthickness=0)
        round_rect(logo, 0, 0, 44, 44, 12, fill=ACCENT, outline="")
        logo.create_polygon(17, 13, 17, 31, 32, 22, fill=ACCENT_TEXT, outline="")
        logo.pack(side="left")

        titles = tk.Frame(inner, bg=SURFACE)
        titles.pack(side="left", padx=14)
        tk.Label(titles, text="Video Downloader", font=(FONT, 16, "bold"),
                 fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(titles, text="Fast  •  Simple  •  Bulk downloads", font=(FONT, 9),
                 fg=MUTED, bg=SURFACE).pack(anchor="w")

        self.update_btn = RoundButton(inner, "⟳  Update yt-dlp", self.update_ytdlp,
                                      width=140, height=34, radius=9,
                                      font=(FONT, 9, "bold"))
        self.update_btn.pack(side="right")
        self.app_update_btn = RoundButton(inner, "Check for Updates", self.check_for_updates,
                                          width=150, height=34, radius=9,
                                          font=(FONT, 9, "bold"))
        self.app_update_btn.pack(side="right", padx=(0, 10))
        tk.Label(inner, text="Ctrl + Enter  to start", font=(FONT, 9),
                 fg=MUTED, bg=SURFACE).pack(side="right", padx=(0, 16))

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

    def build_statusbar(self):
        bar = tk.Frame(self.root, bg=SURFACE)
        bar.pack(side="bottom", fill="x")
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x")
        inner = tk.Frame(bar, bg=SURFACE)
        inner.pack(fill="x", padx=20, pady=7)

        self.status_dot = tk.Label(inner, text="●", font=(FONT, 9), fg=ACCENT, bg=SURFACE)
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(inner, text="Ready", font=(FONT, 9, "bold"),
                                     fg=TEXT, bg=SURFACE)
        self.status_label.pack(side="left", padx=(6, 0))

        for name, ok in reversed(list(dependency_status().items())):
            tk.Label(inner, text=f"● {name}", font=(FONT, 8),
                     fg=ACCENT if ok else RED, bg=SURFACE).pack(side="right", padx=(12, 0))
        self.ver_label = tk.Label(inner, text="", font=(FONT, 8, "bold"), fg=MUTED, bg=SURFACE)
        self.ver_label.pack(side="right", padx=(12, 18))

    def set_status(self, text, color=TEXT):
        self.status_label.config(text=text)
        self.status_dot.config(fg=color)

    def show_dependency_status(self):
        missing = [n for n, ok in dependency_status().items() if not ok]
        if missing:
            self.set_status("Missing: " + ", ".join(missing), RED)
        else:
            self.set_status("Ready", ACCENT)

    # --------------------------------------------------------
    # Body
    # --------------------------------------------------------
    def build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=16)

        left = tk.Frame(body, bg=SURFACE, width=390,
                        highlightbackground=BORDER, highlightthickness=1)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        right = tk.Frame(body, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))

        self.build_left(left)
        self.build_right(right)

    def make_combo(self, parent, label, var, values, row, col, colspan=1, padx=(0, 0)):
        tk.Label(parent, text=label, font=(FONT, 9), fg=MUTED, bg=SURFACE).grid(
            row=row * 2, column=col, columnspan=colspan, sticky="w", padx=padx, pady=(10, 4))
        cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly",
                          style="Dark.TCombobox")
        cb.grid(row=row * 2 + 1, column=col, columnspan=colspan, sticky="ew", padx=padx)
        return cb

    def build_left(self, left):
        # Bottom-anchored blocks are packed first so they can never be clipped.
        actions = tk.Frame(left, bg=SURFACE)
        actions.pack(side="bottom", fill="x", padx=20, pady=(8, 16))

        self.download_btn = RoundButton(
            actions, self.idle_btn_text, self.start_download,
            bg=ACCENT, fg=ACCENT_TEXT, hover=ACCENT_HOVER,
            height=44, radius=12, font=(FONT, 11, "bold"))
        self.download_btn.pack(fill="x")

        self.stop_btn = RoundButton(actions, "■   Stop", self.stop_download, fg=RED, height=36)
        self.stop_btn.pack(fill="x", pady=(8, 0))
        self.stop_btn.set_enabled(False)

        opts = tk.Frame(left, bg=SURFACE)
        opts.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        tk.Frame(opts, bg=BORDER, height=1).pack(fill="x", pady=(0, 12))

        Segmented(opts, ["Video", "Audio"], self.mode_var, self.on_mode_change).pack(fill="x")

        self.opts_grid = tk.Frame(opts, bg=SURFACE)
        self.opts_grid.pack(fill="x")
        self.opts_grid.columnconfigure(0, weight=1, uniform="c")
        self.opts_grid.columnconfigure(1, weight=1, uniform="c")
        self.quality_combo = self.make_combo(
            self.opts_grid, "Quality", self.quality_var,
            ["Best quality", "2160p (4K)", "1440p", "1080p", "720p", "480p"],
            0, 0, padx=(0, 6))
        self.format_combo = self.make_combo(
            self.opts_grid, "Format", self.format_var, ["MP4", "WEBM"], 0, 1, padx=(6, 0))

        self.cookie_frame = tk.Frame(opts, bg=SURFACE)
        self.cookie_frame.columnconfigure(0, weight=1, uniform="n")
        self.cookie_frame.columnconfigure(1, weight=1, uniform="n")
        self.make_combo(self.cookie_frame, "Browser cookies", self.cookies_var,
                        ["Off", "Chrome", "Edge", "Firefox"], 0, 0, padx=(0, 6))
        self.make_combo(self.cookie_frame, "Network  (403 fix)", self.net_var,
                        ["Auto", "IPv6", "IPv4"], 0, 1, padx=(6, 0))

        # Platform selector
        tk.Label(left, text="Platform", font=(FONT, 12, "bold"), fg=TEXT, bg=SURFACE).pack(
            anchor="w", padx=20, pady=(16, 8))
        Segmented(left, ["YouTube", "TikTok", "Other"], self.platform_var,
                  self.on_platform_change).pack(fill="x", padx=20)

        # Input area (swapped depending on platform) — takes whatever space is left
        self.input_container = tk.Frame(left, bg=SURFACE)
        self.input_container.pack(fill="both", expand=True)

        # ---- YouTube: one channel Shorts link ----
        self.yt_frame = tk.Frame(self.input_container, bg=SURFACE)
        tk.Label(self.yt_frame, text="Channel Shorts link", font=(FONT, 11, "bold"),
                 fg=TEXT, bg=SURFACE).pack(anchor="w", padx=20, pady=(14, 2))
        tk.Label(self.yt_frame, text="Paste one link — every Short in that channel is queued",
                 font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(anchor="w", padx=20)
        tk.Entry(
            self.yt_frame, textvariable=self.yt_var, font=(MONO, 10), fg=TEXT, bg="#0f1215",
            insertbackground=ACCENT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT
        ).pack(fill="x", padx=20, pady=(10, 8), ipady=8)

        yrow = tk.Frame(self.yt_frame, bg=SURFACE)
        yrow.pack(fill="x", padx=20)
        RoundButton(yrow, "Paste", self.paste_yt).pack(side="left", expand=True, fill="x", padx=(0, 4))
        RoundButton(yrow, "Clear", lambda: self.yt_var.set("")).pack(
            side="left", expand=True, fill="x", padx=(4, 0))

        lim = tk.Frame(self.yt_frame, bg=SURFACE)
        lim.pack(fill="x", padx=20, pady=(12, 0))
        tk.Label(lim, text=f"Max videos  (limit {MAX_YT_VIDEOS})", font=(FONT, 9),
                 fg=MUTED, bg=SURFACE).pack(side="left")
        tk.Spinbox(
            lim, from_=1, to=MAX_YT_VIDEOS, textvariable=self.limit_var, width=6,
            font=(FONT, 10, "bold"), fg=TEXT, bg=SURFACE_3, buttonbackground=SURFACE_3,
            insertbackground=TEXT, relief="flat", justify="center",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT
        ).pack(side="right", ipady=4)

        tk.Label(
            self.yt_frame,
            text="The Shorts are listed first, then downloaded one by one. "
                 "A bare channel link is sent to its Shorts tab.",
            font=(FONT, 8), fg=MUTED, bg=SURFACE, justify="left", anchor="w", wraplength=330
        ).pack(fill="x", padx=20, pady=(10, 0))

        # ---- TikTok / Other: many links, one per line ----
        self.multi_frame = tk.Frame(self.input_container, bg=SURFACE)
        head = tk.Frame(self.multi_frame, bg=SURFACE)
        head.pack(fill="x", padx=20, pady=(14, 2))
        tk.Label(head, text="Add links", font=(FONT, 11, "bold"), fg=TEXT, bg=SURFACE).pack(side="left")
        self.link_count = tk.Label(head, text="0 links", font=(FONT, 9), fg=MUTED, bg=SURFACE)
        self.link_count.pack(side="right")
        self.multi_hint = tk.Label(self.multi_frame, text="", font=(FONT, 9), fg=MUTED, bg=SURFACE)
        self.multi_hint.pack(anchor="w", padx=20)

        row = tk.Frame(self.multi_frame, bg=SURFACE)
        row.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        RoundButton(row, "Paste", self.paste_link).pack(side="left", expand=True, fill="x", padx=(0, 4))
        RoundButton(row, "Clear", self.clear_links).pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.url_box = tk.Text(
            self.multi_frame, height=3, font=(MONO, 10), fg=TEXT, bg="#0f1215",
            insertbackground=ACCENT, relief="flat", wrap="none", undo=True,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
            padx=10, pady=8, selectbackground="#28405f")
        self.url_box.pack(fill="both", expand=True, padx=20, pady=(10, 8))
        self.url_box.bind("<KeyRelease>", self.update_link_count)

    def on_platform_change(self):
        p = self.platform_var.get()
        self.yt_frame.pack_forget()
        self.multi_frame.pack_forget()
        self.cookie_frame.pack_forget()

        if p == "YouTube":
            self.yt_frame.pack(fill="both", expand=True)
            self.idle_btn_text = "⬇   DOWNLOAD SHORTS"
            self.cookie_frame.pack(fill="x", after=self.opts_grid)
        else:
            self.multi_frame.pack(fill="both", expand=True)
            if p == "TikTok":
                self.multi_hint.config(text="Paste TikTok links — one per line")
                self.idle_btn_text = "⬇   DOWNLOAD TIKTOKS"
                self.cookie_frame.pack(fill="x", after=self.opts_grid)
            else:
                self.multi_hint.config(text="Any site yt-dlp supports — one per line")
                self.idle_btn_text = "⬇   DOWNLOAD ALL"

        if not self.is_downloading:
            self.download_btn.set_text(self.idle_btn_text)

    def build_right(self, right):
        top = tk.Frame(right, bg=SURFACE)
        top.pack(fill="x", padx=20, pady=(18, 0))

        tl = tk.Frame(top, bg=SURFACE)
        tl.pack(side="left")
        tk.Label(tl, text="Queue", font=(FONT, 14, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        self.stats_label = tk.Label(tl, text="No downloads yet", font=(FONT, 9), fg=MUTED, bg=SURFACE)
        self.stats_label.pack(anchor="w")

        btns = tk.Frame(top, bg=SURFACE)
        btns.pack(side="right")
        RoundButton(btns, "Retry failed", self.retry_failed, width=96, height=32, radius=9,
                    font=(FONT, 9, "bold")).pack(side="left", padx=3)
        RoundButton(btns, "Clear finished", self.clear_finished, width=104, height=32, radius=9,
                    font=(FONT, 9, "bold")).pack(side="left", padx=3)
        RoundButton(btns, "Open folder", self.open_folder, width=96, height=32, radius=9,
                    font=(FONT, 9, "bold")).pack(side="left", padx=3)
        RoundButton(btns, "Clear all", self.clear_all, width=80, height=32, radius=9,
                    fg=RED, font=(FONT, 9, "bold")).pack(side="left", padx=3)

        srow = tk.Frame(right, bg=SURFACE)
        srow.pack(fill="x", padx=20, pady=(14, 0))
        tk.Label(srow, text="Save to", font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(side="left", padx=(0, 10))
        RoundButton(srow, "Browse", self.choose_folder, width=76, height=32, radius=9,
                    font=(FONT, 9, "bold")).pack(side="right", padx=(8, 0))
        tk.Entry(srow, textvariable=self.download_folder, font=(FONT, 9), fg=TEXT, bg=SURFACE_3,
                 insertbackground=TEXT, relief="flat", highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT
                 ).pack(side="left", fill="x", expand=True, ipady=6)

        # Overall progress
        orow = tk.Frame(right, bg=SURFACE)
        orow.pack(fill="x", padx=20, pady=(16, 0))
        tk.Label(orow, text="Overall progress", font=(FONT, 9, "bold"), fg=MUTED, bg=SURFACE).pack(side="left")
        self.overall_label = tk.Label(orow, text="0%", font=(FONT, 9, "bold"), fg=TEXT, bg=SURFACE)
        self.overall_label.pack(side="right")
        self.overall_bar = ProgressBar(right, height=8)
        self.overall_bar.pack(fill="x", padx=20, pady=(6, 0))

        # Tabs
        tabs = tk.Frame(right, bg=SURFACE)
        tabs.pack(fill="x", padx=20, pady=(16, 8))
        self.tab_labels = {}
        for name in ["All", "Active", "Completed", "Failed"]:
            lbl = tk.Label(tabs, text=name, font=(FONT, 9, "bold"), padx=14, pady=6, cursor="hand2")
            lbl.pack(side="left", padx=(0, 6))
            lbl.bind("<Button-1>", lambda e, n=name: self.set_filter(n))
            self.tab_labels[name] = lbl
        self.style_tabs()

        # Scrollable list
        wrap = tk.Frame(right, bg=SURFACE)
        wrap.pack(fill="both", expand=True, padx=(8, 4), pady=(0, 10))

        self.canvas = tk.Canvas(wrap, bg=SURFACE, highlightthickness=0, yscrollincrement=30)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview,
                           style="Dark.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=sb.set)

        self.list_frame = tk.Frame(self.canvas, bg=SURFACE)
        self.list_window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>",
                             lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self.list_window, width=e.width))

        wrap.bind("<Enter>", lambda e: self.root.bind_all("<MouseWheel>", self.on_wheel))
        wrap.bind("<Leave>", lambda e: self.root.unbind_all("<MouseWheel>"))

        self.empty_label = tk.Label(self.list_frame, text="", font=(FONT, 11), fg=MUTED,
                                    bg=SURFACE, justify="center")

    def on_wheel(self, e):
        if self.list_frame.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    # --------------------------------------------------------
    # Tabs / list
    # --------------------------------------------------------
    def style_tabs(self):
        for name, lbl in self.tab_labels.items():
            sel = name == self.filter
            lbl.config(bg=SURFACE_3 if sel else SURFACE, fg=TEXT if sel else MUTED)

    def set_filter(self, name):
        self.filter = name
        self.style_tabs()
        self.refresh_list()
        self.canvas.yview_moveto(0)

    def matches(self, card):
        s = card.status
        if self.filter == "All":
            return True
        if self.filter == "Active":
            return s in ("waiting", "active", "processing")
        if self.filter == "Completed":
            return s == "done"
        return s in ("failed", "stopped")

    def refresh_list(self):
        for c in self.cards:
            c.pack_forget()
        self.empty_label.pack_forget()

        visible = [c for c in self.cards if self.matches(c)]
        for c in visible:
            c.pack(fill="x", padx=12, pady=5)

        if not visible:
            if not self.cards:
                text = "⬇\n\nNo downloads yet\nPaste links on the left and press  DOWNLOAD ALL"
            else:
                text = f"Nothing in “{self.filter}”"
            self.empty_label.config(text=text)
            self.empty_label.pack(pady=90)

        self.update_counts()

    def update_counts(self):
        counts = {"All": len(self.cards), "Active": 0, "Completed": 0, "Failed": 0}
        for c in self.cards:
            if c.status in ("waiting", "active", "processing"):
                counts["Active"] += 1
            elif c.status == "done":
                counts["Completed"] += 1
            else:
                counts["Failed"] += 1
        for name, lbl in self.tab_labels.items():
            lbl.config(text=f"{name}  {counts[name]}")

        if not self.cards:
            self.stats_label.config(text="No downloads yet")
        else:
            self.stats_label.config(
                text=f"{counts['Completed']} completed  •  {counts['Active']} pending  •  {counts['Failed']} failed")

    def scroll_to(self, card):
        try:
            self.root.update_idletasks()
            total = self.list_frame.winfo_height()
            if total <= self.canvas.winfo_height() or not card.winfo_manager():
                return
            self.canvas.yview_moveto(max(0, card.winfo_y() - 10) / total)
        except tk.TclError:
            pass

    def create_card(self, number, url):
        card = DownloadCard(self.list_frame, number, url, self.remove_card)
        self.cards.append(card)
        return card

    def remove_card(self, card):
        if self.is_downloading and card.status in ("waiting", "active", "processing"):
            self.set_status("Stop the download before removing an active item", ORANGE)
            return
        card.destroy()
        if card in self.cards:
            self.cards.remove(card)
        self.refresh_list()

    # --------------------------------------------------------
    # Link box actions
    # --------------------------------------------------------
    def update_link_count(self, event=None):
        n = sum(1 for l in self.url_box.get("1.0", tk.END).splitlines() if l.strip())
        self.link_count.config(text=f"{n} link{'s' if n != 1 else ''}")

    def paste_link(self):
        try:
            data = self.root.clipboard_get().strip()
        except tk.TclError:
            data = ""
        if not data:
            messagebox.showwarning("Paste Link", "There is no text in the clipboard.")
            return
        for line in (x.strip() for x in data.splitlines() if x.strip()):
            self.url_box.insert(tk.END, line + "\n")
        self.update_link_count()

    def clear_links(self):
        self.url_box.delete("1.0", tk.END)
        self.yt_var.set("")
        self.update_link_count()

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Choose download folder")
        if folder:
            self.download_folder.set(folder)

    def open_folder(self):
        folder = self.download_folder.get().strip()
        if not os.path.isdir(folder):
            messagebox.showwarning("Open folder", "That folder does not exist yet.")
            return
        if os.name == "nt":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    def on_mode_change(self):
        if self.mode_var.get() == "Audio":
            values = ["MP3", "M4A"]
            self.quality_combo.config(state="disabled")
        else:
            values = ["MP4", "WEBM"]
            self.quality_combo.config(state="readonly")
        self.format_combo.config(values=values)
        if self.format_var.get() not in values:
            self.format_var.set(values[0])

    # --------------------------------------------------------
    # Queue actions
    # --------------------------------------------------------
    def clear_finished(self):
        for c in [c for c in self.cards if c.status == "done"]:
            c.destroy()
            self.cards.remove(c)
        self.refresh_list()

    def clear_all(self):
        if self.is_downloading:
            messagebox.showwarning("Download in progress",
                                   "Stop the current download before clearing everything.")
            return
        self.clear_links()
        for c in self.cards:
            c.destroy()
        self.cards.clear()
        self.set_overall(0)
        self.refresh_list()
        self.canvas.yview_moveto(0)
        self.set_status("Ready for new links", ACCENT)

    def retry_failed(self):
        if self.is_downloading:
            return
        batch = [c for c in self.cards if c.status in ("failed", "stopped")]
        if not batch:
            self.set_status("Nothing to retry", MUTED)
            return
        folder = self.preflight()
        if not folder:
            return
        for c in batch:
            c.update("waiting", 0, "--", "--", meta="Queued for retry")
        self.refresh_list()
        self.run_batch(batch, folder)

    def set_overall(self, percent, completed=None, total=None):
        percent = max(0, min(100, percent))
        self.overall_bar.set(percent)
        self.overall_label.config(text=f"{percent:.0f}%")

    def set_running(self, running):
        self.download_btn.set_enabled(not running)
        self.download_btn.set_text("DOWNLOADING…" if running else self.idle_btn_text)
        self.stop_btn.set_enabled(running)

    # --------------------------------------------------------
    # Start / stop
    # --------------------------------------------------------
    def preflight(self):
        if not os.path.exists(YT_DLP):
            messagebox.showerror(
                "Downloader component missing",
                "yt-dlp.exe was not found.\n\nPlace the portable EXE files in the same folder "
                "as this application. No system installation is required.")
            return None

        optional = [n for n, p in (("ffmpeg.exe", FFMPEG), ("deno.exe", DENO)) if not os.path.exists(p)]
        if optional and not self.warned_optional:
            self.warned_optional = True
            if not messagebox.askyesno(
                    "Optional components missing",
                    "Not found beside the app: " + ", ".join(optional) +
                    "\n\nHigh-quality merging and some sites (e.g. YouTube) may fail without them."
                    "\n\nContinue anyway?"):
                return None

        folder = self.download_folder.get().strip()
        if not folder:
            messagebox.showwarning("Folder", "Choose a download folder first.")
            return None
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Folder", f"Cannot use this folder:\n{exc}")
            return None
        return folder

    def paste_yt(self):
        try:
            data = self.root.clipboard_get().strip()
        except tk.TclError:
            data = ""
        if not data:
            messagebox.showwarning("Paste Link", "There is no text in the clipboard.")
            return
        self.yt_var.set(data.splitlines()[0].strip())

    def start_download(self):
        if self.is_downloading:
            return
        platform = self.platform_var.get()
        folder = self.preflight()
        if not folder:
            return

        # ---- YouTube: expand one channel/Shorts link into up to N videos ----
        if platform == "YouTube":
            url = self.yt_var.get().strip()
            if not url:
                messagebox.showwarning("No link", "Paste a YouTube channel Shorts link first.")
                return
            if not is_youtube_url(url):
                messagebox.showwarning("Not a YouTube link", "That doesn't look like a YouTube link.")
                return
            try:
                limit = int(self.limit_var.get())
            except ValueError:
                limit = MAX_YT_VIDEOS
            limit = max(1, min(MAX_YT_VIDEOS, limit))
            self.limit_var.set(str(limit))

            for c in self.cards:
                c.destroy()
            self.cards.clear()
            self.refresh_list()
            self.set_overall(0)
            self.run_batch(None, folder, expand=(normalize_youtube_url(url), limit))
            return

        # ---- TikTok / Other: one link per line ----
        urls = []
        for line in self.url_box.get("1.0", tk.END).splitlines():
            line = line.strip()
            if line and line not in urls:
                urls.append(line)
        if not urls:
            messagebox.showwarning("No links", "Paste at least one video URL.")
            return

        if platform == "TikTok":
            good = [u for u in urls if is_tiktok_url(u)]
            skipped = len(urls) - len(good)
            if skipped:
                if not good:
                    messagebox.showwarning("TikTok", "None of these look like TikTok links.")
                    return
                if not messagebox.askyesno(
                        "TikTok",
                        f"{skipped} link(s) are not TikTok links and will be skipped.\n\nContinue?"):
                    return
                urls = good

        for c in self.cards:
            c.destroy()
        self.cards.clear()
        for i, url in enumerate(urls, 1):
            self.create_card(i, url)
        self.refresh_list()
        self.canvas.yview_moveto(0)

        self.run_batch(list(self.cards), folder)

    def add_cards(self, items, holder, ready):
        # Runs on the UI thread: builds one card per video found in the channel.
        for c in self.cards:
            c.destroy()
        self.cards.clear()

        cards = []
        for i, it in enumerate(items, 1):
            card = self.create_card(i, it["url"])
            card.known = {"title": it.get("title") or "", "duration": it.get("duration"),
                          "extractor_key": "YouTube"}
            dur = fmt_duration(it.get("duration"))
            meta = "  •  ".join(p for p in ["YouTube", dur, "Waiting in queue"] if p)
            card.update("waiting", 0, "--", "--", title=it.get("title") or None, meta=meta)
            cards.append(card)

        self.refresh_list()
        self.canvas.yview_moveto(0)
        holder["cards"] = cards
        ready.set()

    def on_expand_failed(self, message):
        self.is_downloading = False
        self.set_running(False)
        self.set_status(trunc(message, 110), RED)
        messagebox.showerror("YouTube", f"Could not read that link.\n\n{message}")

    def run_batch(self, batch, folder, expand=None):
        self.batch_id += 1
        bid = self.batch_id
        self.stop_event = threading.Event()

        # Snapshot Tk variables on the main thread.
        mode = self.mode_var.get()
        quality = self.quality_var.get()
        fmt = self.format_var.get()
        browser = self.cookies_var.get()
        net = self.net_var.get()
        if browser == "Off" or self.platform_var.get() == "Other":
            browser = None

        self.is_downloading = True
        self.set_running(True)
        self.set_overall(0)
        if expand:
            self.set_status("Reading channel…", ORANGE)
        else:
            self.set_status(f"Downloading {len(batch)} item(s)…", ORANGE)

        threading.Thread(
            target=self.worker,
            args=(bid, self.stop_event, batch, folder, mode, quality, fmt, browser, expand, net),
            daemon=True,
        ).start()

    def stop_download(self):
        if not self.is_downloading:
            return
        self.batch_id += 1          # invalidates any queued events from the old worker
        self.stop_event.set()
        proc = self.current_process
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        for c in self.cards:
            if c.status in ("waiting", "active", "processing"):
                c.update("stopped", meta="Stopped by user")
        self.is_downloading = False
        self.set_running(False)
        self.refresh_list()
        self.set_status("Stopped", RED)

    # --------------------------------------------------------
    # Thread-safe UI plumbing
    # --------------------------------------------------------
    def poll(self):
        try:
            while True:
                bid, fn, args = self.ui_queue.get_nowait()
                if bid is None or bid == self.batch_id:
                    try:
                        fn(*args)
                    except tk.TclError:
                        pass
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def apply_update(self, card, status, percent, speed, eta, title=None, meta=None):
        if card not in self.cards or not card.winfo_exists():
            return
        changed = card.status != status
        card.update(status, percent, speed, eta, title, meta)
        if changed:
            self.update_counts()
            if self.filter != "All":
                self.refresh_list()
            if status == "active":
                self.scroll_to(card)

    # --------------------------------------------------------
    # App update (GitHub Releases)
    # --------------------------------------------------------
    def check_for_updates(self):
        if self.app_update_checking:
            return
        if self.is_downloading:
            messagebox.showinfo(
                "Check for Updates",
                "Press Stop or wait for the current download to finish first."
            )
            return
        if not getattr(sys, "frozen", False):
            messagebox.showinfo(
                "Check for Updates",
                "In-app updates are available in the packaged Windows EXE.\n\n"
                "Run the released VideoDownloader.exe to test the updater."
            )
            return

        self.app_update_checking = True
        self.app_update_btn.set_enabled(False)
        self.app_update_btn.set_text("Checking…")
        self.set_status("Checking for app updates…", BLUE)
        threading.Thread(target=self._app_update_worker, daemon=True).start()

    def _app_update_worker(self):
        try:
            outcome = ("ok", fetch_latest_release())
        except UpdateCheckError as exc:
            outcome = ("error", str(exc))
        except Exception as exc:
            outcome = ("error", f"Unexpected problem: {exc}")
        self.ui_queue.put((None, self._app_update_done, (outcome,)))

    def _app_update_done(self, outcome):
        self.app_update_checking = False
        self.app_update_btn.set_enabled(True)
        self.app_update_btn.set_text("Check for Updates")
        self.root.after(30, lambda: self._show_update_result(outcome))

    def _show_update_result(self, outcome):
        kind, payload = outcome
        title = "Check for Updates"
        try:
            if kind == "error":
                if messagebox.askretrycancel(
                        title, f"Could not check for updates.\n\n{payload}\n\nTry again?"):
                    self.check_for_updates()
                else:
                    self.set_status("Update check failed", RED)
                return

            tag = payload["tag"]
            latest = tag.lstrip("vV")
            status = evaluate_update(tag)

            if status == "newer":
                if messagebox.askyesno(
                        "Update available",
                        "A newer version of Video Downloader is available.\n\n"
                        f"Installed version:  {APP_VERSION}\n"
                        f"Latest version:  {latest}\n\n"
                        "Download and install it now?"):
                    self._start_app_update(payload)
                else:
                    self.set_status("Update postponed", MUTED)
            elif status == "uptodate":
                messagebox.showinfo(
                    title,
                    "You're up to date!\n\n"
                    f"Installed version:  {APP_VERSION}\n"
                    f"Latest release:  {latest}")
                self.set_status("Up to date", ACCENT)
            else:
                messagebox.showwarning(
                    title,
                    f"The latest GitHub release is tagged \"{tag}\", which this app "
                    "cannot compare safely.\n\nPlease check the release manually.")
                self.set_status("Could not compare release version", ORANGE)
        except Exception as exc:
            print("Update dialog problem:", exc)
            self.set_status("Update check failed", RED)

    def _start_app_update(self, release):
        self.app_update_checking = True
        self.app_update_btn.set_enabled(False)
        self.app_update_btn.set_text("Downloading…")
        self.set_status(f"Downloading Video Downloader {release['tag'].lstrip('vV')}…", BLUE)
        threading.Thread(
            target=self._app_update_download_worker,
            args=(release,),
            daemon=True,
        ).start()

    def _app_update_progress(self, percent, downloaded, total):
        if total:
            self.app_update_btn.set_text(f"Updating {percent:.0f}%")
            self.set_status(
                f"Downloading update… {percent:.0f}%  •  "
                f"{self._format_bytes(downloaded)} / {self._format_bytes(total)}",
                BLUE,
            )
        else:
            self.app_update_btn.set_text("Downloading…")
            self.set_status(
                f"Downloading update… {self._format_bytes(downloaded)}",
                BLUE,
            )

    @staticmethod
    def _format_bytes(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "0 B"
        units = ("B", "KB", "MB", "GB")
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{value:.1f} GB"

    def _app_update_download_worker(self, release):
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(prefix="VideoDownloader-update-")
            zip_path = os.path.join(temp_dir, release.get("asset_name", APP_UPDATE_ASSET_NAME))

            req = urllib.request.Request(
                release["asset_url"],
                headers={
                    "Accept": "application/octet-stream",
                    "User-Agent": f"VideoDownloader/{APP_VERSION}",
                },
            )
            with urllib.request.urlopen(req, timeout=APP_UPDATE_DOWNLOAD_TIMEOUT) as resp:
                total_header = resp.headers.get("Content-Length")
                try:
                    total = int(total_header) if total_header else int(release.get("asset_size") or 0)
                except (TypeError, ValueError):
                    total = 0

                downloaded = 0
                last_reported = -1
                with open(zip_path, "wb") as out:
                    while True:
                        chunk = resp.read(APP_UPDATE_CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            percent = min(100, downloaded * 100 / total)
                            whole = int(percent)
                            if whole != last_reported:
                                last_reported = whole
                                self.ui_queue.put((
                                    None, self._app_update_progress,
                                    (percent, downloaded, total),
                                ))
                        elif downloaded % (APP_UPDATE_CHUNK_SIZE * 4) == 0:
                            self.ui_queue.put((
                                None, self._app_update_progress,
                                (0, downloaded, 0),
                            ))

            if not os.path.isfile(zip_path) or os.path.getsize(zip_path) < 1024:
                raise UpdateCheckError("The downloaded update file is incomplete.")

            self.ui_queue.put((None, self._app_update_download_done,
                               ("ok", zip_path, temp_dir, release)))
        except urllib.error.HTTPError as exc:
            msg = f"GitHub returned an error while downloading the update (HTTP {exc.code})."
            self.ui_queue.put((None, self._app_update_download_done,
                               ("error", msg, temp_dir, release)))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                msg = "The update download timed out. Please try again."
            else:
                msg = "Could not download the update. Check your internet connection."
            self.ui_queue.put((None, self._app_update_download_done,
                               ("error", msg, temp_dir, release)))
        except (socket.timeout, TimeoutError):
            self.ui_queue.put((None, self._app_update_download_done,
                               ("error", "The update download timed out. Please try again.", temp_dir, release)))
        except Exception as exc:
            self.ui_queue.put((None, self._app_update_download_done,
                               ("error", f"Could not download the update.\n\n{exc}", temp_dir, release)))

    def _app_update_download_done(self, outcome, path, temp_dir, release):
        if outcome != "ok":
            self.app_update_checking = False
            self.app_update_btn.set_enabled(True)
            self.app_update_btn.set_text("Check for Updates")
            self.set_status("Update download failed", RED)
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    import shutil
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
            messagebox.showerror("Update failed", path)
            return

        try:
            self._launch_app_updater(path, temp_dir, release)
        except Exception as exc:
            self.app_update_checking = False
            self.app_update_btn.set_enabled(True)
            self.app_update_btn.set_text("Check for Updates")
            self.set_status("Could not start updater", RED)
            messagebox.showerror(
                "Update failed",
                f"The update was downloaded, but the updater could not be started.\n\n{exc}",
            )

    @staticmethod
    def _ps_quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    def _launch_app_updater(self, zip_path, temp_dir, release):
        if not getattr(sys, "frozen", False):
            raise RuntimeError("In-app updating is only supported by the packaged Windows EXE.")
        if os.name != "nt":
            raise RuntimeError("In-app updating is currently supported on Windows only.")

        install_dir = os.path.dirname(os.path.abspath(sys.executable))
        target_exe = os.path.join(install_dir, os.path.basename(sys.executable))
        pid = os.getpid()
        stage_dir = os.path.join(temp_dir, "extracted")
        script_path = os.path.join(temp_dir, "install_update.ps1")
        log_path = os.path.join(temp_dir, "update.log")
        temp_dir_ps = self._ps_quote(temp_dir)

        script = f"""$ErrorActionPreference = \"Stop\"
$tempDir = {temp_dir_ps}
$pidToWait = {pid}
$zipPath = {self._ps_quote(zip_path)}
$stageDir = {self._ps_quote(stage_dir)}
$installDir = {self._ps_quote(install_dir)}
$targetExe = {self._ps_quote(target_exe)}
$logPath = {self._ps_quote(log_path)}
$version = {self._ps_quote(release.get("tag", "").lstrip("vV"))}

function Log($message) {{
    try {{ Add-Content -LiteralPath $logPath -Value (\"[\" + (Get-Date -Format s) + \"] \" + $message) }} catch {{}}
}}

try {{
    Log \"Waiting for Video Downloader process $pidToWait to exit.\"
    for ($i = 0; $i -lt 120; $i++) {{
        if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) {{ break }}
        Start-Sleep -Milliseconds 250
    }}
    if (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {{
        throw \"The old Video Downloader process did not exit in time.\"
    }}

    if (-not (Test-Path -LiteralPath $zipPath)) {{ throw \"Downloaded update package was not found.\" }}
    if (Test-Path -LiteralPath $stageDir) {{ Remove-Item -LiteralPath $stageDir -Recurse -Force }}
    New-Item -ItemType Directory -Path $stageDir -Force | Out-Null

    Log \"Extracting update package.\"
    Expand-Archive -LiteralPath $zipPath -DestinationPath $stageDir -Force

    $newExe = Get-ChildItem -LiteralPath $stageDir -Filter \"VideoDownloader.exe\" -File -Recurse | Select-Object -First 1
    if (-not $newExe) {{ throw \"The update package does not contain VideoDownloader.exe.\" }}
    $packageRoot = $newExe.Directory.FullName

    if (-not (Test-Path -LiteralPath $installDir)) {{ throw \"The application folder no longer exists.\" }}
    Log \"Copying update files from $packageRoot to $installDir.\"
    Copy-Item -Path (Join-Path $packageRoot \"*\") -Destination $installDir -Recurse -Force

    if (-not (Test-Path -LiteralPath $targetExe)) {{ throw \"The updated VideoDownloader.exe was not found after installation.\" }}
    Log \"Starting Video Downloader $version.\"
    Start-Process -FilePath $targetExe -WorkingDirectory $installDir
    Log \"Update completed successfully.\"
}} catch {{
    Log (\"Update failed: \" + $_.Exception.Message)
    try {{ Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show(
        \"Video Downloader could not finish the update.`n`n\" + $_.Exception.Message,
        \"Update failed\", \"OK\", \"Error\") | Out-Null }} catch {{}}
}} finally {{
    Start-Sleep -Seconds 2
    try {{ Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue }} catch {{}}
}}
"""
        with open(script_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(script)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
            cwd=install_dir,
            creationflags=creationflags,
            close_fds=True,
        )

        self.set_status(
            f"Installing Video Downloader {release.get('tag', '').lstrip('vV')}… The app will restart.",
            ACCENT,
        )
        self.app_update_btn.set_text("Installing…")
        self.root.after(700, self.root.destroy)

    def fetch_version(self):
        ver = ""
        if os.path.exists(YT_DLP):
            try:
                v = subprocess.run([YT_DLP, "--version"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30,
                                   creationflags=NO_WINDOW)
                ver = (v.stdout or "").strip()
            except Exception:
                pass
        self.ui_queue.put((None, self.set_version, (ver,)))

    def set_version(self, ver):
        self.ver_label.config(text=f"yt-dlp {ver}" if ver else "")

    def update_ytdlp(self):
        if self.is_downloading:
            messagebox.showinfo("Update yt-dlp",
                                "Press Stop or wait for the current download to finish first.")
            return
        if not os.path.exists(YT_DLP):
            messagebox.showerror("Update yt-dlp", "yt-dlp.exe was not found beside the app.")
            return
        choice = messagebox.askyesnocancel(
            "Update yt-dlp",
            "Which build do you want?\n\n"
            "YES  =  Nightly  (newest YouTube fixes — try this if downloads fail)\n"
            "NO   =  Stable\n"
            "CANCEL  =  do nothing")
        if choice is None:
            return
        channel = "nightly" if choice else "stable"
        self.update_btn.set_enabled(False)
        self.update_btn.set_text("Updating…")
        self.set_status("Updating yt-dlp…", ORANGE)
        threading.Thread(target=self._update_worker, args=(channel,), daemon=True).start()

    def _update_worker(self, channel):
        out, ver = "", ""
        try:
            r = subprocess.run([YT_DLP, "--update-to", channel], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=240,
                               creationflags=NO_WINDOW)
            out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
            v = subprocess.run([YT_DLP, "--version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30,
                               creationflags=NO_WINDOW)
            ver = (v.stdout or "").strip()
        except Exception as exc:
            out = str(exc)
        self.ui_queue.put((None, self._update_done, (out, ver)))

    def _update_done(self, out, ver):
        self.update_btn.set_enabled(True)
        self.update_btn.set_text("⟳  Update yt-dlp")
        self.set_status(f"yt-dlp version {ver}" if ver else "Update failed", ACCENT if ver else RED)
        self.set_version(ver)
        messagebox.showinfo("Update yt-dlp",
                            (f"Current version: {ver}\n\n" if ver else "") + trunc(out, 700))

    def on_finished(self, completed, failed, total):
        self.is_downloading = False
        self.set_running(False)
        self.set_overall(100)
        text = f"Finished  •  {completed}/{total} completed"
        if failed:
            text += f"  •  {failed} failed  (details: download_log.txt)"
        self.set_status(text, ACCENT if not failed else ORANGE)
        self.root.bell()

    # --------------------------------------------------------
    # Worker thread
    # --------------------------------------------------------
    def run_download(self, bid, stop, card, command, pos, total):
        # Runs one yt-dlp process, streams progress to the UI, returns (code, last_error, log_lines)
        def post(fn, *args):
            self.ui_queue.put((bid, fn, args))

        lines = []
        last_error = ""
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW)
        self.current_process = proc
        try:
            for raw in proc.stdout:
                if stop.is_set():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break

                line = raw.strip()
                progress = parse_progress(line)
                if line and not progress:
                    lines.append(line)

                if "ERROR:" in line:
                    last_error = line.split("ERROR:", 1)[1].strip()

                if re.search(r"\[(Merger|ExtractAudio|VideoRemuxer|VideoConvertor|Fixup\w*)\]", line):
                    post(self.apply_update, card, "processing", 100, "--", "--")

                if progress:
                    percent, speed, eta = progress
                    post(self.apply_update, card, "active", percent, speed, eta)
                    post(self.set_overall, (pos + percent / 100) / total * 100)

            code = proc.wait()
        finally:
            if self.current_process is proc:
                self.current_process = None
        return code, last_error, lines[-300:]

    def worker(self, bid, stop, batch, folder, mode, quality, fmt, browser, expand=None, net="Auto"):
        completed = 0
        failed = 0
        used_names = set()

        def post(fn, *args):
            self.ui_queue.put((bid, fn, args))

        # Step 1 (YouTube): list the videos behind the channel link
        if expand:
            url, limit = expand
            post(self.set_status, f"Reading channel (up to {limit} videos)…", ORANGE)
            items, err = expand_playlist(url, limit)
            if stop.is_set():
                return
            if not items:
                post(self.on_expand_failed, err or "No videos found at that link")
                return

            holder = {}
            ready = threading.Event()
            post(self.add_cards, items, holder, ready)
            while not ready.wait(0.1):
                if stop.is_set():
                    return
            batch = holder["cards"]
            post(self.set_status, f"Downloading {len(batch)} video(s)…", ORANGE)

        total = len(batch)
        strategies = yt_strategies(quality)
        net_args = {"IPv4": ["--force-ipv4"], "IPv6": ["--force-ipv6"]}.get(net, [])
        best_idx = 0
        fail_streak = 0

        for pos, card in enumerate(batch):
            if stop.is_set():
                break

            url = card.url
            last_error = ""

            if pos > 0 and is_tiktok_url(url):
                for _ in range(2):
                    if stop.is_set():
                        break
                    time.sleep(1)

            post(self.apply_update, card, "active", 0, "--", "--", None, "Retrieving information…")

            # Titles from the channel listing are reused (skips a slow lookup per video).
            known = getattr(card, "known", None)
            info = dict(known) if known else get_info(url, browser)
            if stop.is_set():
                break

            title_text = info.get("title", "") or ""
            if is_generic_title(title_text):
                title_text = ""

            if mode == "Audio":
                ext = "mp3" if fmt == "MP3" else "m4a"
            elif fmt == "WEBM":
                ext = "webm"
            else:
                ext = "mp4"

            base_name = clean_filename(title_text) if title_text else f"Video {card.number}"
            final_name = base_name
            n = 1
            while (final_name.lower() in used_names
                   or os.path.exists(os.path.join(folder, f"{final_name}.{ext}"))):
                final_name = f"{base_name} ({n})"
                n += 1
            used_names.add(final_name.lower())

            output_template = os.path.join(folder, f"{final_name}.%(ext)s")

            site = info.get("extractor_key") or site_info(url)[0]
            parts = [site, info.get("uploader") or info.get("channel"), fmt_duration(info.get("duration"))]
            meta = "  •  ".join(str(p) for p in parts if p)

            post(self.apply_update, card, "active", 0, "--", "--", final_name, meta)

            youtube = is_youtube_url(url)
            if youtube:
                order = list(range(len(strategies)))
                order = order[best_idx:] + order[:best_idx]   # start with what worked last
                if fail_streak >= 2:
                    order = order[:1]                          # don't waste time if nothing works
            else:
                order = [None]

            code, last_error, log_lines, used_idx = -1, "", [], None
            try:
                for attempt, sidx in enumerate(order):
                    if stop.is_set():
                        break
                    extra = strategies[sidx] if sidx is not None else []
                    if attempt > 0:
                        post(self.apply_update, card, "active", 0, "--", "--", None,
                             f"Retrying with another method ({attempt}/{len(order) - 1})…")

                    command = build_command(url, output_template, mode, quality, fmt, browser)
                    command = command[:-3] + net_args + extra + command[-3:]

                    code, last_error, lines = self.run_download(bid, stop, card, command, pos, total)
                    log_lines += ["", f"=== attempt {attempt + 1}: {' '.join(extra) or 'default'} ==="] + lines

                    if code == 0:
                        used_idx = sidx
                        break
                    if "403" not in last_error and "Forbidden" not in last_error:
                        break

                if stop.is_set():
                    break

                if code == 0:
                    completed += 1
                    fail_streak = 0
                    if used_idx is not None:
                        best_idx = used_idx
                    saved = (meta + "  •  " if meta else "") + "Saved"
                    post(self.apply_update, card, "done", 100, "--", "--", None, saved)
                else:
                    failed += 1
                    fail_streak += 1
                    reason = last_error or "yt-dlp could not download this URL"
                    write_log(url, log_lines)
                    post(self.apply_update, card, "failed", 0, "--", "--", None, reason)

            except Exception as exc:
                failed += 1
                post(self.apply_update, card, "failed", 0, "--", "--", None, f"Error: {exc}")

            post(self.set_overall, (pos + 1) / total * 100)

        if not stop.is_set():
            post(self.on_finished, completed, failed, total)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()