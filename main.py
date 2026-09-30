"""Daily streamer-news YouTube Shorts bot.

Pipeline:
  Google News -> Gemini picks the most viral story
  -> finds that streamer's Twitch/Kick clips -> transcribes them (Whisper)
  -> Gemini picks the clip + exact seconds that show the moment and writes intro/outro
  -> [narrated intro] + [the REAL clip with its own audio + captions] + [narrated outro]
  -> FFmpeg (captions, zooms, whooshes, music) -> YouTube upload.
"""
import asyncio
import base64
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import edge_tts
import requests

ROOT = Path(__file__).parent
WORK = ROOT / "work"
HISTORY = ROOT / "history.json"
UA = {"User-Agent": "Mozilla/5.0 (shorts-bot)"}

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
VOICE = os.environ.get("TTS_VOICE", "en-US-AndrewNeural")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small.en")
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # build video but skip upload

FORMATS = [
    "breaking news recap: what happened, why it matters",
    "drama recap told like a story with a twist at the end",
    "records, numbers and stats angle",
    "'things you didn't know' about the streamer(s) involved, using only facts in the sources",
]


# ---------- 1. News ----------
# The names people actually search for. Stories about them get far more views.
BIG_STREAMERS = [
    "Kai Cenat", "IShowSpeed", "xQc", "Adin Ross", "Pokimane", "Ninja", "MrBeast", "Dr Disrespect",
    "Hasan Piker", "Asmongold", "Ludwig", "Valkyrae", "Sketch", "Jynxzi", "Duke Dennis", "Fanum",
    "Agent 00", "Caseoh", "Tfue", "Shroud", "TimTheTatman", "Summit1g", "Amouranth", "Sneako",
    "N3on", "Clix", "Plaqueboymax", "Stable Ronaldo", "Lacy", "Emiru", "Ibai", "Mizkif",
    "Moistcr1tikal", "Tyler1", "Sykkuno", "Faze Banks", "Jake Paul", "Kick streamer",
]
HOOK_WORDS = ["banned", "record", "drama", "arrested", "leaves", "quits", "million", "lawsuit",
              "apologizes", "reacts", "exposed", "breaks", "subathon", "signs", "returns", "fight",
              "heated", "confronts", "calls out", "responds"]
# Names that also mean other things (Ninja blenders, BBC's "Ludwig", ...) need a streaming word nearby.
AMBIGUOUS = {"ninja", "ludwig", "sketch", "lacy", "shroud", "clix", "ibai", "fanum", "kick streamer",
             "marlon", "silky", "cuffem", "kishka", "emiru", "tfue", "agent 00", "fanum"}
STREAM_WORDS = ["stream", "twitch", "kick", "youtuber", "subathon", "clip", "creator", "influencer", "chat"]
JUNK_WORDS = ["air fryer", "blender", "knife", "deal", "sale", "% off", "review:", "prime day",
              "appliance", "vacuum", "cookware", "stock price", "earnings", "net worth", "who is"]


def fetch_google_news(names):
    """Last-2-days headlines about these streamers (context for their clips)."""
    queries = [(f'"{n}"', n.lower()) for n in names]
    items, seen = [], set()
    for q, name in queries:
        try:
            url = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}+when:2d&hl=en-US&gl=US&ceid=US:en"
            root = ET.fromstring(requests.get(url, headers=UA, timeout=15).content)
            for it in list(root.iter("item"))[:8]:
                title = it.findtext("title") or ""
                low = title.lower()
                if low in seen or any(w in low for w in JUNK_WORDS):
                    continue
                if name and (name not in low or
                             (name in AMBIGUOUS and not any(w in low for w in STREAM_WORDS))):
                    continue
                seen.add(low)
                items.append({"title": title, "url": it.findtext("link"),
                              "text": f"({it.findtext('pubDate')}) " + (it.findtext("description") or "")[:600],
                              "score": 0})
        except Exception as e:
            print(f"news '{q}' failed: {e}")
    # Pre-rank: famous names + dramatic words + how many outlets cover the same person (= trending)
    names = [n.lower() for n in BIG_STREAMERS]
    buzz = {n: sum(n in i["title"].lower() for i in items) for n in names}
    for i in items:
        t = i["title"].lower()
        i["score"] = (sum(3 + buzz[n] for n in names if n in t)
                      + sum(2 for w in HOOK_WORDS if w in t))
    return items


def load_history():
    return json.loads(HISTORY.read_text()) if HISTORY.exists() else []


def api_views(video_ids):
    """{video_id: views} via the official YouTube Data API (1 quota unit per 50 videos).
    Uses YT_API_KEY if set, otherwise tries the bot's own upload login."""
    ids = [v for v in video_ids if v][:50]
    if not ids:
        return {}
    params = {"part": "statistics", "id": ",".join(ids)}
    key = os.environ.get("YT_API_KEY")
    if key:
        r = requests.get("https://www.googleapis.com/youtube/v3/videos", params={**params, "key": key}, timeout=30)
    else:
        r = requests.get("https://www.googleapis.com/youtube/v3/videos", params=params, timeout=30,
                         headers={"Authorization": f"Bearer {yt_access_token()}"})
    r.raise_for_status()
    return {i["id"]: int(i["statistics"].get("viewCount", 0)) for i in r.json().get("items", [])}


def yt_access_token():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"])
    creds.refresh(Request())
    return creds.token


def update_views(history, max_checks=15):
    """Refresh view counts of our Shorts from the last 2-21 days (public data, no extra keys)."""
    today = dt.date.today()
    todo = [h for h in history if h.get("video") and
            2 <= (today - dt.date.fromisoformat(h["date"])).days <= 21][-max_checks:]
    feed = {}
    try:
        feed = api_views([h["video"] for h in todo])
    except Exception as e:
        print("View check via YouTube API failed (learning only - posting is unaffected):", str(e)[:200])
    for h in todo:
        if h["video"] in feed:
            h["views"] = feed[h["video"]]
            continue
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--skip-download", "--print", "view_count",
                            f"https://www.youtube.com/shorts/{h['video']}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        v = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if v.isdigit():
            h["views"] = int(v)
    if todo:
        print("Views updated:", [(h.get("streamer"), h.get("views")) for h in todo])


def performance(history):
    """-> ({streamer: avg views}, text summary of what worked, for Gemini)."""
    rated = [h for h in history if h.get("views") is not None and h.get("streamer")]
    if len(rated) < 3:
        return {}, "(not enough data yet)"
    by = {}
    for h in rated:
        by.setdefault(h["streamer"], []).append(h["views"])
    avg = {s: sum(v) / len(v) for s, v in by.items()}
    best = sorted(rated, key=lambda h: h["views"], reverse=True)
    lines = [f"- {h['views']} views: {h['streamer']} - {h.get('topic')} (title: {h.get('yt_title')})"
             for h in best[:5]]
    if len(best) > 8:
        lines += ["WORST:"] + [f"- {h['views']} views: {h['streamer']} - {h.get('topic')}" for h in best[-3:]]
    return avg, "\n".join(lines)


