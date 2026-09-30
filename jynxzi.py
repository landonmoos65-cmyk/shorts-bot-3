"""Jynxzi funny moments - "edit" style Shorts bot.

Same engine and editing as the wholesome bot (wholesome.py + main.py). Only the clip source
(Jynxzi only), what counts as a good moment (FUNNY), titles, and music are different.
No background music: his reactions ARE the audio.
"""
import random

import main as core
import wholesome as base

STREAMER = "Jynxzi"
TWITCH_USER = "jynxzi"
core.BASE_HASHTAGS = ["#shorts", "#jynxzi", "#funny", "#twitch", "#r6", "#fyp"]
FUNNY_WORDS = ["lmao", "lol", "rage", "scream", "crash", "mom", "💀", "😭", "funny", "bro", "tweak",
               "lost it", "mad", "pissed", "reaction", "caught", "fell", "died", "insane", "crazy"]


# Never touch clips whose titles point to slurs, leaked private info, or controversy - these could
# hurt him, get the video removed, or get the channel in trouble.
BLOCK_WORDS = ["n word", "nword", "n-word", "slur", "racist", "leak", "card", "address", "dox",
               "phone number", "password", "nsfw", "banned for"]


def find_candidates(history, avg_views, keep=10):
    """Half recent (last 7/30 days), half all-time classics - never a clip we already used.
    All-time clips have millions of views, so they're ranked in their own group; otherwise
    they'd always push the recent ones out."""
    used = {h.get("url") for h in history}
    recent = pick_from(core.list_twitch(TWITCH_USER, ranges=("7d", "30d"), limit=40), used, keep // 2)
    used |= {c["url"] for c in recent}
    classics = pick_from(core.list_twitch(TWITCH_USER, ranges=("all",), limit=100), used, keep - len(recent))
    print(f"{STREAMER}: {len(recent)} recent + {len(classics)} all-time candidates")
    return recent + classics


def pick_from(got, used, n):
    pool, seen = [], set()
    for c in got:
        if c["url"] in used or c["url"] in seen or not 8 <= c["duration"] <= 90:
            continue
        if any(b in c["title"].lower() for b in BLOCK_WORDS):
            print("blocked:", c["title"])
            continue
        seen.add(c["url"])
        c["name"] = STREAMER
        hits = sum(w in c["title"].lower() for w in FUNNY_WORDS)
        # recent clips first, then views; a little randomness so we don't always get the same order
        c["score"] = c["views"] * (1 + hits) * (1.5 if c["range"] == "7d" else 1.0) * random.uniform(0.8, 1.2)
        pool.append(c)
    return sorted(pool, key=lambda c: c["score"], reverse=True)[:n]


def pick_moment(clips, recent, perf, rejected, use_images=True):
    blocks, images = [], []
    for i, c in enumerate(clips):
        seen = "no image"
        if c.get("sheet") and use_images:
            images.append(c["sheet"])
            seen = f"IMAGE #{len(images)} shows 3 frames from it"
        blocks.append(f"CLIP [{i}] \"{c['title']}\" {c['views']} views {c['duration']:.0f}s - {seen}\n"
                      + (core.as_timestamped_text(c["words"]) if c["words"] else "(no speech)"))
    return core.gemini(f"""You run a Jynxzi clips Shorts channel. Each Short is just the real moment with its
original audio + captions, no narrator. Jynxzi is a huge Twitch streamer (Rainbow Six Siege) known
for screaming, raging, over-the-top reactions, his mom, and chaotic moments with friends.

Below are his popular recent clips, transcribed, with frames attached (IMAGE #1, #2, ...).
Recently posted (don't repeat the same bit): {recent}
How our past Shorts performed (favor what worked): {perf}
Already rejected, don't pick: {rejected or 'none'}

STEP 1 - pick the ONE FUNNIEST moment:
- GOOD: huge reactions, rage that's funny (not scary), unexpected twists, funny lines, his mom,
  chaotic moments with friends, a clear setup -> punchline.
- REJECT: boring/quiet gameplay, clips you can't understand without context, anything mean to a
  real person (bullying, harassment), sexual stuff, slur-heavy clips, gibberish transcripts.
- clip_start/clip_end: 10-35 seconds with the whole joke (setup -> payoff), from the timestamps.
  Never cut before the punchline. Shorter and punchier is better.
- If nothing is actually funny, clip_index = -1.

STEP 2 - write:
- title: under 45 chars, 1 emoji at the end, e.g. "Jynxzi Lost His Mind 😭", "Jynxzi's Mom Caught
  Him 💀". Must be TRUE to the clip. Add " #shorts".
- top_text: max 6 words shown at the top for the first 3s ("Wait for it...", "He did NOT expect this").
- highlight_words: 6-14 funniest/most important words from the transcript to show in yellow.
- boom_times: 1-3 exact moments (in the CLIP's own seconds, from the transcript timestamps, inside
  clip_start..clip_end) for a "vine boom" sound: the punchline word, the peak scream/reaction,
  or the craziest beat. Use the START time of that word. Fewer is better - only the biggest beats.
- mood: "funny".
- description: 1-2 sentences. hashtags: 3. tags: 8.

Return JSON: {{"clip_index": int, "clip_start": float, "clip_end": float, "title": str,
"top_text": str, "highlight_words": [str], "boom_times": [float], "mood": str,
"description": str, "hashtags": [str], "tags": [str]}}

CLIPS:
{chr(10).join(blocks)}""", temperature=0.6, images=images)


def check_moment(meta, clip, n):
    cs, ce = meta["clip_start"], meta["clip_end"]
    said = " ".join(w for _, _, w in core.words_in(clip["words"], cs, ce)) or "(no speech)"
    sheet = core.contact_sheet(clip, f"check{n}", times=(cs + 0.5, (cs + ce) / 2, ce - 0.5))
    data = core.gemini(f"""You are a strict reviewer for a Jynxzi funny-moments YouTube Short.

EXACT words the viewer hears: "{said}"
The image = 3 frames (start, middle, end) of that part, raw stream (any text is the stream's own).
Title: "{meta.get('title')}"   Top text: "{meta.get('top_text')}"   Description: "{meta.get('description')}"

CHECK:
a) Is it actually funny/entertaining with a clear payoff a viewer gets within a few seconds?
b) Are the title, top text and description TRUE to the clip (right people, no invented story)?
c) Is the punchline included? (cut points are already snapped to word boundaries - don't nitpick)
d) Nothing mean-spirited toward a real person, sexual, or slur-heavy? No private info visible
   or said (card numbers, addresses, phone numbers, passwords)? Any of these = fail.

Return JSON: {{"pass": bool, "problems": ["specific problem + how to fix"]}}""",
                       temperature=0.1, images=[sheet] if sheet else [])
    return bool(data.get("pass")), [str(p) for p in data.get("problems", [])]


def no_music():  # his audio is the content
    return None, None


# Plug the Jynxzi versions into the shared wholesome pipeline and run it
base.find_candidates = lambda history, avg_views: find_candidates(history, avg_views)
base.pick_moment = pick_moment
base.check_moment = check_moment
core.pick_music = no_music
base.USE_BOOMS = True

if __name__ == "__main__":
    base.main()
