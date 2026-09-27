"""Wholesome streamer moments - "edit" style Shorts bot.

Uses the same engine as the streamer-news bot (main.py) but a different format:
  no narrator -> the real moment with its original audio, big highlighted captions,
  soft emotional music, short emotional titles ("Kai Made His Day ❤️").

Pipeline: top clips of big streamers (7d + 30d) -> download, transcribe, look at frames
  -> Gemini picks the most wholesome moment + exact seconds -> strict check -> edit -> upload.
"""
import datetime as dt
import json
import os
import random
import re
import sys

import main as core  # shared engine: clips, Gemini/Groq, transcription, editing, upload

WORK, HISTORY = core.WORK, core.HISTORY
WATERMARK = os.environ.get("WATERMARK", "")  # e.g. "@WholesomeStreams" - shown small on the video
core.BASE_HASHTAGS = ["#shorts", "#wholesome", "#streamer", "#fyp", "#edit"]

# Words in clip titles that hint at a wholesome moment (clip titles are written by viewers)
SOFT_WORDS = ["wholesome", "cry", "cried", "tears", "fan", "mom", "dad", "kid", "baby", "gift",
              "surprise", "sweet", "love", "thank", "hug", "made his day", "made her day", "heart",
              "cute", "happy", "proud", "grandma", "grandpa", "family", "dream", "emotional", "❤", "🥹", "🥺"]
MUSIC = {"wholesome": ["piano", "acoustic", "heartwarming", "soft"],
         "emotional": ["emotional", "piano", "cinematic", "sad piano"],
         "funny": ["happy", "ukulele", "upbeat", "cheerful"]}


# ---------- 1. Candidates ----------
def find_candidates(history, avg_views, n_streamers=20, keep=10):
    """Popular clips from the last month (wholesome moments are rarer than drama, so we look
    further back), boosted when the title sounds wholesome."""
    used = {h.get("url") for h in history}
    overall = sum(avg_views.values()) / len(avg_views) if avg_views else 1
    weight = {n: min(4.0, max(0.3, avg_views[n] / overall)) if n in avg_views and overall else 1.0
              for n in core.STREAMER_ACCOUNTS}
    names = sorted(core.STREAMER_ACCOUNTS, key=lambda n: random.random() ** (1 / weight[n]),
                   reverse=True)[:n_streamers]
    pool = []
    for name in names:
        tw, kk = core.STREAMER_ACCOUNTS[name]
        got = ((core.list_twitch(tw, ranges=("7d", "30d"), limit=8) if tw else [])
               + (core.list_kick(kk, ranges=("week", "month"), limit=8) if kk else []))
        seen = set()
        for c in got:
            if c["url"] in used or c["url"] in seen or not 12 <= c["duration"] <= 90:
                continue
            seen.add(c["url"])
            c["name"] = name
            hits = sum(w in c["title"].lower() for w in SOFT_WORDS)
            c["score"] = c["views"] * (1 + 3 * hits)
            pool.append(c)
        print(f"{name}: {len(seen)} clips")
    pool.sort(key=lambda c: c["score"], reverse=True)
    picked, per = [], {}
    for c in pool:
        if per.get(c["name"], 0) < 2:
            picked.append(c)
            per[c["name"]] = per.get(c["name"], 0) + 1
        if len(picked) >= keep:
            break
    return picked


# ---------- 2. Pick + write ----------
def pick_moment(clips, recent, perf, rejected, use_images=True):
    blocks, images = [], []
    for i, c in enumerate(clips):
        seen = "no image"
        if c.get("sheet") and use_images:
            images.append(c["sheet"])
            seen = f"IMAGE #{len(images)} shows 3 frames from it"
        blocks.append(f"CLIP [{i}] {c['name']} ({c['platform']}) \"{c['title']}\" {c['duration']:.0f}s - {seen}\n"
                      + (core.as_timestamped_text(c["words"]) if c["words"] else "(no speech)"))
    prompt = f"""You run a WHOLESOME streamer moments Shorts channel (like @GoofyRecaps - "Speed Made His
Day ❤️"). Each Short is just the real moment with its original audio + captions, no narrator.

Below are popular recent clips from famous streamers, transcribed, with frames attached
(IMAGE #1, #2, ... in the order listed).

Recently posted (don't repeat): {recent}
How our past Shorts performed (favor what worked): {perf}
Clips already rejected, don't pick: {rejected or 'none'}

STEP 1 - pick the ONE most wholesome, heartwarming moment:
- GOOD: a streamer being kind to a fan/stranger/kid/elder, making someone's day, surprising
  someone, a gift, helping someone, a sweet family/friend moment, a fan meeting their idol,
  someone getting emotional in a good way, a wholesome funny moment where everyone's smiling.
- REJECT: drama, fights, insults, mean pranks, anything sexual, gambling, gameplay-only,
  sad-without-a-happy-side, confusing clips, or gibberish transcripts.
- Use the frames AND transcript to understand what really happens and who does what.
- clip_start/clip_end: 15-40 seconds covering the whole moment (setup -> sweet payoff),
  using the transcript timestamps. Never cut before the payoff.
- If NOTHING is genuinely wholesome, clip_index = -1.

STEP 2 - write:
- title: under 45 chars, emotional, simple, 1 emoji at the end, e.g. "Kai Made His Day ❤️",
  "xQc Helped A Stranger 🥹". Must be TRUE to what the clip shows. Add " #shorts".
- top_text: max 6 words shown small at the top for the first 3s to hook ("He didn't expect this...").
- highlight_words: 6-14 key words from the transcript to show in yellow (names, emotions, the
  important nouns/verbs - e.g. "rule", "phone", "love", "proud").
- mood: "wholesome", "emotional" or "funny" (picks the background music).
- description: 1-2 sentences describing the moment. hashtags: 3 (e.g. "#kaicenat").

Return JSON: {{"clip_index": int, "clip_start": float, "clip_end": float, "title": str,
"top_text": str, "highlight_words": [str], "mood": str, "description": str,
"hashtags": [str], "tags": [8 strings]}}

CLIPS:
{chr(10).join(blocks)}"""
    return core.gemini(prompt, temperature=0.6, images=images)