# ---------- 2. Gemini ----------
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
_models = None


def gemini_models():
    """Ask Google which text models this key can use; newest 'flash' first."""
    global _models
    if _models is None:
        r = requests.get(f"{GEMINI_API}/models", headers={"x-goog-api-key": GEMINI_KEY},
                         params={"pageSize": 200}, timeout=30)
        r.raise_for_status()
        skip = ("image", "tts", "audio", "live", "embedding", "vision", "thinking", "learnlm", "gemma",
                "robotics", "transcribe", "computer-use", "customtools", "-pro", "pro-")
        names = [m["name"] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])
                 and "gemini" in m["name"] and not any(s in m["name"] for s in skip)]
        rank = lambda n: ("flash" in n, "lite" not in n, "preview" not in n and "exp" not in n, n)
        _models = sorted(names, key=rank, reverse=True)
        print("Gemini models available:", _models[:8])
    return _models


_last_ok = [None]       # model that answered last time
_out_of_quota = set()   # models that said 429/404 this run


class Blocked(Exception):
    """Gemini's safety filter refused this content - retrying won't help."""


# Streamer clips swear and joke crudely; use the loosest filter Google allows.
SAFETY = [{"category": c, "threshold": "BLOCK_NONE"} for c in (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]


def gemini(prompt, temperature=0.9, images=()):
    """images: jpeg file paths sent along with the prompt (Gemini can see them).
    Raises Blocked if the safety filter refuses the content on every model."""
    parts = [{"text": prompt}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(Path(p).read_bytes()).decode()}}
        for p in images]
    body = {"contents": [{"parts": parts}], "safetySettings": SAFETY,
            "generationConfig": {"responseMimeType": "application/json", "temperature": temperature}}
    # Try the model that last worked first; skip models that ran out of quota earlier this run.
    models = sorted(gemini_models(), key=lambda m: m != _last_ok[0])
    dead = set(_out_of_quota)
    blocked = 0
    # Google often returns 503 "high demand" for a few minutes; keep retrying for ~20 min.
    for attempt, wait in enumerate([0, 30, 60, 120, 180, 300, 300, 300]):
        if wait:
            print(f"All models busy - waiting {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
        for model in models:
            if model in dead:
                continue
            try:
                r = requests.post(f"{GEMINI_API}/{model}:generateContent",
                                  headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=180)
            except requests.RequestException as e:
                print(f"{model} error: {e}")
                continue
            if r.ok:
                data = r.json()
                try:
                    out = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
                    _last_ok[0] = model
                    return out
                except (KeyError, IndexError, ValueError):
                    reason = (data.get("promptFeedback", {}).get("blockReason")
                              or (data.get("candidates") or [{}])[0].get("finishReason") or "empty")
                    print(f"{model} refused: {reason}")
                    dead.add(model)
                    if reason not in ("empty", "MAX_TOKENS"):
                        blocked += 1
                        if blocked >= 2:  # two models refused the same content -> it's the content
                            out = groq(prompt, temperature, images)
                            if out is not None:
                                return out
                            raise Blocked(reason)
                    continue
            print(f"{model} failed: {r.status_code}")
            if r.status_code in (400, 403, 404, 429):
                dead.add(model)
                _out_of_quota.add(model)  # remembered for the rest of this run
        # Gemini didn't answer this round -> ask the backup (Groq) before waiting around
        out = groq(prompt, temperature, images)
        if out is not None:
            return out
        if len(dead) >= len(models):
            break
    if blocked:
        raise Blocked("refused by all models")
    sys.exit("Gemini AND Groq unavailable - it will try again at the next run.")


# ---------- Backup AI: Groq (free, used only when Gemini can't answer) ----------
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API = "https://api.groq.com/openai/v1"
_groq_models = None


def groq_models():
    """Chat models on this Groq key, best first. Vision-capable ones are marked."""
    global _groq_models
    if _groq_models is None:
        _groq_models = []
        try:
            r = requests.get(f"{GROQ_API}/models", headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=30)
            r.raise_for_status()
            skip = ("whisper", "tts", "guard", "playai", "distil", "embed", "orpheus", "prompt")
            ids = [m["id"] for m in r.json().get("data", []) if m.get("active", True)
                   and not any(s in m["id"].lower() for s in skip)]
            size = lambda i: max([int(x) for x in re.findall(r"(\d+)b", i.lower())] or [0])
            _groq_models = sorted(ids, key=lambda i: (size(i), "maverick" in i or "scout" in i), reverse=True)
            print("Groq backup models:", _groq_models[:5])
        except Exception as e:
            print("Groq model list failed:", e)
    return _groq_models


def groq(prompt, temperature=0.7, images=()):
    """Same job as gemini(), on Groq. Returns parsed JSON or None. Frames are sent only to
    models that can see images; others get the text (transcripts are the main evidence anyway)."""
    if not GROQ_KEY:
        return None
    for model in groq_models()[:4]:
        vision = any(v in model.lower() for v in ("vision", "scout", "maverick", "llama-4"))
        content = [{"type": "text", "text": prompt}]
        if vision and images:
            content += [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," +
                         base64.b64encode(Path(p).read_bytes()).decode()}} for p in list(images)[:5]]
        try:
            r = requests.post(f"{GROQ_API}/chat/completions", timeout=180,
                              headers={"Authorization": f"Bearer {GROQ_KEY}"},
                              json={"model": model, "temperature": temperature,
                                    "response_format": {"type": "json_object"},
                                    "messages": [{"role": "user", "content": content if vision and images else prompt}]})
            if r.ok:
                print(f"Using backup AI: Groq {model}" + ("" if vision or not images else " (text only)"))
                return json.loads(r.json()["choices"][0]["message"]["content"])
            print(f"groq {model} failed: {r.status_code} {r.text[:150]}")
        except Exception as e:
            print(f"groq {model} error: {e}")
    return None


# ---------- 3. Clips ----------
def list_twitch(user, ranges=("7d",), limit=6):
    clips = []
    for rng in ranges:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(limit), "--print",
                            "%(title)s\t%(duration)s\t%(view_count)s\t%(url)s",
                            f"https://www.twitch.tv/{user}/clips?filter=clips&range={rng}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 4 and parts[3].startswith("http"):
                clips.append({"title": parts[0], "duration": float(parts[1] or 0),
                              "views": int(parts[2] or 0) if parts[2].isdigit() else 0,
                              "url": parts[3], "platform": "twitch", "user": user, "range": rng})
        if len(clips) >= limit + 2:
            break
    return clips


def list_kick(user, ranges=("week",), limit=6):
    clips = []
    try:
        from curl_cffi import requests as cffi  # gets past Kick's Cloudflare
        for rng in ranges:
            r = cffi.get(f"https://kick.com/api/v2/channels/{user}/clips", impersonate="chrome",
                         params={"cursor": 0, "sort": "view", "time": rng}, timeout=30)
            for c in r.json().get("clips", [])[:limit]:
                url = c.get("video_url") or c.get("clip_url")
                if url:
                    clips.append({"title": c.get("title") or "", "duration": float(c.get("duration") or 0),
                                  "views": int(c.get("view_count") or 0), "url": url,
                                  "platform": "kick", "user": user, "range": rng})
            if len(clips) >= limit + 2:
                break
    except Exception as e:
        print(f"kick {user} failed: {e}")
    return clips


