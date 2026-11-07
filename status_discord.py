"""
Discord Lyrics Status (Discord custom status only, no overlay)

Updates your Discord CUSTOM STATUS (the bubble next to your avatar)
with synchronized lyrics from the currently playing Spotify desktop track.

IMPORTANT WARNING
  Discord provides no official API to update custom status programmatically.
  This script uses your personal account token ("self-bot"), which violates
  Discord's Terms of Service and may result in account limitation or termination.
  Use at your own risk.
  Your token grants FULL access to your account: NEVER share it, and do not
  post it in screenshots or commit it to GitHub.

Token lookup order:
  1. Environment variable DISCORD_TOKEN
  2. discord_token.txt file (in the same directory as this script)
  3. Interactive console prompt when the script starts.

Exit: Ctrl+C or Ctrl+Shift+Q (restores original custom status)
Shift timing: Ctrl+Shift+Left / Ctrl+Shift+Right
"""
import asyncio
import ctypes
import getpass
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from datetime import datetime, timezone

try:
    import winrt.windows.media.control as media_control
    WINRT_OK = True
except ImportError:
    media_control = None
    WINRT_OK = False

# =====================================================================
# SETTINGS
# =====================================================================
EMOJI = "🎵"                  # status emoji prefix ("" = no emoji)
LYRIC_LEAD = 0.5              # seconds: display lyric slightly ahead to compensate for Discord delay
MIN_SEND_INTERVAL = 3.0       # minimum delay between Discord updates (do not set lower!)
OFFSET_STEP = 0.25
SPOTIFY_POLL_SECONDS = 0.5
SPOTIFY_MAX_DRIFT = 1.0
PAUSE_GRACE_SECONDS = 2.5     # grace period before restoring original status when track is paused
LYRIC_HTTP_TIMEOUT = 6.0
SHOW_TITLE_WHEN_NO_LYRICS = True    # if lyrics not found: show "Title - Artist"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lyrics_cache")
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discord_token.txt")
OFFSET_KEY = "status_offset"
API = "https://discord.com/api/v9/users/@me"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(*args):
    print("[status]", *args, flush=True)


def key_down(vk):
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


# =====================================================================
# Lyrics (LRCLIB + cache, shared with overlay)
# =====================================================================
def cache_path(artist, title, create=True):
    if create:
        os.makedirs(CACHE_DIR, exist_ok=True)
    raw = json.dumps([artist.casefold().strip(), title.casefold().strip()], ensure_ascii=False)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{artist}_{title}").strip("._-")[:80] or "track"
    return os.path.join(CACHE_DIR, f"{slug}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}.json")