def check_moment(meta, clip, n):
    """Strict check -> (passed, problems)."""
    cs, ce = meta["clip_start"], meta["clip_end"]
    said = " ".join(w for _, _, w in core.words_in(clip["words"], cs, ce)) or "(no speech)"
    sheet = core.contact_sheet(clip, f"check{n}", times=(cs + 0.5, (cs + ce) / 2, ce - 0.5))
    data = core.gemini(f"""You are a strict reviewer for a WHOLESOME streamer-moments YouTube Short.

Clip from {clip['name']}'s channel. EXACT words the viewer hears: "{said}"
The image = 3 frames (start, middle, end) of that part, raw stream (any text is the stream's own).
Title: "{meta.get('title')}"   Top text: "{meta.get('top_text')}"   Description: "{meta.get('description')}"

CHECK:
a) Is this genuinely wholesome/heartwarming? (no meanness, fights, sexual stuff, mocking someone)
b) Are the title, top text and description TRUE to what the clip shows - right person, right
   action, no exaggeration or invented backstory?
c) Is the moment complete - does the viewer get the sweet payoff? (cut points are already snapped
   to word boundaries by software - don't nitpick timestamps)
d) Would a viewer understand what's happening within a few seconds?

Return JSON: {{"pass": bool, "problems": ["specific problem + how to fix"]}}""",
                       temperature=0.1, images=[sheet] if sheet else [])
    return bool(data.get("pass")), [str(p) for p in data.get("problems", [])]


def repair_text(meta, clip, problems):
    cs, ce = meta["clip_start"], meta["clip_end"]
    said = " ".join(w for _, _, w in core.words_in(clip["words"], cs, ce))
    fixed = core.gemini(f"""Fix the text of this wholesome streamer Short. Same clip ({clip['name']}).
Words heard: "{said}"
Current: title="{meta.get('title')}" top_text="{meta.get('top_text')}" description="{meta.get('description')}"
Problems: {problems}
Rules: only what the clip shows, right person, no exaggeration. Title under 45 chars, 1 emoji at
the end, ends with " #shorts". top_text max 6 words.
Return JSON {{"title": str, "top_text": str, "description": str}}""", temperature=0.3)
    for k in ("title", "top_text", "description"):
        if isinstance(fixed.get(k), str) and fixed[k].strip():
            meta[k] = fixed[k].strip()
    return meta


# ---------- 3. Captions (big, centered, key words in yellow) ----------
def write_captions(words, total, path, top_text, highlights, credit):
    hl = {re.sub(r"[^\w]", "", h.lower()) for h in highlights}
    head = """[Script Info]
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Big,DejaVu Sans,88,&H00FFFFFF,&H00000000,&H64000000,1,1,6,2,5,0
Style: Top,DejaVu Sans,52,&H00FFFFFF,&H00000000,&H64000000,1,1,4,1,8,330
Style: Credit,DejaVu Sans,36,&H00FFFFFF,&H00000000,&H90000000,1,3,3,0,2,210
Style: Mark,DejaVu Sans,40,&H60FFFFFF,&H60000000,&H00000000,1,1,2,0,2,300

[Events]
Format: Layer, Start, End, Style, Text
"""
    lines = []
    if top_text:
        lines.append(f"Dialogue: 1,{core.ts(0)},{core.ts(min(3.0, total))},Top,{{\\fad(150,300)}}"
                     f"{core.ass_escape(top_text)}")
    lines.append(f"Dialogue: 1,{core.ts(0)},{core.ts(total)},Credit,{core.ass_escape(credit)}")
    if WATERMARK:
        lines.append(f"Dialogue: 1,{core.ts(0)},{core.ts(total)},Mark,{core.ass_escape(WATERMARK)}")
    i = 0
    while i < len(words):  # 3 words per line, but break early on a sentence end or pause
        grp = [words[i]]
        while (len(grp) < 3 and i + len(grp) < len(words) and grp[-1][2][-1:] not in ".?!,"
               and words[i + len(grp)][0] - grp[-1][1] < 0.5):
            grp.append(words[i + len(grp)])
        nxt = words[i + len(grp)][0] if i + len(grp) < len(words) else total
        end = min(nxt, grp[-1][1] + 0.6)
        parts = []
        for _, _, w in grp:
            t = core.ass_escape(core.clean(w).upper())
            key = re.sub(r"[^\w]", "", w.lower())
            parts.append("{\\c&H00E5FF&}" + t + "{\\c&HFFFFFF&}" if key in hl else t)  # yellow
        lines.append(f"Dialogue: 0,{core.ts(grp[0][0])},{core.ts(end)},Big,"
                     f"{{\\blur1\\fscx108\\fscy108\\t(0,90,\\fscx100\\fscy100)}}{' '.join(parts)}")
        i += len(grp)
    path.write_text(head + "\n".join(lines), encoding="utf-8")


