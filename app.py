import streamlit as st
import streamlit.components.v1 as components
import pdfplumber
import edge_tts
import asyncio
import os
import json
import hashlib
import base64
import html
import threading
import google.generativeai as genai

# --- 1. Set up the Google AI Director ---
# PASTE YOUR API KEY BETWEEN THE QUOTES BELOW!
API_KEY = st.secrets["API_KEY"]
genai.configure(api_key=API_KEY, transport="rest")

model = genai.GenerativeModel(
    'gemini-3.5-flash-lite',
    generation_config={"response_mime_type": "application/json"}
)

MEMORY_FILE = "book_memory.json"
CACHE_DIR = "audio_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# Internal voice catalog (handled automatically by the AI)
VOICE_CATALOG = {
    "en-US-GuyNeural":         {"gender": "Male",   "traits": "Warm, balanced, neutral storyteller (best for Narrator)"},
    "en-US-RogerNeural":       {"gender": "Male",   "traits": "Mature, confident, authoritative, bold mentor or leader"},
    "en-US-ChristopherNeural": {"gender": "Male",   "traits": "Deep, serious, grounded, intense"},
    "en-US-SteffanNeural":     {"gender": "Male",   "traits": "Calm, thoughtful, educated, polite, less aggressive"},
    "en-US-BrianNeural":       {"gender": "Male",   "traits": "Gentle, mild, cautious, relatable everyday person"},
    "en-US-EricNeural":        {"gender": "Male",   "traits": "Energetic, youthful, enthusiastic, friendly"},
    "en-GB-RyanNeural":        {"gender": "Male",   "traits": "Formal, refined, polished British accent"},
    "en-US-AriaNeural":        {"gender": "Female", "traits": "Confident, expressive, dynamic, assertive"},
    "en-US-JennyNeural":       {"gender": "Female", "traits": "Friendly, warm, conversational, approachable"},
    "en-US-AvaNeural":         {"gender": "Female", "traits": "Natural, articulate, calm, modern"},
    "en-US-MichelleNeural":    {"gender": "Female", "traits": "Soft-spoken, gentle, empathetic, quiet"},
    "en-GB-SoniaNeural":       {"gender": "Female", "traits": "Poised, formal, sophisticated British accent"},
}

VALID_RATES = {"-8%", "-4%", "+0%", "+6%"}
VALID_PITCHES = {"-4Hz", "-2Hz", "+0Hz", "+3Hz"}

ROLE_COLORS = [
    "#2563eb",  # Blue (Narrator)
    "#d97706",  # Amber
    "#059669",  # Emerald
    "#7c3aed",  # Purple
    "#dc2626",  # Red
    "#0891b2",  # Cyan
    "#db2777",  # Pink
]

@st.cache_resource
def get_bg_tracker():
    return {"lock": threading.Lock(), "in_progress": set()}

# --- 2. Memory, Cache & PDF Helpers ---
def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_memory(memory_data):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory_data, f, indent=2)

def get_book_id(file_bytes):
    return hashlib.md5(file_bytes).hexdigest()

def get_page_cache_path(book_id, page_num):
    book_folder = os.path.join(CACHE_DIR, book_id)
    os.makedirs(book_folder, exist_ok=True)
    # v2 cache includes word-by-word timestamps and spoiler-free labels
    return os.path.join(book_folder, f"page_{page_num}_v2.json")

def is_page_cached(book_id, page_num):
    return os.path.exists(get_page_cache_path(book_id, page_num))

def load_cached_page(book_id, page_num):
    path = get_page_cache_path(book_id, page_num)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def extract_clean_page_text(page, top_trim=0.08, bottom_trim=0.08):
    top = page.height * top_trim
    bottom = page.height * (1.0 - bottom_trim)
    if bottom <= top:
        return page.extract_text() or ""
    cropped = page.crop((0, top, page.width, bottom), relative=True)
    return cropped.extract_text() or ""

def normalize_cast_entry(entry):
    if isinstance(entry, str):
        return {"voice": entry, "personality": "Standard voice", "rate": "+0%", "pitch": "+0Hz"}
    return {
        "voice": entry.get("voice", "en-US-GuyNeural"),
        "personality": entry.get("personality", ""),
        "rate": entry.get("rate", "+0%"),
        "pitch": entry.get("pitch", "+0Hz")
    }

def get_revealed_up_to_page(book_info, page_num):
    """Returns only character names/links revealed on or before page_num (spoiler-free)."""
    revealed = book_info.get("revealed_names", {})
    allowed = {}
    for role, info in revealed.items():
        if isinstance(info, dict) and info.get("page", 999999) <= page_num:
            allowed[role] = info.get("label", "")
    return allowed