# name -> (twitch username, kick username). Wrong/missing usernames are just skipped.
STREAMER_ACCOUNTS = {
    "Kai Cenat": ("kaicenat", None), "xQc": ("xqc", "xqc"), "Adin Ross": (None, "adinross"),
    "N3on": (None, "n3on"), "Pokimane": ("pokimane", None), "Hasan Piker": ("hasanabi", None),
    "Asmongold": ("zackrawrr", None), "Jynxzi": ("jynxzi", None), "Duke Dennis": ("dukedennis", None),
    "Fanum": ("fanum", None), "Agent 00": ("agent00", None), "Caseoh": ("caseoh_", None),
    "Shroud": ("shroud", None), "Summit1g": ("summit1g", None), "Amouranth": ("amouranth", "amouranth"),
    "Sneako": (None, "sneako"), "Clix": ("clix", None), "Plaqueboymax": ("plaqueboymax", None),
    "Stable Ronaldo": ("stableronaldo", None), "Lacy": ("lacy", None), "Emiru": ("emiru", None),
    "Mizkif": ("mizkif", None), "Moistcr1tikal": ("moistcr1tikal", None), "Tyler1": ("loltyler1", None),
    "Silky": ("silky", None), "Jason The Ween": ("jasontheween", None), "ExtraEmily": ("extraemily", None),
    "Marlon": ("marlon", None), "Sketch": ("sketch", None), "Ninja": ("ninja", None),
    "Ibai": ("ibai", None), "Tfue": ("tfue", "tfue"), "Zherka": (None, "zherka"),
    "Cuffem": (None, "cuffem"), "Kishka": (None, "kishka"), "iShowSpeed": (None, "ishowspeed"),
}


def find_trending_clips(history, avg_views=None, n_streamers=18, keep=8):
    """This week's most-viewed clips from a random set of big streamers -> best `keep` candidates
    (max 2 per streamer, never a clip we already used). Streamers whose Shorts did well on our
    channel get picked more often; flops less often (but never zero - tastes change)."""
    used = {h.get("clip") for h in history} | {h.get("url") for h in history}
    avg_views = avg_views or {}
    overall = sum(avg_views.values()) / len(avg_views) if avg_views else 1
    weight = {n: min(4.0, max(0.3, avg_views[n] / overall)) if n in avg_views and overall else 1.0
              for n in STREAMER_ACCOUNTS}
    # weighted sampling without replacement
    names = sorted(STREAMER_ACCOUNTS, key=lambda n: random.random() ** (1 / weight[n]), reverse=True)
    names = names[:n_streamers]
    pool = []
    for name in names:
        tw, kk = STREAMER_ACCOUNTS[name]
        got = (list_twitch(tw) if tw else []) + (list_kick(kk) if kk else [])
        got = [c for c in got if c["url"] not in used and 8 <= c["duration"] <= 90]
        for c in got:
            c["name"] = name
        print(f"{name}: {len(got)} clips, top views {max([c['views'] for c in got], default=0)}")
        pool += got
    pool.sort(key=lambda c: c["views"], reverse=True)
    picked, per = [], {}
    for c in pool:
        if per.get(c["name"], 0) < 2:
            picked.append(c)
            per[c["name"]] = per.get(c["name"], 0) + 1
        if len(picked) >= keep:
            break
    return picked


def download_clip(c, path):
    if c["platform"] == "twitch":
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-part", "-f", "b", "-o", str(path), c["url"]], timeout=300)
    else:  # kick: HLS playlist
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-user_agent", "Mozilla/5.0", "-i", c["url"],
                            "-c", "copy", str(path)], timeout=300)
    ok = r.returncode == 0 and path.exists() and path.stat().st_size > 50_000
    print(("downloaded " if ok else "download failed ") + c["url"])
    return ok


def contact_sheet(c, k, times=None):
    """One jpeg with 3 frames side by side (default 20%, 50%, 80% through the clip), so Gemini can
    see what the clip actually shows (face cam / IRL vs. gameplay)."""
    dst = WORK / f"sheet{k}.jpg"
    d = max(c["duration"], 3)
    inputs = []
    for t in times or (d * 0.2, d * 0.5, d * 0.8):
        inputs += ["-ss", f"{max(0, t):.1f}", "-i", str(c["path"])]
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex",
                        "[0:v]scale=480:-2,trim=end_frame=1[a];[1:v]scale=480:-2,trim=end_frame=1[b];"
                        "[2:v]scale=480:-2,trim=end_frame=1[c];[a][b][c]hstack=3[v]",
                        "-map", "[v]", "-frames:v", "1", "-q:v", "4", str(dst)], check=True, timeout=60)
        return dst
    except Exception as e:
        print("contact sheet failed:", e)
        return None


def credit_for(c):
    return f"Clip: {c['platform']}.{'tv' if c['platform'] == 'twitch' else 'com'}/{c['user']}"


_whisper = None


def load_audio_16k(path):
    """Decode any clip to the raw 16 kHz mono audio Whisper wants, using FFmpeg directly.
    (faster-whisper's own decoder depends on the PyAV library, whose updates broke it once -
    FFmpeg is always there and stable.)"""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True, timeout=300).stdout
    return np.frombuffer(raw, np.float32).copy()


def transcribe(path):
    """-> list of (start, end, word) using faster-whisper."""
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    audio = load_audio_16k(path)
    if audio.size < 8000:  # under half a second of audio
        return []
    segs, _ = _whisper.transcribe(audio, word_timestamps=True, vad_filter=True)
    return [(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or []) if w.word.strip()]


def has_speech(clip, meta, min_words=6):
    """The shown part must actually contain talking - otherwise it's a silent/boring clip."""
    n = len(words_in(clip["words"], float(meta["clip_start"]), float(meta["clip_end"])))
    if n < min_words:
        print(f"  Rejected: only {n} spoken words in the chosen part - need {min_words}+")
        return False
    return True


def check_transcripts(clips):
    """If NOT ONE clip has any words, transcription itself is broken - stop loudly instead of
    making videos 'blind' (that's what caused silent clips to get picked before)."""
    if clips and not any(c.get("words") for c in clips):
        sys.exit("TRANSCRIPTION BROKEN: 0 words in every clip - not posting blind. See errors above.")