def read_cache(artist, title):
    try:
        with open(cache_path(artist, title, create=False), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_cache_fields(artist, title, **fields):
    try:
        data = read_cache(artist, title)
        data.update(fields)
        data["artist"], data["title"] = artist, title
        with open(cache_path(artist, title), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except (OSError, ValueError):
        pass


def parse_lrc(text):
    lines = []
    pattern = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
    for line in (text or "").splitlines():
        matches = list(pattern.finditer(line))
        if not matches:
            continue
        body = line[matches[-1].end():].strip()
        if not body:
            continue
        for m in matches:
            minutes, seconds, frac = m.groups()
            fs = int(frac) / (10 ** len(frac)) if frac else 0.0
            lines.append((int(minutes) * 60 + int(seconds) + fs, body))
    return sorted(lines, key=lambda row: row[0])


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "DiscordLyricsStatus/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=LYRIC_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def clean_title(title):
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)
    t = re.sub(r"\s+[-–]\s+(remaster|live|mono|stereo|version|single|radio|\d{4}).*$", "", t, flags=re.I)
    return t.strip() or title


def fetch_lyrics(title, artist, album, duration):
    cached = read_cache(artist, title).get("parsed")
    if cached:
        return [(float(t), str(s)) for t, s in cached]

    base = "https://lrclib.net/api/"
    text = None
    net_errors = 0
    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration > 0:
        params["duration"] = round(duration)
    try:
        text = http_json(base + "get?" + urllib.parse.urlencode(params)).get("syncedLyrics")
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            net_errors += 1
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        net_errors += 1

    if not text:
        for query in (f"{artist} {title}", f"{artist} {clean_title(title)}"):
            try:
                results = http_json(base + "search?" + urllib.parse.urlencode({"q": query}))
                found = [r for r in results if r.get("syncedLyrics")]
                if duration > 0:
                    found.sort(key=lambda r: abs(float(r.get("duration") or 0) - duration))
                if found:
                    text = found[0]["syncedLyrics"]
                    break
            except urllib.error.HTTPError as e:
                if e.code >= 500:
                    net_errors += 1
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
                net_errors += 1

    parsed = parse_lrc(text)
    if not parsed and net_errors >= 2:
        raise ConnectionError("Unable to reach LRCLIB (check your internet connection)")
    if parsed:   # empty results are not cached so they can be retried later
        write_cache_fields(artist, title, album=album, duration=duration, parsed=parsed)
    return parsed


# =====================================================================
# Spotify (Windows Media Control)
# =====================================================================
def to_seconds(value):
    if value is None:
        return 0.0
    if hasattr(value, "total_seconds"):
        return value.total_seconds()
    ticks = getattr(value, "duration", None)
    return ticks / 10_000_000 if ticks is not None else 0.0


class SpotifyDetector(threading.Thread):
    def __init__(self, out_queue):
        super().__init__(daemon=True)
        self.out = out_queue
        self.running = True
        self.last_err = None

    def stop(self):
        self.running = False

    async def _request_manager(self):
        return await media_control.GlobalSystemMediaTransportControlsSessionManager.request_async()

    def run(self):
        if not WINRT_OK:
            self.out.put(("ERROR", "winrt package is not installed. Run: pip install -r requirements.txt"))
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        manager = None
        try:
            while self.running:
                try:
                    if manager is None:
                        manager = loop.run_until_complete(self._request_manager())
                    data = loop.run_until_complete(self._read(manager))
                    self.out.put(("DATA", data))
                    self.last_err = None
                except Exception as e:   # noqa: BLE001
                    manager = None
                    msg = f"{type(e).__name__}: {e}"
                    if msg != self.last_err:
                        log("Failed to read Spotify ->", msg)
                        self.last_err = msg
                    self.out.put(("IDLE", None))
                time.sleep(SPOTIFY_POLL_SECONDS)
        finally:
            loop.close()

    async def _read(self, manager):
        session = None
        try:
            cur = manager.get_current_session()
            if cur and "spotify" in (cur.source_app_user_model_id or "").casefold():
                session = cur
        except Exception:
            pass
        if session is None:
            for cand in manager.get_sessions():
                if "spotify" in (cand.source_app_user_model_id or "").casefold():
                    session = cand
                    break
        if session is None:
            return None

        props = await session.try_get_media_properties_async()
        title = (props.title or "").strip()
        if not title:
            return None
        artist = (props.artist or "").strip()
        album = (props.album_title or "").strip()

        timeline = session.get_timeline_properties()
        position = to_seconds(timeline.position)
        duration = to_seconds(timeline.end_time)

        status = "stopped"
        info = session.get_playback_info()
        if info is not None and info.playback_status is not None:
            value = int(info.playback_status)
            status = "playing" if value == 4 else "paused" if value == 5 else "stopped"

        if status == "playing":
            updated = getattr(timeline, "last_updated_time", None)
            if isinstance(updated, datetime):
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                extra = (datetime.now(timezone.utc) - updated).total_seconds()
                if 0 <= extra <= 600 and (duration <= 0 or position + extra <= duration + 1):
                    position += extra

        return {"title": title, "artist": artist, "album": album, "duration": duration,
                "position": max(0.0, position), "status": status, "timestamp": time.perf_counter()}


class LyricsWorker(threading.Thread):
    def __init__(self, track, out_queue):
        super().__init__(daemon=True)
        self.track, self.out = track, out_queue

    def run(self):
        t = self.track
        try:
            self.out.put((t["id"], fetch_lyrics(t["title"], t["artist"], t["album"], t["duration"]), None))
        except ConnectionError as e:
            self.out.put((t["id"], [], str(e)))
        except Exception as e:   # noqa: BLE001
            self.out.put((t["id"], [], f"Failed to fetch lyrics: {type(e).__name__}"))


# =====================================================================
# Discord custom status
# =====================================================================
class DiscordStatus(threading.Thread):
    """Sends custom status updates in a dedicated thread. Always dispatches the LATEST text without queue buildup."""
    def __init__(self, token):
        super().__init__(daemon=True)
        self.token = token
        self.lock = threading.Lock()
        self.desired = None          # None = restore original status
        self.sent = None
        self.original = None
        self.last_send = 0.0
        self.stop_event = threading.Event()
        self.fatal = False

    def set_text(self, text):
        with self.lock:
            self.desired = text

    def stop(self):
        self.stop_event.set()

    def request(self, method, url, payload=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": self.token, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        for _ in range(3):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode("utf-8") or "{}"
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    try:
                        wait = float(json.loads(e.read().decode("utf-8")).get("retry_after", 3))
                    except Exception:
                        wait = 3.0
                    log(f"Discord rate limit reached, waiting {wait:.1f}s")
                    self.stop_event.wait(wait + 0.5)
                    continue
                if e.code in (401, 403):
                    self.fatal = True
                    log(f"Discord rejected token (HTTP {e.code}). Check your token or account restrictions.")
                else:
                    log(f"Discord HTTP error {e.code}")
                return None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                log(f"Failed to connect to Discord: {type(e).__name__}")
                return None
        return None

    def verify(self):
        me = self.request("GET", API)
        if not me:
            return False
        log(f"Logged in as: {me.get('global_name') or me.get('username')}")
        settings = self.request("GET", API + "/settings")
        self.original = (settings or {}).get("custom_status")
        return True

    def _send(self, text):
        if text:
            cs = {"text": text[:128]}
            if EMOJI:
                cs["emoji_name"] = EMOJI
            payload = {"custom_status": cs}
        else:
            payload = {"custom_status": self.original}      # None = clear/restore
        ok = self.request("PATCH", API + "/settings", payload) is not None
        self.last_send = time.monotonic()
        if ok:
            self.sent = text
            log(f"Status -> {text[:70]}" if text else "Status restored to original")
        return ok

    def run(self):
        while not self.stop_event.is_set() and not self.fatal:
            with self.lock:
                want = self.desired
            if want != self.sent and time.monotonic() - self.last_send >= MIN_SEND_INTERVAL:
                self._send(want)
            self.stop_event.wait(0.2)
        if self.sent is not None and not self.fatal:      # restore original status upon exit
            self._send(None)


def load_token():
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
    if not token:
        token = getpass.getpass("Paste Discord token (hidden during input): ").strip()
    return token.strip().strip('"')


# =====================================================================
# Main runner
# =====================================================================
class Runner:
    def __init__(self, discord):
        self.discord = discord
        self.spotify_queue, self.lyrics_queue = queue.Queue(), queue.Queue()
        self.detector = SpotifyDetector(self.spotify_queue)
        self.track_id = None
        self.title = self.artist = ""
        self.lyrics, self.times = [], []
        self.lyrics_ready = False
        self.status = "stopped"
        self.sync_pos, self.sync_perf = 0.0, time.perf_counter()
        self.offset = 0.0
        self.not_playing_since = None
        self.prev_left = self.prev_right = False

    def position(self):
        if self.status == "playing":
            return max(0.0, self.sync_pos + time.perf_counter() - self.sync_perf)
        return max(0.0, self.sync_pos)

    def on_track(self, data):
        self.track_id = data["id"]
        self.title, self.artist = data["title"], data["artist"]
        self.lyrics, self.times, self.lyrics_ready = [], [], False
        self.status = data["status"]
        self.sync_pos, self.sync_perf = data["position"], data["timestamp"]
        self.offset = float(read_cache(self.artist, self.title).get(OFFSET_KEY, 0.0))
        log(f"Track: {self.artist} - {self.title} ({self.status}) - searching lyrics...")
        LyricsWorker(data, self.lyrics_queue).start()

    def handle_media(self, data):
        if data is None:
            self.status = "stopped" if self.status != "playing" else "paused"
            return
        tid = f"{data['artist']}\0{data['title']}"
        if tid != self.track_id:
            self.on_track(dict(data, id=tid))
            return
        drift = abs(self.position() - data["position"])
        changed = data["status"] != self.status
        self.status = data["status"]
        if changed or drift > SPOTIFY_MAX_DRIFT:
            self.sync_pos, self.sync_perf = data["position"], data["timestamp"]

    def process_queues(self):
        while True:
            try:
                kind, payload = self.spotify_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "DATA":
                self.handle_media(payload)
            elif kind == "IDLE":
                self.handle_media(None)
            elif kind == "ERROR":
                log(payload)
                return False
        while True:
            try:
                tid, lines, error = self.lyrics_queue.get_nowait()
            except queue.Empty:
                break
            if tid != self.track_id:
                continue
            self.lyrics, self.times = lines, [t for t, _ in lines]
            self.lyrics_ready = True
            if error:
                log(error)
            elif not lines:
                log("Synced lyrics not found on LRCLIB")
            else:
                log(f"Lyrics found: {len(lines)} lines")
        return True

    def check_hotkeys(self):
        cs = key_down(0x11) and key_down(0x10)
        left, right = cs and key_down(0x25), cs and key_down(0x27)
        delta = (-OFFSET_STEP if left and not self.prev_left else 0.0) + \
                (OFFSET_STEP if right and not self.prev_right else 0.0)
        self.prev_left, self.prev_right = left, right
        if delta and self.title:
            self.offset += delta
            write_cache_fields(self.artist, self.title, **{OFFSET_KEY: self.offset})
            log(f"Lyric offset: {self.offset:+.2f}s")

    def desired_text(self):
        if not self.lyrics_ready:
            return f"{self.title} - {self.artist}" if self.title else None
        if not self.lyrics:
            return f"{self.title} - {self.artist}" if SHOW_TITLE_WHEN_NO_LYRICS else None
        pos = self.position() + self.offset + LYRIC_LEAD
        i = bisect_right(self.times, pos) - 1
        return self.lyrics[i][1] if i >= 0 else f"{self.title} - {self.artist}"

    def loop(self):
        self.detector.start()
        log("Running. Play a song on Spotify desktop. Exit: Ctrl+C or Ctrl+Shift+Q")
        while True:
            if sys.platform == "win32" and key_down(0x11) and key_down(0x10) and key_down(0x51):
                return
            if not self.process_queues():
                return
            self.check_hotkeys()

            now = time.monotonic()
            if self.status == "playing":
                self.not_playing_since = None
                self.discord.set_text(self.desired_text())
            else:
                if self.not_playing_since is None:
                    self.not_playing_since = now
                if now - self.not_playing_since >= PAUSE_GRACE_SECONDS:
                    self.discord.set_text(None)
            if self.discord.fatal:
                return
            time.sleep(0.1)


def main():
    if sys.platform != "win32":
        log("This script is for Windows only.")
        return
    token = load_token()
    if not token:
        log("Token is empty.")
        return
    discord = DiscordStatus(token)
    if not discord.verify():
        log("Invalid token or unable to connect to Discord. Exiting.")
        return
    discord.start()
    runner = Runner(discord)
    try:
        runner.loop()
    except KeyboardInterrupt:
        pass
    finally:
        runner.detector.stop()
        discord.stop()
        discord.join(timeout=15)
        log("Finished.")


if __name__ == "__main__":
    main()