def update_revealed_names(book_id, page_num, newly_revealed):
    """Saves newly revealed character names with the earliest page they appeared on."""
    if not newly_revealed or not isinstance(newly_revealed, dict):
        return
    tracker = get_bg_tracker()
    with tracker["lock"]:
        mem = load_memory()
        if book_id not in mem:
            return
        rev = mem[book_id].setdefault("revealed_names", {})
        changed = False
        for role, label in newly_revealed.items():
            clean_label = str(label).strip()
            if not clean_label or len(clean_label) > 22:
                continue
            existing = rev.get(role)
            if not existing or page_num < existing.get("page", 999999):
                rev[role] = {"label": clean_label, "page": page_num}
                changed = True
        if changed:
            save_memory(mem)

# --- 3. Pass 1: Automatic Personality Casting ---
def get_voice_menu_text():
    return "\n".join(f'- "{vid}" ({info["gender"]}): {info["traits"]}' for vid, info in VOICE_CATALOG.items())

def analyze_book_for_cast(filename, pdf):
    opening_text = ""
    for i in range(min(4, len(pdf.pages))):
        opening_text += extract_clean_page_text(pdf.pages[i]) + "\n"
    opening_text = opening_text[:3500]
    voice_menu = get_voice_menu_text()

    fast_prompt = f"""
    You are an expert audiobook casting director. Look at this PDF filename and opening text.
    Filename: {filename}
    Opening Text:
    {opening_text}

    Available Voices:
    {voice_menu}

    Available Speeds: {list(VALID_RATES)}
    Available Pitches: {list(VALID_PITCHES)}

    Task:
    1. Determine if this is a well-known published book that you already know the storyline and characters of.
    2. If YES ("is_known": true), provide the book title, a 2-sentence summary, and ALL main speaking/quoted characters (2 to 8 characters—never omit major contrasting characters like BOTH "Rich Dad" AND "Poor Dad").
    3. FIRST-PERSON RULE: Exclude the first-person narrator/author themselves (they use "Narrator").
    4. PERSONALITY CASTING: Match each character's confidence, age, and emotional tone to the best unique voice, rate, and pitch.
    5. If NO ("is_known": false), return an empty characters list.

    Output strict JSON:
    {{
        "is_known": true or false,
        "title": "Book Title",
        "summary": "Short storyline summary",
        "characters": [
            {{
                "name": "Character Name",
                "gender": "Male",
                "personality": "Brief 4-8 word personality description",
                "voice": "exact voice id",
                "rate": "+0%",
                "pitch": "+0Hz"
            }}
        ]
    }}
    """
    resp = model.generate_content(fast_prompt, request_options={"timeout": 60})
    result = json.loads(resp.text)

    if result.get("is_known") and len(result.get("characters", [])) >= 2:
        result["source"] = "Popular Book Fast-Track ⚡"
        return result

    total_pages = len(pdf.pages)
    step = max(1, total_pages // 12)
    sampled_text = ""
    for idx in range(0, total_pages, step):
        txt = extract_clean_page_text(pdf.pages[idx])
        sampled_text += f"\n--- Page {idx+1} ---\n" + txt[:2000]
        if len(sampled_text) > 25000:
            break

    scan_prompt = f"""
    You are an expert audiobook casting director. Read these sampled pages and identify ALL main speaking/quoted characters.
    FIRST-PERSON RULE: Exclude the first-person narrator/author themselves (they use "Narrator").

    Available Voices:
    {voice_menu}

    Available Speeds: {list(VALID_RATES)}
    Available Pitches: {list(VALID_PITCHES)}

    Output strict JSON:
    {{
        "title": "Best guess of the book title",
        "summary": "2-sentence summary of the book's context",
        "characters": [
            {{
                "name": "Character Name",
                "gender": "Male",
                "personality": "Brief 4-8 word personality description",
                "voice": "exact voice id",
                "rate": "+0%",
                "pitch": "+0Hz"
            }}
        ]
    }}

    Sampled Text:
    {sampled_text}
    """
    resp2 = model.generate_content(scan_prompt, request_options={"timeout": 60})
    result2 = json.loads(resp2.text)
    result2["source"] = "Full PDF Scan 📖"
    return result2

def build_personality_cast(characters):
    cast = {
        "Narrator": {
            "voice": "en-US-GuyNeural",
            "personality": "Main storyteller & first-person perspective",
            "rate": "+0%",
            "pitch": "+0Hz"
        }
    }
    used_voices = {"en-US-GuyNeural"}
    male_voices = [v for v, info in VOICE_CATALOG.items() if info["gender"] == "Male" and v != "en-US-GuyNeural"]
    female_voices = [v for v, info in VOICE_CATALOG.items() if info["gender"] == "Female"]

    for char in characters:
        name = char.get("name", "").strip()
        gender = char.get("gender", "Male").strip().lower()
        if not name or name.lower() == "narrator":
            continue

        chosen_voice = char.get("voice", "")
        pool = female_voices if gender == "female" else male_voices
        if chosen_voice not in VOICE_CATALOG or chosen_voice in used_voices:
            chosen_voice = next((v for v in pool if v not in used_voices), pool[0])
        used_voices.add(chosen_voice)

        rate = char.get("rate", "+0%") if char.get("rate") in VALID_RATES else "+0%"
        pitch = char.get("pitch", "+0Hz") if char.get("pitch") in VALID_PITCHES else "+0Hz"

        cast[name] = {
            "voice": chosen_voice,
            "personality": char.get("personality", "Supporting character"),
            "rate": rate,
            "pitch": pitch
        }

    backup_male = next((v for v in male_voices if v not in used_voices), male_voices[-1])
    backup_female = next((v for v in female_voices if v not in used_voices), female_voices[-1])
    cast["Other Male"] = {"voice": backup_male, "personality": "Minor male speaker", "rate": "+0%", "pitch": "+0Hz"}
    cast["Other Female"] = {"voice": backup_female, "personality": "Minor female speaker", "rate": "+0%", "pitch": "+0Hz"}
    return cast

# --- 4. Pass 2: Spoiler-Free Script & Word-Level Audio Builder ---
def ai_director(text_chunk, book_info, page_num):
    allowed_roles = list(book_info["cast"].keys())
    already_revealed = get_revealed_up_to_page(book_info, page_num)

    prompt = f"""
    You are an audiobook director converting Page {page_num} from "{book_info['title']}" into a dramatic multi-voice script.
    Book Context: {book_info['summary']}
    Allowed Roles: {json.dumps(allowed_roles)}
    Already Revealed Short Names/Links (safe to show): {json.dumps(already_revealed)}

    STRICT RULES:
    1. CLEANUP FIRST: Ignore and remove page numbers, running headers, repeated book/chapter titles at the top or bottom, website links, or copyright footers.
    2. SMART DIALOGUE TAG REMOVAL (AUDIO DRAMA STYLE):
       - Completely REMOVE plain, redundant dialogue tags like "I said", "he said", "she asked", "Rich Dad said", "my poor dad replied", etc., whenever they ONLY identify the speaker. The character's unique voice and on-screen badge already show who is talking.
       - DO NOT remove tags that contain an action, emotion, or important detail (e.g., "Rich Dad said, smiling warmly" or "he whispered nervously"). Keep those as "Narrator" lines so no story details are lost.
    3. VOICE SWITCHING: Whenever ANY other character speaks or is quoted (text inside quotation marks), split that quote into its own item and assign it to their exact role from Allowed Roles (NEVER assign another person's quote to "Narrator").
    4. Use "Narrator" ONLY for narration, action/emotion descriptions, and lines spoken by the first-person narrator ("I" / their younger self).
    5. If a quoted speaker is not in Allowed Roles, use "Other Male" or "Other Female".
    6. Strip quotation marks from the spoken text.
    7. SPOILER-FREE BRACKETED NAMES:
       - ONLY if a character's real name or short relationship link is explicitly revealed ON THIS PAGE (or is already in Already Revealed Short Names/Links), add it in short brackets (1-2 words max) in "display_role", e.g., "Narrator (Robert)" or "Rich Dad (Mike's Dad)".
       - NEVER spoil a name or identity from later in the book if it has NOT been revealed on this page or in Already Revealed Short Names/Links! If not yet revealed, set "display_role" equal to "role".
       - List any brand-new names/links explicitly revealed on THIS page inside "newly_revealed" (keep each label 1-2 words max).

    Output strict JSON object:
    {{
        "newly_revealed": {{
            "RoleName": "Short Name"
        }},
        "script": [
            {{
                "role": "Exact Role from Allowed Roles",
                "display_role": "Role or Role (Short Name)",
                "text": "Exact words to be spoken"
            }}
        ]
    }}

    Text to analyze:
    {text_chunk}
    """
    response = model.generate_content(prompt, request_options={"timeout": 60})
    parsed = json.loads(response.text)
    if isinstance(parsed, list):
        return parsed, {}
    return parsed.get("script", []), parsed.get("newly_revealed", {})

async def synthesize_segments(script_parts, cast_profiles):
    profile_lookup = {k.lower(): normalize_cast_entry(v) for k, v in cast_profiles.items()}
    narrator_profile = profile_lookup.get("narrator", normalize_cast_entry("en-US-GuyNeural"))
    fallback_profile = profile_lookup.get("other male", normalize_cast_entry("en-US-EricNeural"))

    segments = []
    for part in script_parts:
        role = part.get("role", "Narrator").strip()
        display_role = part.get("display_role", role).strip() or role
        text_to_speak = part.get("text", "").strip()
        if not text_to_speak:
            continue

        role_key = role.lower()
        if role_key == "narrator":
            profile = narrator_profile
        elif role_key in profile_lookup:
            profile = profile_lookup[role_key]
        else:
            profile = fallback_profile

        # Request WordBoundary timestamps (compatible with both edge-tts v6 and v7)
        try:
            communicate = edge_tts.Communicate(
                text_to_speak,
                profile["voice"],
                rate=profile["rate"],
                pitch=profile["pitch"],
                boundary="WordBoundary"
            )
        except TypeError:
            communicate = edge_tts.Communicate(
                text_to_speak,
                profile["voice"],
                rate=profile["rate"],
                pitch=profile["pitch"]
            )

        audio_bytes = b""
        word_timings = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes += chunk["data"]
            elif chunk["type"] == "WordBoundary":
                start_sec = chunk["offset"] / 10_000_000.0
                end_sec = (chunk["offset"] + chunk["duration"]) / 10_000_000.0
                word_timings.append({
                    "w": chunk["text"],
                    "s": round(start_sec, 3),
                    "e": round(end_sec, 3)
                })

        if audio_bytes:
            segments.append({
                "role": role,
                "display_role": display_role,
                "text": text_to_speak,
                "words": word_timings,
                "audio_b64": base64.b64encode(audio_bytes).decode("ascii")
            })
    return segments

def prepare_and_cache_page(book_id, page_num, clean_text, book_info):
    if is_page_cached(book_id, page_num):
        return load_cached_page(book_id, page_num)

    if not clean_text.strip():
        data = {"page": page_num, "segments": []}
    else:
        script_parts, newly_revealed = ai_director(clean_text, book_info, page_num)
        if newly_revealed:
            update_revealed_names(book_id, page_num, newly_revealed)
        segments = asyncio.run(synthesize_segments(script_parts, book_info["cast"]))
        data = {"page": page_num, "segments": segments}

    cache_path = get_page_cache_path(book_id, page_num)
    tmp_path = cache_path + f".tmp_{threading.get_ident()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, cache_path)
    return data

def preload_pages_in_background(book_id, pages_to_preload, book_info):
    tracker = get_bg_tracker()

    def worker(p_num, text_str):
        job_key = (book_id, p_num)
        try:
            prepare_and_cache_page(book_id, p_num, text_str, book_info)
        except Exception:
            pass
        finally:
            with tracker["lock"]:
                tracker["in_progress"].discard(job_key)

    for p_num, text_str in pages_to_preload:
        job_key = (book_id, p_num)
        if is_page_cached(book_id, p_num):
            continue
        with tracker["lock"]:
            if job_key in tracker["in_progress"]:
                continue
            tracker["in_progress"].add(job_key)
        t = threading.Thread(target=worker, args=(p_num, text_str), daemon=True)
        t.start()

# --- 5. Word-by-Word Live Audio Reader (HTML + JS) ---
def render_karaoke_player(page_num, total_pages, segments, role_colors, auto_start, has_next_page):
    lines_html = []
    js_segments = []

    for i, seg in enumerate(segments):
        role = seg.get("role", "Narrator")
        display_role = seg.get("display_role", role)
        color = role_colors.get(role.lower(), "#2563eb")
        safe_role = html.escape(display_role)

        words_data = seg.get("words", [])
        if words_data:
            word_spans = "".join(
                f'<span class="w" id="w-{i}-{w_idx}">{html.escape(w_obj["w"])}</span> '
                for w_idx, w_obj in enumerate(words_data)
            )
            timings = [{"s": w_obj["s"], "e": w_obj["e"]} for w_obj in words_data]
            word_count = len(words_data)
        else:
            raw_words = seg.get("text", "").split()
            word_spans = "".join(
                f'<span class="w" id="w-{i}-{w_idx}">{html.escape(w_str)}</span> '
                for w_idx, w_str in enumerate(raw_words)
            )
            timings = []
            word_count = len(raw_words)

        lines_html.append(f"""
        <div class="seg" id="seg-{i}" onclick="jumpTo({i})">
            <span class="badge" style="background:{color};">{safe_role}</span>
            <span class="txt">{word_spans}</span>
        </div>
        """)

        js_segments.append({
            "audio": seg["audio_b64"],
            "timings": timings,
            "count": word_count
        })

    segments_json = json.dumps(js_segments)
    joined_lines = "\n".join(lines_html)
    next_page_num = page_num + 1

    player_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 6px;
            background: transparent;
            color: #1f2937;
        }}
        .page-banner {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 8px;
            background: #f1f5f9;
            border: 1px solid #cbd5e1;
            border-radius: 10px 10px 0 0;
            padding: 8px 14px;
            font-size: 14px;
            font-weight: 700;
            color: #0f172a;
        }}
        .toggle-label {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 13px;
            font-weight: 600;
            color: #334155;
            cursor: pointer;
            user-select: none;
        }}
        .toggle-label input {{
            width: 16px;
            height: 16px;
            accent-color: #2563eb;
            cursor: pointer;
        }}
        .controls {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
            padding: 10px 14px;
            background: #111827;
            color: #ffffff;
            border-radius: 0 0 10px 10px;
            margin-bottom: 10px;
        }}
        .btn {{
            background: #2563eb;
            color: white;
            border: none;
            padding: 7px 14px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
        }}
        .btn:hover {{ background: #1d4ed8; }}
        .speed-group {{
            display: flex;
            gap: 4px;
            margin-left: auto;
        }}
        .spd-btn {{
            background: #374151;
            color: #e5e7eb;
            border: none;
            padding: 5px 10px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }}
        .spd-btn.active-spd {{
            background: #2563eb;
            color: #ffffff;
        }}
        .reader {{
            max-height: 410px;
            overflow-y: auto;
            padding: 14px;
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            line-height: 1.85;
            font-size: 17px;
        }}
        .seg {{
            padding: 8px 10px;
            margin-bottom: 8px;
            border-radius: 8px;
            border-left: 3px solid transparent;
            cursor: pointer;
            transition: background 0.15s ease;
        }}
        .seg:hover {{
            background: #f8fafc;
        }}
        .seg.active-seg {{
            background: #f8fafc;
            border-left: 3px solid #2563eb;
        }}
        .w {{
            padding: 1px 2px;
            border-radius: 4px;
            transition: background 0.08s ease;
        }}
        .w.active-word {{
            background: #fde047;
            color: #111827;
            font-weight: 600;
            box-shadow: 0 0 0 1px #eab308;
        }}
        .badge {{
            display: inline-block;
            color: #ffffff;
            font-size: 11px;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 999px;
            margin-right: 8px;
            vertical-align: middle;
            letter-spacing: 0.3px;
        }}
        .next-page-box {{
            display: none;
            margin-top: 14px;
            padding: 14px;
            text-align: center;
            background: #eff6ff;
            border: 2px dashed #3b82f6;
            border-radius: 10px;
        }}
        .next-page-btn {{
            background: #2563eb;
            color: #ffffff;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
        }}
        .next-page-btn:hover {{
            background: #1d4ed8;
        }}
    </style>
    </head>
    <body>
        <div class="page-banner">
            <span>📄 Page {page_num} of {total_pages}</span>
            <label class="toggle-label" title="Toggle without interrupting playback">
                <input type="checkbox" id="autoTurnToggle" onchange="onToggleAutoTurn()">
                🔄 Auto-turn page
            </label>
        </div>
        <div class="controls">
            <button class="btn" id="playBtn" onclick="togglePlay()">▶ Play Page {page_num}</button>
            <button class="btn" style="background:#374151;" onclick="restartPage()">↺ Restart</button>
            <div class="speed-group">
                <button class="spd-btn" id="spd-0.85" onclick="setSpeed(0.85)">🐢 Slow</button>
                <button class="spd-btn" id="spd-1" onclick="setSpeed(1.0)">🚶 Medium</button>
                <button class="spd-btn" id="spd-1.25" onclick="setSpeed(1.25)">🐇 Fast</button>
            </div>
        </div>

        <div class="reader" id="readerBox">
            {joined_lines}
            <div class="next-page-box" id="nextPageBox">
                <div style="font-size:13px; color:#1e40af; margin-bottom:8px; font-weight:600;">
                    End of Page {page_num}
                </div>
                <button class="next-page-btn" onclick="turnToNextPageWithSound()">
                    📖 Turn to Next Page (Page {next_page_num}) ➡️
                </button>
            </div>
        </div>

        <audio id="player"></audio>

        <script>
            const segments = {segments_json};
            const autoStart = {"true" if auto_start else "false"};
            const hasNextPage = {"true" if has_next_page else "false"};

            let currentIdx = 0;
            let lastWordIdx = -1;
            let isPlaying = false;
            let pageFinished = false;
            let audioCtx = null;

            const audio = document.getElementById("player");
            const playBtn = document.getElementById("playBtn");
            const autoTurnToggle = document.getElementById("autoTurnToggle");
            const nextPageBox = document.getElementById("nextPageBox");

            // Load saved Auto-Turn & Speed preferences from browser (no Streamlit reload!)
            let autoTurn = localStorage.getItem("audioDrama_autoTurn") !== "false";
            autoTurnToggle.checked = autoTurn;

            let playbackSpeed = parseFloat(localStorage.getItem("audioDrama_speed") || "1.0");
            if (![0.85, 1.0, 1.25].includes(playbackSpeed)) playbackSpeed = 1.0;

            function updateSpeedButtons() {{
                document.querySelectorAll(".spd-btn").forEach(b => b.classList.remove("active-spd"));
                const activeBtn = document.getElementById("spd-" + playbackSpeed);
                if (activeBtn) activeBtn.classList.add("active-spd");
            }}
            updateSpeedButtons();

            function setSpeed(rate) {{
                playbackSpeed = rate;
                localStorage.setItem("audioDrama_speed", String(rate));
                audio.playbackRate = rate;
                updateSpeedButtons();
            }}

            function onToggleAutoTurn() {{
                autoTurn = autoTurnToggle.checked;
                localStorage.setItem("audioDrama_autoTurn", String(autoTurn));
                // Show or hide the Turn-to-Next-Page prompt if page already finished
                if (pageFinished && hasNextPage) {{
                    nextPageBox.style.display = autoTurn ? "none" : "block";
                    if (!autoTurn) {{
                        nextPageBox.scrollIntoView({{ behavior: "smooth", block: "end" }});
                    }}
                }}
            }}

            // Realistic Paper Page-Turn Sound (Web Audio API)
            function playPageTurnSound() {{
                try {{
                    const Ctx = window.AudioContext || window.webkitAudioContext;
                    if (!audioCtx) audioCtx = new Ctx();
                    if (audioCtx.state === "suspended") audioCtx.resume();

                    const duration = 0.42;
                    const sampleRate = audioCtx.sampleRate;
                    const bufferSize = Math.floor(sampleRate * duration);
                    const buffer = audioCtx.createBuffer(1, bufferSize, sampleRate);
                    const data = buffer.getChannelData(0);

                    // Shaped noise: soft paper lift -> airy swish -> gentle settle
                    for (let i = 0; i < bufferSize; i++) {{
                        const t = i / bufferSize;
                        const env = Math.sin(Math.PI * t) * (0.6 + 0.4 * Math.sin(Math.PI * 3 * t));
                        data[i] = (Math.random() * 2 - 1) * env * 0.45;
                    }}

                    const noise = audioCtx.createBufferSource();
                    noise.buffer = buffer;

                    const filter = audioCtx.createBiquadFilter();
                    filter.type = "bandpass";
                    filter.Q.value = 1.2;
                    filter.frequency.setValueAtTime(1100, audioCtx.currentTime);
                    filter.frequency.exponentialRampToValueAtTime(3000, audioCtx.currentTime + 0.18);
                    filter.frequency.exponentialRampToValueAtTime(900, audioCtx.currentTime + duration);

                    const gain = audioCtx.createGain();
                    gain.gain.setValueAtTime(0.01, audioCtx.currentTime);
                    gain.gain.linearRampToValueAtTime(0.85, audioCtx.currentTime + 0.12);
                    gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + duration);

                    noise.connect(filter);
                    filter.connect(gain);
                    gain.connect(audioCtx.destination);
                    noise.start();
                }} catch (e) {{
                    console.log("Page turn sound skipped:", e);
                }}
            }}

            function clearWordHighlights() {{
                document.querySelectorAll(".w.active-word").forEach(el => el.classList.remove("active-word"));
            }}

            function setActiveSegment(idx) {{
                document.querySelectorAll(".seg.active-seg").forEach(el => el.classList.remove("active-seg"));
                clearWordHighlights();
                lastWordIdx = -1;
                const segEl = document.getElementById("seg-" + idx);
                if (segEl) {{
                    segEl.classList.add("active-seg");
                    segEl.scrollIntoView({{ behavior: "smooth", block: "center" }});
                }}
            }}

            function highlightWord(segIdx, wIdx) {{
                if (wIdx === lastWordIdx) return;
                clearWordHighlights();
                const wEl = document.getElementById("w-" + segIdx + "-" + wIdx);
                if (wEl) {{
                    wEl.classList.add("active-word");
                    lastWordIdx = wIdx;
                }}
            }}

            function updateWordSync() {{
                const seg = segments[currentIdx];
                if (!seg || seg.count === 0) return;
                const t = audio.currentTime;

                if (seg.timings && seg.timings.length > 0) {{
                    let activeW = 0;
                    for (let i = 0; i < seg.timings.length; i++) {{
                        if (t >= seg.timings[i].s) {{
                            activeW = i;
                        }} else {{
                            break;
                        }}
                    }}
                    highlightWord(currentIdx, activeW);
                }} else if (audio.duration > 0) {{
                    const ratio = Math.min(0.999, Math.max(0, t / audio.duration));
                    const activeW = Math.floor(ratio * seg.count);
                    highlightWord(currentIdx, activeW);
                }}
            }}

            function triggerNextPage() {{
                try {{
                    const parentButtons = window.parent.document.querySelectorAll("button");
                    for (const btn of parentButtons) {{
                        if (btn.innerText && btn.innerText.includes("Next Page")) {{
                            btn.click();
                            return;
                        }}
                    }}
                }} catch (e) {{
                    console.log("Auto-flip unavailable:", e);
                }}
            }}

            function turnToNextPageWithSound() {{
                playPageTurnSound();
                setTimeout(() => {{
                    triggerNextPage();
                }}, 380);
            }}

            function playIndex(idx) {{
                if (idx >= segments.length) {{
                    isPlaying = false;
                    pageFinished = true;
                    clearWordHighlights();
                    playBtn.textContent = "✅ Page {page_num} Finished";
                    if (hasNextPage) {{
                        if (autoTurn) {{
                            playBtn.textContent = "📖 Turning to Page {next_page_num}...";
                            turnToNextPageWithSound();
                        }} else {{
                            nextPageBox.style.display = "block";
                            nextPageBox.scrollIntoView({{ behavior: "smooth", block: "end" }});
                        }}
                    }}
                    return;
                }}
                pageFinished = false;
                nextPageBox.style.display = "none";
                currentIdx = idx;
                setActiveSegment(idx);
                audio.src = "data:audio/mp3;base64," + segments[idx].audio;
                audio.playbackRate = playbackSpeed;
                const playPromise = audio.play();
                if (playPromise !== undefined) {{
                    playPromise.then(() => {{
                        isPlaying = true;
                        playBtn.textContent = "⏸ Pause";
                    }}).catch(() => {{
                        isPlaying = false;
                        playBtn.textContent = "▶ Tap to Play Page {page_num}";
                    }});
                }}
            }}

            function togglePlay() {{
                if (!audio.src || pageFinished) {{
                    playIndex(pageFinished ? 0 : currentIdx);
                    return;
                }}
                if (isPlaying) {{
                    audio.pause();
                    isPlaying = false;
                    playBtn.textContent = "▶ Resume";
                }} else {{
                    audio.playbackRate = playbackSpeed;
                    audio.play();
                    isPlaying = true;
                    playBtn.textContent = "⏸ Pause";
                }}
            }}

            function jumpTo(idx) {{
                playIndex(idx);
            }}

            function restartPage() {{
                playIndex(0);
            }}

            audio.addEventListener("timeupdate", updateWordSync);
            audio.addEventListener("ended", () => {{
                playIndex(currentIdx + 1);
            }});

            if (segments.length > 0) {{
                setActiveSegment(0);
                if (autoStart) {{
                    playIndex(0);
                }}
            }}
        </script>
    </body>
    </html>
    """
    components.html(player_html, height=565, scrolling=False)

# --- 6. App Interface (Clean & Non-Techie) ---
st.set_page_config(page_title="Audio Drama Reader", page_icon="🎭", layout="centered")
st.title("Audio Drama Reader 🎭")

uploaded_file = st.file_uploader("Upload your PDF book to start listening", type="pdf")

if uploaded_file is not None:
    file_bytes = uploaded_file.getvalue()
    book_id = get_book_id(file_bytes)
    memory = load_memory()

    with pdfplumber.open(uploaded_file) as pdf:
        total_pages = len(pdf.pages)

        if book_id not in memory:
            with st.spinner("Preparing your book & casting character voices for the first time..."):
                try:
                    analysis = analyze_book_for_cast(uploaded_file.name, pdf)
                    cast = build_personality_cast(analysis.get("characters", []))
                    memory[book_id] = {
                        "title": analysis.get("title", uploaded_file.name),
                        "summary": analysis.get("summary", ""),
                        "source": analysis.get("source", "Scanned"),
                        "cast": cast,
                        "revealed_names": {},
                        "last_page": 1
                    }
                    save_memory(memory)
                except Exception as e:
                    st.error(f"Could not prepare book: {e}")
                    st.stop()

        book_info = memory[book_id]

        if st.session_state.get("active_book_id") != book_id:
            last_saved = min(book_info.get("last_page", 1), total_pages)
            st.session_state["active_book_id"] = book_id
            st.session_state["current_page"] = last_saved
            st.session_state["last_left_page"] = last_saved
            st.session_state["auto_play"] = False

        current_page = st.session_state["current_page"]
        last_left_page = st.session_state["last_left_page"]

        # Book Title & Page Status Banner
        st.subheader(f"📖 {book_info['title']}")
        m1, m2 = st.columns(2)
        with m1:
            st.info(f"📄 **Currently Showing:** Page **{current_page}** of {total_pages}")
        with m2:
            st.caption(f"🔖 **Last Left Off:** Page **{last_left_page}**")
            if last_left_page != current_page:
                if st.button(f"↩️ Back to Page {last_left_page}", use_container_width=True):
                    st.session_state["current_page"] = last_left_page
                    st.session_state["auto_play"] = False
                    st.rerun()

        # Page Jump Form (Only loads when you click Go / press Enter)
        with st.form(key=f"page_jump_form_{book_id}"):
            c_num, c_go = st.columns([3, 1])
            with c_num:
                target_page = st.number_input(
                    f"Go to Page (1 – {total_pages})",
                    min_value=1,
                    max_value=total_pages,
                    value=current_page,
                    step=1
                )
            with c_go:
                st.write("")
                st.write("")
                go_clicked = st.form_submit_button("Go ↵", use_container_width=True)

        if go_clicked and int(target_page) != current_page:
            st.session_state["current_page"] = int(target_page)
            st.session_state["auto_play"] = False
            st.rerun()

        # Previous / Next Page Buttons
        col_prev, col_next = st.columns(2)
        with col_prev:
            if st.button("⬅️ Previous Page", disabled=(current_page <= 1), use_container_width=True):
                st.session_state["current_page"] = current_page - 1
                st.session_state["auto_play"] = True
                st.rerun()
        with col_next:
            if st.button("Next Page ➡️", disabled=(current_page >= total_pages), use_container_width=True):
                st.session_state["current_page"] = current_page + 1
                st.session_state["auto_play"] = True
                st.rerun()

        # Save reading progress
        if memory[book_id].get("last_page") != current_page:
            memory[book_id]["last_page"] = current_page
            save_memory(memory)

        # Prepare current page
        page_obj = pdf.pages[current_page - 1]
        clean_text = extract_clean_page_text(page_obj).replace('\n', ' ')

        if not is_page_cached(book_id, current_page):
            with st.spinner(f"Preparing word-synced voices for Page {current_page}..."):
                try:
                    page_data = prepare_and_cache_page(book_id, current_page, clean_text, book_info)
                except Exception as e:
                    st.error(f"Could not generate Page {current_page}: {e}")
                    st.stop()
        else:
            page_data = load_cached_page(book_id, current_page)

        # Pre-load the next 2 pages in the background
        upcoming_pages = []
        for offset in (1, 2):
            next_p = current_page + offset
            if next_p <= total_pages and not is_page_cached(book_id, next_p):
                next_txt = extract_clean_page_text(pdf.pages[next_p - 1]).replace('\n', ' ')
                upcoming_pages.append((next_p, next_txt))

        if upcoming_pages:
            preload_pages_in_background(book_id, upcoming_pages, book_info)

        next_page_num = current_page + 1
        if next_page_num <= total_pages:
            if is_page_cached(book_id, next_page_num):
                st.caption(f"⚡ Page {next_page_num} is pre-loaded and ready.")
            else:
                st.caption(f"⏳ Preparing Page {next_page_num} & {min(next_page_num + 1, total_pages)} in the background...")

        role_colors = {}
        for idx, role_name in enumerate(book_info["cast"].keys()):
            role_colors[role_name.lower()] = ROLE_COLORS[idx % len(ROLE_COLORS)]

        segments = page_data.get("segments", [])
        if segments:
            render_karaoke_player(
                page_num=current_page,
                total_pages=total_pages,
                segments=segments,
                role_colors=role_colors,
                auto_start=st.session_state.get("auto_play", False),
                has_next_page=(current_page < total_pages)
            )

            combined_mp3 = b"".join(base64.b64decode(s["audio_b64"]) for s in segments)
            st.download_button(
                label=f"⬇️ Download Page {current_page} MP3",
                data=combined_mp3,
                file_name=f"page_{current_page}_drama.mp3",
                mime="audio/mp3"
            )
        else:
            st.info(f"Page {current_page} has no readable text. Click **Next Page ➡️** to continue.")

        with st.expander("ℹ️ Book Cast & Reset"):
            st.caption(book_info["summary"])
            for role, entry in book_info["cast"].items():
                prof = normalize_cast_entry(entry)
                st.write(f"**{role}**: {prof['personality']}")
            if st.button("🔄 Re-scan Book & Clear Cache"):
                del memory[book_id]
                save_memory(memory)
                cache_folder = os.path.join(CACHE_DIR, book_id)
                if os.path.exists(cache_folder):
                    for fn in os.listdir(cache_folder):
                        os.remove(os.path.join(cache_folder, fn))
                st.rerun()