def snap_window(words, cs, ce, dur, lo=8.0, hi=25.0):
    """Move the cut points to real sentence boundaries so the clip never starts or ends
    mid-sentence. A boundary = sentence punctuation or a pause > 0.7s."""
    if not words:
        return cs, ce
    n = len(words)
    starts = [0] + [i for i in range(1, n)
                    if words[i - 1][2][-1:] in ".?!" or words[i][0] - words[i - 1][1] > 0.7]
    ends = [i for i in range(n)
            if words[i][2][-1:] in ".?!" or i == n - 1 or words[i + 1][0] - words[i][1] > 0.7]
    def start_at(i):  # a little lead-in, but never into the previous word
        prev_end = words[i - 1][1] if i > 0 else 0.0
        return max(0.0, prev_end + 0.02, words[i][0] - 0.15)

    def end_at(i):  # a little tail, but never into the next word
        nxt = words[i + 1][0] if i + 1 < n else dur
        return min(dur, words[i][1] + 0.35, nxt - 0.03)

    si = max([i for i in starts if words[i][0] <= cs + 0.4], default=0)
    ei = min([i for i in ends if i >= si and words[i][1] >= ce - 0.4], default=n - 1)
    new_cs = start_at(si)
    if end_at(ei) - new_cs > hi:  # too long: end at the latest sentence end that still fits
        fit = [i for i in ends if i >= si and lo <= end_at(i) - new_cs <= hi]
        ei = max(fit) if fit else ei
    return round(new_cs, 2), round(end_at(ei), 2)


def words_in(words, cs, ce):
    """Exactly the words inside the cut - the same ones the viewer hears and the captions show."""
    return [(s, e, w) for s, e, w in words if s >= cs - 0.05 and e <= ce + 0.05]


def as_timestamped_text(words):
    """Compact transcript: a timestamp every ~sentence so Gemini can pick exact cut points."""
    out, line = [], []
    for s, e, w in words:
        if not line:
            line.append(f"({s:.1f})")
        line.append(w)
        if w[-1:] in ".?!" or len(line) > 14:
            out.append(" ".join(line) + f" ({e:.1f})")
            line = []
    if line:
        out.append(" ".join(line) + f" ({words[-1][1]:.1f})")
    return "\n".join(out)


# ---------- 4. Script ----------
def write_script(story, clips, feedback=None, rejected=None, use_images=True):
    blocks, images = [], []
    for i, c in enumerate(clips):
        seen = "no image"
        if c.get("sheet"):
            images.append(c["sheet"])
            seen = f"IMAGE #{len(images)} shows 3 frames from it"
        blocks.append(f"CLIP [{i}] {c['platform']}/{c['user']} \"{c['title']}\" ({c['duration']:.0f}s) - {seen}\n"
                      + (as_timestamped_text(c["words"]) if c["words"] else "(no speech)"))
    formats = "\n".join(f"  - {f}" for f in FORMATS)
    prompt = f"""You run a streamer clip channel (like the big LivestreamFail-style Shorts channels) whose
videos get millions of views. The Short = [narrated intro] + [the REAL clip playing with its
original audio] + [narrated outro].

Below are this week's MOST-VIEWED clips from famous streamers (already going viral), transcribed,
with frames attached (IMAGE #1, #2, ... in the order listed).

RECENT NEWS about these streamers (context only - use it if it explains a clip):
{story['news'] or '(none)'}

Topics we already covered recently (don't repeat): {story['recent']}

HOW OUR PAST SHORTS PERFORMED (learn from it - favor the kinds of streamers/moments/titles that
got views, avoid what flopped):
{story.get('perf') or '(not enough data yet)'}

CLIPS (timestamps in seconds):
{chr(10).join(blocks) or '(none)'}

STEP 1 - pick the ONE clip most likely to go viral as a Short:
- A real MOMENT: confrontation, heated argument, shocking/funny reaction, crazy IRL event, big
  announcement, emotional moment, a famous guest. Face cam / IRL where you can SEE it happen.
- The speech must be understandable and make sense with a short intro. It must be clear WHO is
  talking. Look at the frames AND read the transcript to understand what really happens.
- REJECT: plain gameplay, chatting about nothing, music/dancing without a story, clips where the
  transcript is gibberish, anything you can't clearly explain, and repeats of covered topics.
- clip_start/clip_end: 8-18 seconds containing the best part, cut at sentence boundaries using the
  timestamps. Don't start mid-sentence; don't cut off the punchline.
- If the news explains the clip, set clip_relates true and use it. If there's no news, describe
  ONLY what the clip shows - never guess the backstory.

STEP 2 - write:
- intro: 20-35 words (~9s). SENTENCE 1: streamer name + the most shocking thing, under 12 words.
  Then only the context needed to understand the clip, then a setup like "Watch what happened."
  Every claim must be visible/audible in the clip or stated in the news. Don't repeat the clip's lines.
- outro: 8-15 words, COMPLETE sentences: one punchy line + a question that makes people comment
  (e.g. "Kai did not hold back. Was he right, or did he go too far?").
- Describe what happens accurately: no exaggeration ("last second", "massive", "insane") unless the
  clip clearly shows it.
- Pick the format that fits: {formats}

RULES: Never invent quotes/numbers/events/backstory. Rumors = "reportedly". No insults or
unproven accusations. No emojis/hashtags/stage directions in speech. Only if EVERY clip is
unusable: clip_index = -1 and intro = a 60-80 word script about the best news story instead.

- broll: indices of OTHER clips of the SAME moment/event (e.g. another angle of the same fight)
  to show silently behind the narration. Almost always []: we reuse other seconds of the main clip.

Return JSON: {{"clip_index": int, "clip_relates": bool, "clip_start": float, "clip_end": float,
"broll": [int],
"intro": str, "outro": str,
"title": "under 60 chars, names the streamer, curiosity, not a lie, ends with ' #shorts'",
"description": "2 sentences, no links", "hook_text": "max 5 words shown big at the start",
"clip_label": "max 4 words shown on top while the clip plays, e.g. 'N3ON vs STRICKLAND'",
"hashtags": [4 topic hashtags], "tags": [10 strings],
"search_terms": [3 stock-footage queries, only used if we have no clips]}}"""
    if feedback:
        prompt += ("\n\nA FACT-CHECKER REJECTED YOUR LAST ATTEMPT. Fix every problem (pick a different "
                   "clip or seconds, or reword the intro so it only promises what the clip really shows):\n"
                   + "\n".join(f"- {p}" for p in feedback))
    if rejected:
        prompt += (f"\n\nCLIPS ALREADY REJECTED BY THE FACT-CHECKER: {rejected}. Pick a DIFFERENT clip.")
    return gemini(prompt, temperature=0.7, images=images if use_images else ())


def repair_script(story, meta, clip, problems):
    """Cheap fix: keep the same clip + cut, only rewrite the text the fact-checker complained about."""
    cs, ce = float(meta["clip_start"]), float(meta["clip_end"])
    said = " ".join(w for _, _, w in words_in(clip["words"], cs, ce)) or "(no speech)"
    fixed = gemini(f"""Fix this YouTube Short's text. A fact-checker found problems. Keep the same clip.

CLIP ({clip['name']}'s channel) - exact words the viewer hears: "{said}"
RECENT NEWS (only allowed source besides the clip): {story['news'] or '(none)'}

CURRENT TEXT:
intro: {meta.get('intro')}
outro: {meta.get('outro')}
title: {meta.get('title')}
hook_text: {meta.get('hook_text')}
clip_label: {meta.get('clip_label')}
description: {meta.get('description')}

PROBLEMS TO FIX:
{chr(10).join('- ' + p for p in problems)}

Rules: only claim what the clip's words show or the news states. Get WHO said/did what exactly
right. No exaggeration. Intro 20-35 words starting with the streamer's name + the key moment, ending
with a setup like "Watch what happened." Outro 8-15 words, complete sentences, ending with a
question. Title under 60 chars ending with " #shorts".
Return JSON with the same keys: {{"intro", "outro", "title", "hook_text", "clip_label", "description"}}""",
                   temperature=0.3)
    for k in ("intro", "outro", "title", "hook_text", "clip_label", "description"):
        if isinstance(fixed.get(k), str) and fixed[k].strip():
            meta[k] = fixed[k].strip()
    return meta