# ---------- main ----------
def main():
    WORK.mkdir(exist_ok=True)
    history = core.load_history()
    try:
        core.update_views(history)
    except Exception as e:
        print("view update failed:", e)
    HISTORY.write_text(json.dumps(history, indent=1))
    avg_views, perf = core.performance(history)
    print("Performance so far:\n" + perf)

    candidates = find_candidates(history, avg_views)
    if not candidates:
        sys.exit("No clips found.")
    clips = []
    for c in candidates:
        p = WORK / f"clip{len(clips)}.mp4"
        if core.download_clip(c, p):
            c["path"] = p
            try:
                c["words"] = core.transcribe(p)
            except Exception as e:
                print("transcribe failed:", e)
                c["words"] = []
            c["sheet"] = core.contact_sheet(c, len(clips))
            clips.append(c)
    print(f"{len(clips)} clips ready")

    recent = [h.get("topic") for h in history[-40:]]
    rejected, passed, n = [], False, 0
    for clip_try in range(3):
        try:
            meta = pick_moment(clips, recent, perf, rejected)
        except core.Blocked:
            try:
                meta = pick_moment(clips, recent, perf, rejected, use_images=False)
            except core.Blocked:
                sys.exit("AI refused these clips - trying again next run.")
        idx = meta.get("clip_index", -1)
        clip = clips[idx] if isinstance(idx, int) and 0 <= idx < len(clips) else None
        if not clip or idx in rejected:
            print("No (more) wholesome clips in this batch.")
            break
        meta["clip_start"], meta["clip_end"] = core.snap_window(
            clip["words"], float(meta.get("clip_start") or 0), float(meta.get("clip_end") or 25),
            clip["duration"], lo=12.0, hi=45.0)
        print(f"Moment #{clip_try + 1}: {clip['name']} {clip['url']} {meta['clip_start']}-{meta['clip_end']}s"
              f"\n  Title: {meta.get('title')}")
        for fix in range(2):
            n += 1
            try:
                passed, problems = check_moment(meta, clip, n)
            except core.Blocked:
                passed, problems = False, []
                break
            print(f"Check #{n}: {'PASSED' if passed else 'FAILED'}", *problems, sep="\n  ")
            if passed or fix == 1 or not problems:
                break
            try:
                meta = repair_text(meta, clip, problems)
                print(f"  Repaired title: {meta.get('title')}")
            except core.Blocked:
                break
        if passed:
            break
        rejected.append(idx)
    if not passed:
        sys.exit("No wholesome moment passed the check this run - skipping instead of posting a bad one.")

    # ---- edit: the moment itself, original audio, captions, soft music ----
    cs, ce = meta["clip_start"], meta["clip_end"]
    total = ce - cs
    voice = WORK / "voice.wav"
    core.make_voice_track([(clip["path"], cs, total)], voice)
    words = [(s - cs, e - cs, w) for s, e, w in core.words_in(clip["words"], cs, ce)]
    bleeps = [(max(0, s - 0.05), e + 0.05) for s, e, w in words
              if core.SLURS.match(re.sub(r"[^\w]", "", w))]

    core.MUSIC_MOODS = MUSIC.get(meta.get("mood"), MUSIC["wholesome"])
    music, music_credit = core.pick_music()
    ass = WORK / "subs.ass"
    write_captions(words, total, ass, meta.get("top_text", ""), meta.get("highlight_words", []),
                   core.credit_for(clip))
    out = WORK / "final.mp4"
    core.build_video([(clip["path"], cs, total, False)], voice, total, ass, out, music, None, bleeps)
    print(f"Video: {total:.1f}s")

    meta["source"] = {"title": clip["title"], "url": clip["url"]}
    meta["credits"] = [core.credit_for(clip)] + ([music_credit] if music_credit else [])
    vid = None if core.DRY_RUN else core.upload(out, meta)
    history.append({"date": str(dt.date.today()), "topic": f"{clip['name']}: {meta.get('title')}",
                    "title": clip["title"], "url": clip["url"], "video": vid, "streamer": clip["name"],
                    "yt_title": meta.get("title"), "views": None})
    HISTORY.write_text(json.dumps(history, indent=1))


if __name__ == "__main__":
    main()