def verify(story, meta, clip, attempt):
    """Fact-check: does the clip segment really deliver what the narration promises?
    -> (passed, [problems])"""
    cs, ce = float(meta.get("clip_start") or 0), float(meta.get("clip_end") or 0)
    inside = words_in(clip["words"], cs, ce)
    said = " ".join(w for _, _, w in inside) or "(no speech)"
    before = " ".join(w for s, e, w in clip["words"] if cs - 8 <= s and e < cs - 0.05)
    after = " ".join(w for s, e, w in clip["words"] if s > ce + 0.05 and s <= ce + 6)
    sheet = contact_sheet(clip, f"verify{attempt}", times=(cs + 0.5, (cs + ce) / 2, ce - 0.5))
    data = gemini(f"""You are a strict fact-checker for a streamer-news YouTube Short. Be skeptical.

RECENT NEWS (the only allowed source besides the clip itself): {story['news'] or '(none)'}
THE CLIP IS FROM: {clip['name']}'s channel. Full transcript of the clip:
{as_timestamped_text(clip['words']) if clip['words'] else '(no speech)'}

THE VIDEO:
1. Narrator intro: "{meta.get('intro')}"
2. Then a clip from {clip['platform']}.{'tv' if clip['platform'] == 'twitch' else 'com'}/{clip['user']} plays,
   with the on-screen label "{meta.get('clip_label')}".
   EXACT words spoken in the part we show ({cs:.1f}s-{ce:.1f}s): "{said}"
   (just before it: "...{before}")  (just after it: "{after}...")
   The attached image = 3 frames from the start, middle and end of that part, taken from the RAW
   stream. Our captions are NOT in these frames - any text you see is the streamer's own overlay,
   chat, or game UI, so don't judge caption accuracy from it.
   The start/end were already snapped to sentence boundaries by software.
3. Narrator outro: "{meta.get('outro')}"
4. Title: "{meta.get('title')}"

CHECK EVERY ONE:
a) Does the clip part actually deliver what the intro promises? If the intro says "here's what X
   said about Y" / "listen to his response" / "watch what happened", the words and frames must
   really be X's response about Y / that event - not a random moment, not a different topic.
b) Is the person speaking/shown plausibly the streamer the video claims? (channel, frames, words)
c) Is the MEANING complete - does the viewer get the key line/punchline? Our software already
   cuts exactly at word boundaries, so do NOT fail for timestamp nitpicks or tiny trims; only fail
   if the important part of the moment is missing.
d) Are all facts in intro/outro/title/label supported by the news or the clip (transcript+frames)?
   Any invented quote, number, event or backstory = fail.
f) Is this actually worth watching - a clear, interesting moment a viewer understands in 3 seconds?
   Boring/confusing/gameplay-only = fail.
e) Nothing misleading: the label and title must not claim more than the clip shows.

Return JSON: {{"pass": bool, "problems": ["specific problem + how to fix it"]}}""",
                  temperature=0.1, images=[sheet] if sheet else [])
    return bool(data.get("pass")), [str(p) for p in data.get("problems", [])]


# ---------- 5. Voice + captions ----------
async def _tts(text, mp3, voice=VOICE):
    words = []
    comm = edge_tts.Communicate(text, voice, rate="+8%", boundary="WordBoundary")
    with open(mp3, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append((chunk["offset"] / 1e7, (chunk["offset"] + chunk["duration"]) / 1e7,
                              chunk["text"]))
    return words


def tts(text, mp3):
    """Narrator voice with 3 layers so a voice outage can't cost a post:
    1) Microsoft Edge voices (best) - 4 tries, 2 voices   2) Google TTS   3) espeak-ng (offline)."""
    mp3 = Path(mp3)
    words = None
    for n, voice in enumerate([VOICE, VOICE, "en-US-GuyNeural", "en-US-ChristopherNeural"]):
        try:
            words = asyncio.run(_tts(text, mp3, voice))
            if mp3.exists() and mp3.stat().st_size > 2000:
                break
        except Exception as e:
            print(f"Edge voice {voice} failed ({type(e).__name__}) - retrying")
        words = None
        time.sleep(4 * (n + 1))
    if words is None:
        words = []
        try:
            from gtts import gTTS
            tmp = mp3.with_suffix(".g.mp3")
            gTTS(text, lang="en", tld="com").save(str(tmp))
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp), "-af", "atempo=1.15", str(mp3)],
                           check=True)
            print("Using backup voice: Google TTS")
        except Exception as e:
            print(f"Google TTS failed ({e}) - using offline espeak-ng")
            wav = mp3.with_suffix(".wav")
            subprocess.run(["espeak-ng", "-v", "en-us", "-s", "165", "-w", str(wav), text], check=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), str(mp3)], check=True)
    if not words:  # fallback: spread words evenly
        toks, d = text.split(), duration(mp3)
        words = [(i * d / len(toks), (i + 1) * d / len(toks), w) for i, w in enumerate(toks)]
    return words


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)])
    return float(out)


def is_wide(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height", "-of", "csv=p=0", str(path)]).decode()
    w, h = [int(x) for x in out.strip().split(",")[:2]]
    return w > h


def ts(s):
    s = max(0, s)
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02}:{s % 60:05.2f}"


def ass_escape(s):
    return s.replace("{", "(").replace("}", ")").replace("\\", "/")


# Slurs get bleeped in the AUDIO (regular swearing stays - it's normal for streamer clips).
SLURS = re.compile(r"^(n[i1]gg\w*|nigg\w*|fag\w*|f[a4]gg\w*|retard\w*|tr[a4]nn\w*|chinks?|spics?|k[i1]kes?)$", re.I)
SWEARS = re.compile(r"\b(f+u+c+k\w*|shit\w*|bitch\w*|n[i1]gg\w*|cunt\w*|dick\w*|puss\w*|fag\w*)\b", re.I)


def clean(word):
    """Soften swears in captions (audio stays): 'fuck' -> 'f**k'."""
    return SWEARS.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 2) + m.group(0)[-1], word)


def write_ass(words, total, path, hook_text, overlays):
    """words: absolute (start, end, text). overlays: [(start, end, style, text)]."""
    head = """[Script Info]
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Main,DejaVu Sans,92,&H00FFFFFF,&H00000000,&H80000000,1,1,7,3,5,0
Style: Hook,DejaVu Sans,84,&H00FFFFFF,&H000000FF,&H00000000,1,3,6,0,8,260
Style: Label,DejaVu Sans,64,&H00FFFFFF,&H000000FF,&H00000000,1,3,5,0,8,300
Style: Credit,DejaVu Sans,40,&H00FFFFFF,&H00000000,&H90000000,1,3,4,0,2,230

[Events]
Format: Layer, Start, End, Style, Text
"""
    lines = []
    if hook_text:  # big red-boxed hook for the first 2.5s
        lines.append(f"Dialogue: 1,{ts(0)},{ts(min(2.5, total))},Hook,"
                     f"{{\\fad(0,200)\\fscx60\\fscy60\\t(0,150,\\fscx100\\fscy100)}}{ass_escape(hook_text.upper())}")
    for s, e, style, text in overlays:
        lines.append(f"Dialogue: 1,{ts(s)},{ts(e)},{style},{ass_escape(text)}")
    for i in range(0, len(words), 2):  # 2 words per caption
        grp = words[i:i + 2]
        nxt = words[i + 2][0] if i + 2 < len(words) else total
        end = min(nxt, grp[-1][1] + 0.5)
        text = ass_escape(clean(" ".join(w[2] for w in grp)).upper())
        color = "{\\c&H00F0FF&}" if i % 4 == 0 else ""  # alternate yellow/white
        lines.append(f"Dialogue: 0,{ts(grp[0][0])},{ts(end)},Main,{color}{{\\fscx110\\fscy110\\t(0,80,\\fscx100\\fscy100)}}{text}")
    path.write_text(head + "\n".join(lines), encoding="utf-8")


# ---------- 6. Footage + render ----------
def pexels_clips(terms, n=3):
    if not PEXELS_KEY:
        return []
    urls = []
    for q in terms + ["gaming setup", "streamer setup"]:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": PEXELS_KEY},
                         params={"query": q, "orientation": "portrait", "per_page": 6}, timeout=20)
        for v in r.json().get("videos", []):
            files = [f for f in v["video_files"] if (f.get("height") or 0) >= 1280]
            if files:
                urls.append(min(files, key=lambda f: f["height"])["link"])
                break
        if len(urls) >= n:
            break
    paths = []
    for k, u in enumerate(urls):
        p = WORK / f"stock{k}.mp4"
        p.write_bytes(requests.get(u, timeout=120).content)
        paths.append(p)
    return paths


ZOOM = "zoompan=z='min(zoom+0.0012,1.12)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30"
FLASH = "fade=t=in:st=0:d=0.15:color=white"


def render_part(src, start, length, dst, zoom=True):
    """One video-only segment. Wide (16:9) clips sit big in the middle over a blurred copy of
    themselves. B-roll gets a slow push-in; every cut gets a white flash."""
    fx = f"{ZOOM},{FLASH}" if zoom else f"{FLASH}"
    if is_wide(src):
        fc = ("[0:v]fps=30,split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
              "crop=1080:1920,boxblur=20:2,eq=brightness=-0.2[bg];"
              "[b]scale=1440:-2,crop=1080:ih[fg];"  # zoom in a bit so the streamer fills more of the screen
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{fx},setsar=1[v]")
    else:
        fc = (f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,"
              f"eq=brightness=-0.08,{fx},setsar=1[v]")
    loop = ["-stream_loop", "-1"] if zoom else []  # b-roll may loop; the main clip never does
    subprocess.run(["ffmpeg", "-y", "-v", "error", *loop, "-ss", f"{start:.2f}", "-i", str(src),
                    "-t", f"{length:.2f}", "-filter_complex", fc, "-map", "[v]", "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-r", "30", str(dst)], check=True)


def make_voice_track(pieces, out):
    """pieces: [(path, start, length)] -> one normalized wav, played back to back."""
    cmd, fc = ["ffmpeg", "-y", "-v", "error"], []
    for k, (p, s, l) in enumerate(pieces):
        cmd += ["-ss", f"{s:.2f}", "-t", f"{l:.2f}", "-i", str(p)]
        fc.append(f"[{k}:a]aresample=44100,aformat=channel_layouts=stereo,"
                  f"loudnorm=I=-15:TP=-1.5:LRA=11,apad=whole_dur={l:.2f}[a{k}]")
    fc.append("".join(f"[a{k}]" for k in range(len(pieces))) + f"concat=n={len(pieces)}:v=0:a=1[out]")
    subprocess.run(cmd + ["-filter_complex", ";".join(fc), "-map", "[out]", "-ar", "44100", str(out)],
                   check=True)


def make_whoosh(path):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=0.45:c=pink:a=0.5",
                    "-af", "highpass=f=500,lowpass=f=5000,afade=t=in:d=0.2,afade=t=out:st=0.2:d=0.25",
                    str(path)], check=True)


MUSIC_MOODS = ["upbeat", "energetic", "electronic", "hip hop", "action", "epic",
               "dramatic", "trailer", "funky", "edm"]


def pick_music():
    """Find a fresh free track online (Openverse: CC0 / CC-BY only, safe for YouTube with
    credit). Falls back to any mp3s in a local music/ folder. -> (path, credit) or (None, None)"""
    mood = random.choice(MUSIC_MOODS)
    try:
        r = requests.get("https://api.openverse.org/v1/audio/", headers=UA, timeout=30, params={
            "q": mood, "license": "cc0,by", "category": "music", "page_size": 20})
        r.raise_for_status()
        tracks = [t for t in r.json().get("results", [])
                  if (t.get("duration") or 0) >= 40000 and t.get("url")]
        random.shuffle(tracks)
        for t in tracks[:5]:
            try:
                data = requests.get(t["url"], headers=UA, timeout=60).content
                if len(data) < 100_000:
                    continue
                p = WORK / "music_dl"
                p.write_bytes(data)
                duration(p)  # make sure ffmpeg can read it
                lic = f"CC {t['license'].upper()} {t.get('license_version') or ''}".strip()
                print(f"Music ({mood}): {t['title']} by {t['creator']}")
                return p, f"Music: \"{t['title']}\" by {t['creator']} ({lic}) {t.get('foreign_landing_url', '')}"
            except Exception as e:
                print("track failed:", e)
    except Exception as e:
        print("music search failed:", e)
    local = sorted((ROOT / "music").glob("*.mp3"))
    return (random.choice(local), None) if local else (None, None)


def make_boom(path):
    """The vine boom. Uses boom.mp3 / boom.wav from the repo if it's there (the real sound);
    otherwise generates a similar deep hit: a sine dropping ~190 Hz -> ~45 Hz with a short echo."""
    for name in ("boom.mp3", "boom.wav", "vine-boom.mp3", "vine_boom.mp3"):
        src = ROOT / name
        if src.exists():
            # trim silence before the hit (so it lands exactly on the beat), cap the length,
            # and normalize loudness (downloaded files vary a lot)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-af",
                            "silenceremove=start_periods=1:start_threshold=-45dB,atrim=end=2.5,"
                            "loudnorm=I=-14:TP=-1.5:LRA=7,aformat=channel_layouts=stereo,aresample=44100",
                            str(path)], check=True)
            if duration(path) < 0.15:
                raise RuntimeError(f"{name} is empty or silent")
            print(f"Using the real vine boom ({name}, {duration(path):.2f}s after trimming)")
            return
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "aevalsrc='0.9*sin(2*PI*(45+145*exp(-14*t))*t)*exp(-3.2*t)':s=44100:d=1.3",
                    "-af", "volume=2.2,asoftclip=type=tanh,lowpass=f=900,aecho=0.8:0.55:55:0.35,"
                    "afade=t=out:st=0.7:d=0.6,aformat=channel_layouts=stereo",
                    str(path)], check=True)


def build_video(sections, voice, total, ass, out, music=None, duck=None, bleeps=(), booms=()):
    """sections: [(src, start, length, zoom)] played back to back. duck: (start, end) where the
    real clip plays - music drops so you can hear it. bleeps: [(start, end)] to mute + beep.
    booms: [seconds] where a vine-boom hit plays (the funniest beats)."""
    parts = []
    for k, (src, start, length, zoom) in enumerate(sections):
        p = WORK / f"part{k}.mp4"
        render_part(src, start, length, p, zoom)
        parts.append(p)
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    bg = WORK / "bg.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(bg)], check=True)

    whoosh = WORK / "whoosh.wav"
    make_whoosh(whoosh)
    cuts, t = [], 0
    for _, _, length, _ in sections[:-1]:
        t += length
        cuts.append(t)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(bg), "-i", str(voice)]
    for _ in cuts:
        cmd += ["-i", str(whoosh)]
    fc, mix = [], ["[1:a]"]
    if bleeps:
        when = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in bleeps)
        fc.append(f"[1:a]volume=0:enable='{when}'[vo]")
        mix = ["[vo]"]
    for n, c in enumerate(cuts):
        ms = max(0, int((c - 0.2) * 1000))  # whoosh leads into the cut
        fc.append(f"[{n + 2}:a]adelay={ms}|{ms},volume=0.5[w{n}]")
        mix.append(f"[w{n}]")
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        duck_f = f",volume=0.25:enable='between(t,{duck[0]:.2f},{duck[1]:.2f})'" if duck else ""
        fc.append(f"[{len(cuts) + 2}:a]volume=0.13{duck_f},afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[m]")
        mix.append("[m]")
    booms = [b for b in booms if 0 <= b < total - 0.2]
    if booms:
        boom = WORK / "boom.wav"
        try:
            make_boom(boom)
        except Exception as e:  # never lose a post over a sound effect
            print("boom sound failed, posting without it:", e)
            booms = []
    if booms:
        first = len(cuts) + 2 + (1 if music else 0)  # inputs: bg, voice, whooshes, music, booms, beep
        for n, b in enumerate(booms):
            cmd += ["-i", str(boom)]
            ms = int(b * 1000)
            fc.append(f"[{first + n}:a]adelay={ms}|{ms},volume=0.9[bm{n}]")
            mix.append(f"[bm{n}]")
    if bleeps:  # classic 1 kHz censor beep exactly over the muted words
        k = len(cuts) + 2 + (1 if music else 0) + len(booms)
        cmd += ["-f", "lavfi", "-t", f"{total:.2f}", "-i", "sine=frequency=1000:sample_rate=44100"]
        fc.append(f"[{k}:a]volume='0.3*({when})':eval=frame,aformat=channel_layouts=stereo[bp]")
        mix.append("[bp]")
    fc.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[a]")
    fc.append(f"[0:v]ass={ass.name}[v]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
            "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True, cwd=WORK)


def split_broll(broll, length, max_part=4.5):
    """Cover `length` seconds with b-roll cuts of <= max_part seconds, rotating sources."""
    n = max(1, round(length / max_part + 0.49))
    part = length / n
    return [(broll[k % len(broll)][0], broll[k % len(broll)][1], part, True) for k in range(n)]


# ---------- 7. Upload ----------
BASE_HASHTAGS = ["#shorts", "#viral", "#streamer", "#twitch", "#fyp"]  # other bots can override
def upload(path, meta):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"])
    yt = build("youtube", "v3", credentials=creds)
    tags = [t if t.startswith("#") else "#" + t for t in meta.get("hashtags", [])]
    hashtags = " ".join(dict.fromkeys(BASE_HASHTAGS +
                                      [t.replace(" ", "") for t in tags]))
    credits = "\n".join(dict.fromkeys(meta.get("credits", []))) or "Footage: Pexels"
    desc = (f"{meta['description']}\n\nCredits:\n{credits}\nOriginal clip: {meta['source']['url']}\n\n"
            f"All clips belong to their respective creators.\n\n{hashtags}")
    body = {"snippet": {"title": meta["title"][:100],
                        "description": desc[:4900],
                        "tags": meta.get("tags", []), "categoryId": "20"},
            "status": {"privacyStatus": os.environ.get("YT_PRIVACY", "public"),
                       "selfDeclaredMadeForKids": False}}
    for attempt in range(4):  # network hiccups / YouTube 5xx: wait and try again
        try:
            res = yt.videos().insert(part="snippet,status", body=body,
                                     media_body=MediaFileUpload(str(path), resumable=True)).execute()
            print("Uploaded: https://youtube.com/shorts/" + res["id"])
            meta["channel_id"] = (res.get("snippet") or {}).get("channelId")  # for view tracking
            return res["id"]
        except Exception as e:
            msg = str(e)
            if "quotaExceeded" in msg or "uploadLimitExceeded" in msg:
                sys.exit("YouTube daily upload limit reached - the video will be made again next run.")
            if attempt == 3:
                raise
            print(f"Upload failed ({msg[:200]}) - retrying in {60 * (attempt + 1)}s")
            time.sleep(60 * (attempt + 1))


# ---------- main ----------
def main():
    WORK.mkdir(exist_ok=True)
    history = load_history()
    # 0. Learn from our own results: refresh view counts of recent Shorts
    try:
        update_views(history)
    except Exception as e:
        print("view update failed:", e)
    HISTORY.write_text(json.dumps(history, indent=1))  # keep view counts even if we skip today
    avg_views, perf = performance(history)
    print("Performance so far:\n" + perf)

    # 1. This week's most-viewed clips from big streamers = moments already going viral
    candidates = find_trending_clips(history, avg_views)
    if not candidates:
        sys.exit("No clips found.")

    # 2. News about those streamers, only as context for what's happening in the clips
    names = list(dict.fromkeys(c["name"] for c in candidates))
    items = sorted(fetch_google_news(names), key=lambda i: i["score"], reverse=True)[:25]
    story = {"news": "\n".join(f"- {i['title']} ({i['url']})" for i in items),
             "recent": [h.get("topic") for h in history[-30:]], "perf": perf}
    print(f"{len(items)} news items for {names}")

    # 3. Download, transcribe and grab frames from each candidate
    clips = []
    for c in candidates:
        p = WORK / f"clip{len(clips)}.mp4"
        if download_clip(c, p):
            c["path"] = p
            try:
                c["words"] = transcribe(p)
            except Exception as e:
                print("transcribe failed:", e)
                c["words"] = []
            c["sheet"] = contact_sheet(c, len(clips))
            clips.append(c)
    print(f"{len(clips)} clips ready ({sum(bool(c['words']) for c in clips)} with speech)")
    check_transcripts(clips)

    # 4. Pick + write -> fact-check -> fix (up to 4 tries). Nothing passes = no video today:
    #    better to skip a day than post something bad.
    #    Per clip: write -> check -> (repair the text -> check again) -> else next clip. Max 3 clips.
    rejected, passed, checks = [], False, 0
    for clip_try in range(3):
        try:
            meta = write_script(story, clips, None, rejected)
        except Blocked:
            print("Script request blocked by Gemini's safety filter - retrying without frames")
            try:
                meta = write_script(story, clips, None, rejected, use_images=False)
            except Blocked:
                sys.exit("Gemini refused these clips (safety filter) - trying again next run.")
        idx = meta.get("clip_index", -1)
        main_clip = clips[idx] if isinstance(idx, int) and 0 <= idx < len(clips) else None
        if not main_clip or idx in rejected:
            print("Gemini found no more usable clips.")
            break
        meta["clip_start"], meta["clip_end"] = snap_window(
            main_clip["words"], float(meta.get("clip_start") or 0), float(meta.get("clip_end") or 15),
            main_clip["duration"])
        print(f"Clip #{clip_try + 1}: {main_clip['name']} - {main_clip['url']} "
              f"{meta['clip_start']}-{meta['clip_end']}s\n  Intro: {meta.get('intro')}")
        if not has_speech(main_clip, meta):  # don't waste fact-checks on silent clips
            rejected.append(idx)
            continue
        for fix in range(2):  # first check, then one repair + re-check
            checks += 1
            try:
                passed, problems = verify(story, meta, main_clip, checks)
            except Blocked as e:  # the fact-checker can't even look at it -> never post it
                print(f"Fact-check blocked ({e}) - skipping this clip")
                passed, problems = False, []
                break
            print(f"Fact-check #{checks}: {'PASSED' if passed else 'FAILED'}", *problems, sep="\n  ")
            if passed or fix == 1 or not problems:
                break
            try:
                meta = repair_script(story, meta, main_clip, problems)
                print(f"  Repaired intro: {meta.get('intro')}\n  Repaired outro: {meta.get('outro')}")
            except Blocked:
                break
        if passed:
            break
        rejected.append(idx)
    if not passed:
        sys.exit("No clip passed the fact-check today - skipping instead of posting a bad video.")
    meta.update(source={"title": main_clip["title"], "url": main_clip["url"]},
                topic=f"{main_clip['name']}: {meta.get('clip_label') or meta['title']}")
    print("Title:", meta["title"])
    print("Intro:", meta["intro"])
    print("Clip:", main_clip and f"{main_clip['url']} {meta.get('clip_start')}-{meta.get('clip_end')}s "
          f"(relates: {meta.get('clip_relates')})")
    print("Outro:", meta.get("outro"))

    # B-roll = only the clips Gemini judged relevant (it saw frames from each), shown muted.
    # None relevant -> reuse other moments of the main clip. Stock only if there are no clips at all.
    ok = [i for i in meta.get("broll", []) if isinstance(i, int) and 0 <= i < len(clips)]
    broll_clips = [clips[i] for i in ok if clips[i] is not main_clip]
    print("B-roll clips:", [c["url"] for c in broll_clips] or "none relevant - reusing main clip")
    broll = [(c["path"], 3.0 if c["duration"] > 10 else 0.0) for c in broll_clips]
    if not broll and main_clip:
        d, cs = main_clip["duration"], float(meta.get("clip_start") or 0)
        broll = [(main_clip["path"], s) for s in dict.fromkeys([0.0, max(0.0, cs - 6), min(d * 0.6, d - 5)])]
    stock_used = False
    if not broll:
        broll = [(p, 0.0) for p in pexels_clips(meta.get("search_terms", []))]
        stock_used = bool(broll)
    if not broll:
        sys.exit("No footage found at all.")
    random.shuffle(broll)

    intro_mp3, outro_mp3 = WORK / "intro.mp3", WORK / "outro.mp3"
    intro_words = tts(meta["intro"], intro_mp3)
    t1 = duration(intro_mp3)
    pieces, sections, words, overlays = [(intro_mp3, 0, t1)], split_broll(broll, t1), list(intro_words), []
    credits = ([credit_for(c) for c in [main_clip] + broll_clips if c]
               + (["Stock footage: Pexels"] if stock_used else []))
    duck, bleeps = None, []

    if main_clip:
        cs = max(0.0, float(meta.get("clip_start") or 0))
        ce = min(main_clip["duration"], float(meta.get("clip_end") or cs + 15))
        if ce - cs < 4:
            ce = min(main_clip["duration"], cs + 15)
        t2 = ce - cs
        pieces.append((main_clip["path"], cs, t2))
        sections.append((main_clip["path"], cs, t2, False))
        clip_words = [(s - cs + t1, e - cs + t1, w) for s, e, w in words_in(main_clip["words"], cs, ce)]
        words += clip_words
        bleeps = [(max(0, s - 0.05), e + 0.05) for s, e, w in clip_words
                  if SLURS.match(re.sub(r"[^\w]", "", w))]
        if bleeps:
            print(f"Bleeping {len(bleeps)} word(s)")
        overlays.append((t1, t1 + t2, "Credit", credit_for(main_clip)))
        if meta.get("clip_label"):
            overlays.append((t1, t1 + t2, "Label", meta["clip_label"].upper()))
        duck = (t1, t1 + t2)
    else:
        t2 = 0

    total = t1 + t2
    if meta.get("outro"):
        outro_words = tts(meta["outro"], outro_mp3)
        t3 = duration(outro_mp3)
        pieces.append((outro_mp3, 0, t3))
        sections += split_broll(broll, t3)
        words += [(s + total, e + total, w) for s, e, w in outro_words]
        total += t3

    voice = WORK / "voice.wav"
    make_voice_track(pieces, voice)
    music, music_credit = pick_music()
    meta["credits"] = credits + ([music_credit] if music_credit else [])

    ass = WORK / "subs.ass"
    write_ass(words, total, ass, meta.get("hook_text", ""), overlays)
    out = WORK / "final.mp4"
    build_video(sections, voice, total, ass, out, music, duck, bleeps)
    print(f"Video: {total:.1f}s")

    if DRY_RUN:  # test mode: video is in Artifacts; don't upload or use up the clip
        print("TEST MODE - not uploaded. Download the video from this run's Artifacts.")
        return
    vid = upload(out, meta)
    history.append({"date": str(dt.date.today()), "topic": meta["topic"], "title": meta["source"]["title"],
                    "url": meta["source"]["url"], "video": vid, "streamer": main_clip["name"],
                    "yt_title": meta["title"], "views": None, "channel_id": meta.get("channel_id")})
    HISTORY.write_text(json.dumps(history, indent=1))


if __name__ == "__main__":
    main()
