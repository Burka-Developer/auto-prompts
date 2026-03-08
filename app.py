"""
YouTube Automation Tool — Unified Web App
==========================================
Flask backend: Gemini generation + Template management + Folder creation + Bulk Automation

Run:  python app.py
Open: http://localhost:5000
"""

import json
import logging
import os
import re
import sys
import io
import time
import math
import uuid
import zipfile
import threading
from pathlib import Path
from typing import Optional
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file, Response, stream_with_context
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from google import genai
import anthropic
from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════════
# Setup
# ═══════════════════════════════════════════════════════════════════════════
load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("yt-auto")

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
UPLOAD_DIR = BASE_DIR / "uploads"
TEMPLATES_FILE = BASE_DIR / "templates_data.json"

GEMINI_MODEL = "gemini-2.5-flash"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_RETRIES = 4
RETRY_DELAY = 8
RETRY_BACKOFF_MULTIPLIER = 2.0  # exponential backoff: 8s, 16s, 32s, 64s...
RETRY_MAX_DELAY = 120  # cap at 2 minutes
BULK_ITEM_DELAY = 5  # seconds between bulk items (base)
BULK_ITEM_DELAY_AFTER_429 = 30  # seconds after a 429 error
CLAUDE_BATCH_DELAY = 15  # seconds sleep between Claude batch requests to preserve limits
BULK_PARALLEL_WORKERS = 3        # concurrent video-generation threads
ADOBE_PARALLEL_BATCHES = 2       # concurrent Adobe Stock API-call threads
LOG_DIR = BASE_DIR / "logs"

def _ensure_log_dir():
    LOG_DIR.mkdir(exist_ok=True)

_ensure_log_dir()

# File logger for persistent error analysis
_file_handler = logging.FileHandler(LOG_DIR / "generation.log", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
log.addHandler(_file_handler)


def _load_api_keys() -> list[str]:
    """Load one or more Gemini API keys from env vars.

    Supports GEMINI_API_KEYS (comma/semicolon/newline separated) or
    GEMINI_API_KEY (single key). Duplicate/blank entries are removed while
    preserving order.
    """

    raw = (os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not raw:
        return []

    keys = []
    for part in re.split(r"[,;\n]+", raw):
        key = part.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _load_claude_api_keys() -> list[str]:
    """Load one or more Claude/Anthropic API keys from env vars.

    Supports CLAUDE_API_KEYS (comma/semicolon/newline separated) or
    CLAUDE_API_KEY (single key).
    """
    raw = (os.getenv("CLAUDE_API_KEYS") or os.getenv("CLAUDE_API_KEY") or "").strip()
    if not raw:
        return []
    keys = []
    for part in re.split(r"[,;\n]+", raw):
        key = part.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


API_KEYS = _load_api_keys()
CLAUDE_API_KEYS = _load_claude_api_keys()
_api_key_lock = threading.Lock()
_api_key_index = 0
_claude_key_lock = threading.Lock()
_claude_key_index = 0
_key_quota_until: dict[str, float] = {}  # key → unix timestamp when its quota cooldown expires
_claude_key_quota_until: dict[str, float] = {}
KEY_QUOTA_COOLDOWN = 65                   # seconds before a quota-exceeded key is retried


def _mask_key(key: str) -> str:
    """Obfuscate key for logging."""
    return f"...{key[-4:]}" if len(key) > 6 else "[key]"


def _count_available_keys() -> int:
    """Number of keys NOT currently in quota cooldown."""
    now = time.time()
    return sum(1 for k in API_KEYS if now >= _key_quota_until.get(k, 0))


def _mark_key_quota(key: str) -> None:
    """Mark a key as quota-exceeded; it will be skipped for KEY_QUOTA_COOLDOWN seconds."""
    with _api_key_lock:
        _key_quota_until[key] = time.time() + KEY_QUOTA_COOLDOWN
    log.warning(
        "[KEYRING] Key %s → QUOTA COOLDOWN %ds | available=%d/%d",
        _mask_key(key), KEY_QUOTA_COOLDOWN, _count_available_keys(), len(API_KEYS),
    )


def _get_available_key() -> str:
    """Return the next key not in quota cooldown (round-robin, thread-safe).
    If ALL keys are in cooldown, returns the one recovering soonest."""
    if not API_KEYS:
        raise RuntimeError("No Gemini API keys configured. Set GEMINI_API_KEYS or GEMINI_API_KEY in .env")
    global _api_key_index
    now = time.time()
    with _api_key_lock:
        for i in range(len(API_KEYS)):
            idx = (_api_key_index + i) % len(API_KEYS)
            key = API_KEYS[idx]
            if now >= _key_quota_until.get(key, 0):
                _api_key_index = (idx + 1) % len(API_KEYS)
                return key
        # All keys cooling — return the one recovering soonest
        best_idx = min(range(len(API_KEYS)), key=lambda i: _key_quota_until.get(API_KEYS[i], 0))
        _api_key_index = (best_idx + 1) % len(API_KEYS)
        return API_KEYS[best_idx]


def _secs_until_any_key_available() -> float:
    """Seconds until at least one key exits cooldown. Returns 0 if any key is ready now."""
    if not API_KEYS:
        return 0.0
    now = time.time()
    if any(now >= _key_quota_until.get(k, 0) for k in API_KEYS):
        return 0.0
    return max(0.0, min(_key_quota_until.get(k, 0) for k in API_KEYS) - now)


# Legacy alias kept for any callers
def _next_api_key() -> str:
    return _get_available_key()


# ── Claude Key Management ──────────────────────────────────────────────────

def _count_available_claude_keys() -> int:
    now = time.time()
    return sum(1 for k in CLAUDE_API_KEYS if now >= _claude_key_quota_until.get(k, 0))


def _mark_claude_key_quota(key: str) -> None:
    with _claude_key_lock:
        _claude_key_quota_until[key] = time.time() + KEY_QUOTA_COOLDOWN
    log.warning(
        "[CLAUDE-KEYRING] Key %s → QUOTA COOLDOWN %ds | available=%d/%d",
        _mask_key(key), KEY_QUOTA_COOLDOWN, _count_available_claude_keys(), len(CLAUDE_API_KEYS),
    )


def _get_available_claude_key() -> str:
    if not CLAUDE_API_KEYS:
        raise RuntimeError("No Claude API keys configured. Set CLAUDE_API_KEYS or CLAUDE_API_KEY in .env")
    global _claude_key_index
    now = time.time()
    with _claude_key_lock:
        for i in range(len(CLAUDE_API_KEYS)):
            idx = (_claude_key_index + i) % len(CLAUDE_API_KEYS)
            key = CLAUDE_API_KEYS[idx]
            if now >= _claude_key_quota_until.get(key, 0):
                _claude_key_index = (idx + 1) % len(CLAUDE_API_KEYS)
                return key
        best_idx = min(range(len(CLAUDE_API_KEYS)), key=lambda i: _claude_key_quota_until.get(CLAUDE_API_KEYS[i], 0))
        _claude_key_index = (best_idx + 1) % len(CLAUDE_API_KEYS)
        return CLAUDE_API_KEYS[best_idx]


def _secs_until_any_claude_key_available() -> float:
    if not CLAUDE_API_KEYS:
        return 0.0
    now = time.time()
    if any(now >= _claude_key_quota_until.get(k, 0) for k in CLAUDE_API_KEYS):
        return 0.0
    return max(0.0, min(_claude_key_quota_until.get(k, 0) for k in CLAUDE_API_KEYS) - now)


def _is_claude_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return bool(
        "rate_limit" in text
        or "rate limit" in text
        or "overloaded" in text
        or "429" in text
        or "too many requests" in text
    )


def _is_quota_error(exc: Exception) -> bool:
    """Detect quota / rate limit style errors from the SDK."""

    text = str(exc).lower()
    code = getattr(exc, "code", None) or getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return bool(
        (code == 429)
        or "quota" in text
        or "rate limit" in text
        or "exceeded" in text
        or "resource exhausted" in text
        or "too many requests" in text
        or "429" in text
    )

# ═══════════════════════════════════════════════════════════════════════════
# Bulk Automation State
# ═══════════════════════════════════════════════════════════════════════════
bulk_jobs = {}  # job_id -> job state dict
bulk_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════
# Pydantic Schemas (Gemini structured output)
# ═══════════════════════════════════════════════════════════════════════════

class UploadPack(BaseModel):
    upload_title: str = Field(description="Final YouTube title — SEO-optimised, emotional, curiosity-driven (60-100 chars).")
    upload_description: str = Field(description="Full YouTube description: hook lines, summary, timestamps placeholder, CTA, hashtags.")
    tags: list[str] = Field(description="20-30 YouTube tags.")
    keywords: list[str] = Field(description="10-15 core SEO keywords.")
    hashtags: list[str] = Field(description="3-5 hashtags (without # prefix).")


class Scene(BaseModel):
    scene_number: int = Field(description="Sequential scene number starting at 1.")
    scene_title: str = Field(description="Short descriptive title (3-8 words).")
    voiceover: str = Field(description="Full narration/voiceover for this scene. Include ambient cues in brackets like [Soft Rain].")
    dialogue: Optional[str] = Field(default=None, description="Character dialogue or narrator lines for this scene. Null if no dialogue requested.")
    image_prompts: list[str] = Field(
        default_factory=list,
        description="List of detailed AI image-generation prompts (60-150 words each). Each prompt shows a different camera angle, composition, or perspective of the same scene while maintaining IDENTICAL character appearance, art style, lighting direction, and color palette. Include Character Master Variable verbatim. End with quality tags."
    )
    video_prompts: list[str] = Field(
        default_factory=list,
        description="List of detailed AI video-generation prompts (60-150 words each). Each varies camera MOVEMENT and subject MOTION while keeping character and style perfectly consistent. Include Character Master Variable. Optimised for Runway/Kling/Pika."
    )
    image_to_video_prompts: list[str] = Field(
        default_factory=list,
        description="List of prompts for converting scene images into animated video. Describes motion, transitions, camera moves. Optimised for Runway image-to-video."
    )


class GenerationResult(BaseModel):
    video_concept_title: str = Field(description="Overall concept/title of the video.")
    character_master_variable: Optional[str] = Field(
        default=None,
        description="Immutable visual description of recurring character(s). Null if no recurring character."
    )
    scenes: list[Scene] = Field(description="Ordered list of scenes.")
    upload_pack: UploadPack = Field(description="YouTube upload metadata.")


class TitleIdea(BaseModel):
    title: str = Field(description="YouTube title (60-100 chars), SEO-optimized, curiosity-driven, emotional.")
    hook_angle: str = Field(description="The psychological hook used: curiosity gap, fear, authority, numbers, secret, challenge, urgency, etc.")
    why_it_works: str = Field(description="Brief explanation of why this title would get high CTR on YouTube.")
    target_emotion: str = Field(description="Primary emotion triggered: curiosity, fear, awe, urgency, outrage, hope, etc.")
    content_brief: str = Field(description="2-3 sentence outline of what this video would cover.")


class NicheTitleResult(BaseModel):
    niche: str = Field(description="The niche analyzed.")
    niche_analysis: str = Field(description="Brief analysis: audience size, trends, competition, content gaps, growth potential.")
    audience_profile: str = Field(description="Target audience demographics and psychographics.")
    titles: list[TitleIdea] = Field(description="List of viral title ideas.")
    content_strategy_tips: list[str] = Field(description="3-5 actionable content strategy tips for this niche.")


# ═══════════════════════════════════════════════════════════════════════════
# Adobe Stock — Pydantic Schemas
# ═══════════════════════════════════════════════════════════════════════════

class AdobeStockImagePrompt(BaseModel):
    number: int = Field(description="Sequential image number in this batch, starting from (concept_offset * variations_per_concept) + 1.")
    concept_id: int = Field(description="Which concept group this image belongs to (1-indexed from concept_offset+1).")
    variation_id: int = Field(description="Which variation within the concept (1 to variations_per_concept).")
    variation_label: str = Field(description="Short label describing this variation type: 'Wide Establishing', 'Medium Shot', 'Close-Up Detail', 'Alternative Angle', 'Different Lighting', 'Environmental Context', etc.")
    concept_name: str = Field(description="Short 4-10 word label for this concept that captures the subject and setting.")
    title: str = Field(description="Adobe Stock image title — specific, keyword-rich, 15-100 chars. Describe EXACTLY what is depicted. Example: 'Senior woman laughing while using VR headset in bright living room' NOT 'Happy woman with technology'.")
    prompt: str = Field(description="Full 4K AI generation prompt, 150-300 words. Must include: subject description, scene/environment, camera body, lens focal length and aperture, lighting setup with Kelvin color temp, composition rule, color palette, depth of field, film grain specification, authentic imperfections notes, and quality suffix. Self-contained — directly usable in Midjourney, Adobe Firefly, DALL-E 3, Stable Diffusion.")
    negative_prompt: str = Field(description="Elements to actively exclude: watermarks, text overlays, logos, brand names, extra fingers, floating limbs, artificial HDR, neon glow effects, over-sharpening, plastic skin texture, etc.")
    keywords: list[str] = Field(description="35-50 Adobe Stock keywords. All lowercase. Mix of: primary subject, secondary elements, setting, mood, colors, industry/vertical, commercial use terms, abstract concepts. Example: ['senior woman', 'virtual reality', 'vr headset', 'living room', 'active aging', 'technology', 'lifestyle', ...].")


class AdobeStockBatchResult(BaseModel):
    niche: str = Field(description="The niche being generated for.")
    concept_offset: int = Field(description="0-indexed starting concept number for this batch.")
    niche_analysis: str = Field(description="2026 Adobe Stock demand analysis for this niche: current demand level, top-selling types, gaps in existing stock, RPM potential, seasonality, key buyer segments. 100-200 words.")
    monetization_tips: list[str] = Field(description="4-6 specific, actionable tips to maximize revenue and acceptance rate on Adobe Stock for this niche in 2026.")
    images: list[AdobeStockImagePrompt] = Field(description="Ordered list of all image prompts in this batch.")


# ═══════════════════════════════════════════════════════════════════════════
# Template Management (from Prompting Funda)
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_TEMPLATE = {
    "Voice_Over": {"files": ["voiceover_script"], "file_contents": {"voiceover_script": ""}},
    "Images": {"files": ["hook_images", "actual_images"], "file_contents": {"hook_images": "", "actual_images": ""}},
    "Videos": {"files": ["hook_video", "actual_video"], "file_contents": {"hook_video": "", "actual_video": ""}},
}

# ═══════════════════════════════════════════════════════════════════════════
# Niche Presets — Professional configurations for popular YouTube niches
# ═══════════════════════════════════════════════════════════════════════════

NICHE_PRESETS = {
    "Dark Psychology": {
        "description": "Manipulation, narcissism, covert influence, mind games, toxic behavior awareness",
        "sub_topics": ["Narcissistic Abuse", "Gaslighting", "Dark Triad", "Manipulation Red Flags", "Covert Control", "Psychological Warfare", "Toxic Relationships", "Love Bombing", "Emotional Exploitation", "Shadow Psychology"],
        "image_style": "Cinematic noir photography, deep shadows with selective warm amber highlights, moody atmospheric fog, dark teal and crimson palette, psychological thriller aesthetic, dramatic chiaroscuro, 8K photorealistic",
        "video_style": "Slow suspenseful dolly zoom, noir cinematography, atmospheric particle effects, dramatic shadow play, smooth transitions, dark ambient, cinematic depth of field",
        "tone": "Authoritative, mysterious, educational, revealing hidden truths",
        "target_audience": "18-45, psychology enthusiasts, self-protection seekers, relationship awareness",
    },
    "Stoicism & Philosophy": {
        "description": "Ancient wisdom, Marcus Aurelius, Seneca, mental toughness, philosophical life lessons",
        "sub_topics": ["Marcus Aurelius", "Seneca's Letters", "Epictetus Teachings", "Memento Mori", "Amor Fati", "Discipline", "Emotional Control", "Daily Stoic Practices", "Philosophical Paradoxes", "Wisdom of Ages"],
        "image_style": "Classical Renaissance painting style, marble textures, golden hour lighting, warm sepia and bronze tones, ancient Greek/Roman architecture, dramatic oil painting aesthetic, museum-quality composition",
        "video_style": "Slow majestic pan across classical art, gentle parallax on marble statues, smooth cinematic zoom, warm atmospheric lighting, documentary elegance",
        "tone": "Calm authoritative, philosophical depth, timeless wisdom, contemplative",
        "target_audience": "20-50, self-improvement seekers, intellectuals, men's development audience",
    },
    "True Crime & Mystery": {
        "description": "Unsolved cases, criminal psychology, forensic analysis, mystery deep dives, cold cases",
        "sub_topics": ["Unsolved Murders", "Criminal Profiling", "Cold Cases", "Forensic Psychology", "Serial Killers", "Disappearances", "Crime Scene Analysis", "Justice System", "Criminal Mind", "Mystery Investigations"],
        "image_style": "Dark documentary photography, crime scene aesthetic, cool blue and desaturated tones, evidence board style, dramatic directional lighting, gritty texture, investigation room atmosphere",
        "video_style": "Slow tracking shot through evidence, documentary zoom on details, Ken Burns effect on photos, suspenseful camera movements, cold color grading, film noir transitions",
        "tone": "Investigative journalist, suspenseful, fact-driven, gripping narrative",
        "target_audience": "18-55, true crime community, podcast listeners, mystery enthusiasts",
    },
    "Horror & Creepypasta": {
        "description": "Scary stories, urban legends, creepypasta narration, paranormal, nightmare fuel",
        "sub_topics": ["Creepypasta Stories", "Urban Legends", "Paranormal Encounters", "Sleep Paralysis", "Dark Web Stories", "Haunted Places", "SCP Foundation", "Nightmare Scenarios", "Folklore Monsters", "Reddit Horror"],
        "image_style": "Dark horror aesthetic, deep black shadows, blood red and ghostly blue accents, fog and mist, abandoned places, distorted perspectives, unsettling compositions, grainy film texture, dread atmosphere",
        "video_style": "Slow creeping dolly forward, sudden subtle movements, flickering light effects, dutch angle shots, found footage style, atmospheric horror, unsettling transitions",
        "tone": "Whispered intensity, building dread, immersive storytelling, atmospheric horror narrator",
        "target_audience": "16-40, horror fans, late-night viewers, creepypasta community",
    },
    "Personal Finance & Wealth": {
        "description": "Money psychology, investing, passive income, wealth building, financial freedom strategies",
        "sub_topics": ["Passive Income Streams", "Stock Market", "Real Estate Investing", "Money Psychology", "Budgeting Hacks", "Crypto Basics", "Side Hustle Ideas", "Rich vs Poor Mindset", "Tax Strategy", "Financial Independence"],
        "image_style": "Clean modern corporate aesthetic, gold and dark navy color scheme, luxury minimalism, financial charts and data overlays, premium glass and metal textures, sharp professional lighting, wealth aesthetic",
        "video_style": "Smooth corporate pan, dynamic data visualization animations, modern motion graphics style, clean transitions, professional zoom on key points, luxury b-roll feel",
        "tone": "Confident expert, actionable advice, data-backed, motivational yet practical",
        "target_audience": "22-45, aspiring investors, entrepreneurs, career professionals",
    },
    "Space & Cosmos": {
        "description": "Universe mysteries, black holes, NASA discoveries, alien life, cosmic phenomena",
        "sub_topics": ["Black Holes", "Alien Life", "Mars Colonization", "Multiverse Theory", "Neutron Stars", "Space Exploration", "Hubble Discoveries", "Quantum Universe", "Exoplanets", "Dark Matter"],
        "image_style": "Epic cosmic photography, deep space nebula colors, vibrant purple and electric blue, star field backgrounds, NASA-quality rendering, volumetric cosmic fog, ultra-wide cinematic framing, 8K space art",
        "video_style": "Sweeping cosmic flythrough, slow orbital camera movement, nebula particle effects, epic scale zoom transitions, weightless camera drift, awe-inspiring reveals",
        "tone": "Wonder-filled narrator, scientific authority, mind-expanding, Carl Sagan style awe",
        "target_audience": "15-55, science enthusiasts, curious minds, documentary lovers",
    },
    "History & Civilizations": {
        "description": "Ancient civilizations, lost empires, historical mysteries, war history, forgotten stories",
        "sub_topics": ["Ancient Egypt", "Roman Empire", "Medieval Period", "World War Secrets", "Lost Civilizations", "Samurai History", "Viking Age", "Ottoman Empire", "Ancient Greece", "Cold War"],
        "image_style": "Epic historical painting style, warm golden age lighting, atmospheric period-accurate scenes, dramatic battle compositions, ancient architecture, weathered texture, museum-quality historical illustration",
        "video_style": "Ken Burns pan across historical art, dramatic zoom on artifacts, sweeping landscape shots, period-matched color grading, documentary pacing, epic reveal transitions",
        "tone": "Scholarly storyteller, epic narrative, bringing history alive, dramatic moments",
        "target_audience": "20-60, history buffs, documentary watchers, educational content seekers",
    },
    "Motivation & Self-Improvement": {
        "description": "Discipline, habits, success mindset, productivity, mental toughness, life transformation",
        "sub_topics": ["Morning Routines", "Atomic Habits", "Dopamine Detox", "Monk Mode", "Discipline Secrets", "Goal Setting", "Confidence Building", "Time Management", "Mental Toughness", "Success Psychology"],
        "image_style": "High-contrast cinematic photography, dramatic sunrise golden hour, powerful silhouettes, bold typography overlay areas, athletic/warrior aesthetic, strong directional lighting, inspiring composition",
        "video_style": "Dynamic motivational pacing, powerful slow-motion moments, sunrise timelapse, bold camera movements, energy-building rhythm, cinematic impact cuts",
        "tone": "Powerful motivational speaker, no-nonsense tough love, action-oriented, transformational",
        "target_audience": "16-40, self-improvement seekers, students, young professionals, fitness enthusiasts",
    },
    "Relationship Psychology": {
        "description": "Attachment styles, dating psychology, attraction science, love languages, relationship dynamics",
        "sub_topics": ["Attachment Theory", "Attraction Psychology", "Love Languages", "Red Flags", "Communication Skills", "Breakup Recovery", "Dating Mistakes", "Emotional Intelligence", "Trust Building", "Healthy Boundaries"],
        "image_style": "Warm intimate cinematography, soft bokeh backgrounds, warm amber and rose tones, couples silhouettes, emotional portrait style, gentle natural lighting, romantic yet educational aesthetic",
        "video_style": "Soft focus transitions, gentle camera movements, warm color grading, intimate close-ups, emotional pacing, heartfelt documentary style",
        "tone": "Empathetic expert, warm yet direct, psychologically informed, healing-focused",
        "target_audience": "18-45, singles, couples, heartbreak recovery, emotional growth seekers",
    },
    "Conspiracy & Hidden Knowledge": {
        "description": "Cover-ups, secret societies, government secrets, hidden history, unexplained phenomena",
        "sub_topics": ["Secret Societies", "Government Cover-ups", "Ancient Aliens", "Hidden Technology", "Forbidden Archaeology", "MK Ultra", "Illuminati", "Suppressed Inventions", "Deep State", "Unexplained Events"],
        "image_style": "Dark investigative aesthetic, evidence board with red string connections, classified document style, green matrix tones with dark amber, surveillance camera feel, mysterious shadow lighting, conspiracy wall aesthetic",
        "video_style": "Quick investigative cuts, zoom into documents, redacted text reveals, dramatic evidence connections, surveillance-style footage, investigative documentary pacing",
        "tone": "Investigative truth-seeker, questioning authority, connecting dots, thought-provoking",
        "target_audience": "18-50, alternative thinkers, truth seekers, podcast community, documentary fans",
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# Adobe Stock — Niche Configurations
# ═══════════════════════════════════════════════════════════════════════════

ADOBE_STOCK_NICHES = {
    "Hyper-Local Culture & Food": {
        "description": "Regional architecture, local festivals, authentic ethnic foods, and street markets. High demand because global AI models miss hyper-specific cultural visuals.",
        "sub_niches": ["Street Food Vendors", "Local Festivals & Celebrations", "Regional Architecture", "Traditional Crafts", "Ethnic Market Stalls", "Cultural Ceremonies", "Local Artisans", "Traditional Clothing"],
        "photography_style": "Documentary street photography, natural ambient light, authentic candid moments, film grain, warm color grading, shallow DOF for subject isolation",
        "ctr_potential": "Very High",
        "competition": "Low — AI poorly covers hyper-local specifics",
        "key_buyers": "Travel platforms, food delivery apps, tourism boards, lifestyle magazines",
        "primary_keywords": ["street food", "local culture", "ethnic food", "traditional", "authentic", "festival", "regional cuisine", "cultural heritage"],
        "icon": "fa-map-marker-alt",
        "color": "#f0a444",
    },
    "Authentic Messy Tech Workspaces": {
        "description": "Real messy desks, cable management struggles, authentic home offices. Market tired of glowing blue holograms — buyers want relatable workspaces.",
        "sub_niches": ["Messy Gaming Desks", "Home Office Chaos", "Developer Workspace", "Cable Management", "Multi-Monitor Setups", "Remote Worker Reality", "Tech Clutter", "Cozy Coding Corner"],
        "photography_style": "Candid room photography, natural window light or warm desk lamp, deliberate clutter composition, authentic lived-in feel, 35mm lens",
        "ctr_potential": "Very High",
        "competition": "Low",
        "key_buyers": "Tech blogs, SaaS companies, remote work platforms, productivity apps",
        "primary_keywords": ["home office", "remote work", "gaming desk", "developer workspace", "tech setup", "messy desk", "work from home"],
        "icon": "fa-desktop",
        "color": "#3dd68c",
    },
    "Active Senior Lifestyle": {
        "description": "Non-cliché elderly imagery: seniors using VR, lifting weights, at music festivals, traveling. Massive gap in authentic senior lifestyle stock.",
        "sub_niches": ["Senior Tech Users", "Elderly Fitness", "Senior Travelers", "Active Grandparents", "Silver Economy", "Senior Musicians", "Elderly Gamers", "Fit Seniors Outdoors"],
        "photography_style": "Bright energetic lifestyle photography, golden hour outdoor light, authentic expressions, no staged stock-photo smiles, natural movement",
        "ctr_potential": "High",
        "competition": "Low",
        "key_buyers": "Healthcare companies, senior living brands, fitness brands, travel industry, financial planners",
        "primary_keywords": ["active senior", "elderly lifestyle", "senior fitness", "aging well", "silver economy", "senior technology", "healthy aging"],
        "icon": "fa-user-friends",
        "color": "#6c8ebf",
    },
    "Green Energy Infrastructure": {
        "description": "Emerging tech visuals: green hydrogen plants, EV charging stations, solar battery walls, wind farm aerials. Specialized visuals bought at premium prices.",
        "sub_niches": ["Solar Panel Installations", "EV Charging Stations", "Wind Farm Aerials", "Green Hydrogen Plants", "Residential Battery Storage", "Electric Vehicle Fleet", "Smart Grid Technology", "Offshore Wind"],
        "photography_style": "Clean industrial photography, blue-sky contrast with energy equipment, golden hour aerial drone shots, technical precision, wide-angle establishing shots",
        "ctr_potential": "Very High",
        "competition": "Medium",
        "key_buyers": "Energy companies, ESG investors, news media, government agencies, sustainability consultants",
        "primary_keywords": ["solar energy", "renewable energy", "EV charging", "green energy", "clean energy", "wind turbine", "sustainability", "electric vehicle"],
        "icon": "fa-leaf",
        "color": "#3dd68c",
    },
    "Neurodiversity & Inclusion": {
        "description": "Realistic depictions of neurodivergent individuals in professional and educational settings. High-intent search with very low competition.",
        "sub_niches": ["Sensory-Friendly Workspaces", "Autism in the Workplace", "ADHD Focus", "Inclusive Classrooms", "Therapy Sessions", "Quiet Spaces", "Learning Support", "Neurodiversity Awareness"],
        "photography_style": "Warm empathetic documentary photography, soft diffused lighting, uncluttered backgrounds, authentic expressions, diverse representation",
        "ctr_potential": "High",
        "competition": "Very Low",
        "key_buyers": "HR departments, education institutions, healthcare, advocacy organizations, accessibility brands",
        "primary_keywords": ["neurodiversity", "autism", "ADHD", "inclusive workplace", "sensory friendly", "special needs", "learning differences", "mental health inclusion"],
        "icon": "fa-brain",
        "color": "#b48bff",
    },
    "Surreal Silliness": {
        "description": "Photorealistic but logically impossible scenes. 2026 scroll-stopping trend with very high CTR for advertising creatives.",
        "sub_niches": ["Animals in Business Settings", "Floating Objects", "Gravity-Defying Concepts", "Cats as Executives", "Dogs at Computers", "Oversized Objects in Nature", "Impossible Color Combos", "Nostalgic Surrealism"],
        "photography_style": "Ultra-photorealistic rendering, shallow DOF for believability, realistic lighting on impossible subjects, hyperrealistic textures on fantasy elements, natural shadows",
        "ctr_potential": "Very High",
        "competition": "Medium",
        "key_buyers": "Advertising agencies, social media marketers, editorial illustration, viral content creators",
        "primary_keywords": ["surreal", "creative concept", "bizarre", "funny concept", "unusual", "creative photography", "whimsical", "photorealistic fantasy", "absurd"],
        "icon": "fa-magic",
        "color": "#ff9f40",
    },
    "B2B Logistics & Smart Warehousing": {
        "description": "Automated warehouses, drone delivery hubs, last-mile logistics, cold chain facilities. Highly specialized visuals with premium buyers.",
        "sub_niches": ["Automated Warehouse Robots", "Drone Delivery", "Last-Mile Delivery", "Cold Chain Logistics", "Smart Forklift Operations", "Parcel Sorting", "Logistics Control Room", "Supply Chain"],
        "photography_style": "Industrial technical photography, blue-white LED warehouse lighting, wide-angle shots of scale, clean modern industrial aesthetic",
        "ctr_potential": "High",
        "competition": "Low",
        "key_buyers": "Logistics companies, supply chain software, e-commerce platforms, warehouse automation firms",
        "primary_keywords": ["warehouse automation", "logistics", "drone delivery", "supply chain", "smart warehouse", "automated forklift", "last mile delivery", "cold chain"],
        "icon": "fa-truck",
        "color": "#4ecdc4",
    },
    "Mental Health & Wellness": {
        "description": "Beyond 'woman with salad': therapy sessions, digital detox, realistic burnout, mindfulness moments. Huge growing market with premium buyers.",
        "sub_niches": ["Online Therapy Sessions", "Digital Detox", "Workplace Burnout", "Meditation Practice", "Mental Health Day", "Anxiety Concepts", "Mindfulness Journaling", "Self-Care Routines"],
        "photography_style": "Warm intimate photography, soft natural window light, muted earth tones, authentic emotional expressions, clutter-free zen environments, gentle bokeh",
        "ctr_potential": "Very High",
        "competition": "Medium — high quality is scarce",
        "key_buyers": "Healthcare brands, wellness apps, therapy platforms, HR departments, insurance companies",
        "primary_keywords": ["mental health", "wellness", "therapy", "meditation", "self care", "stress relief", "mindfulness", "burnout", "anxiety"],
        "icon": "fa-heart",
        "color": "#ff6b9d",
    },
    "Hyper-Realistic Textures & Materials": {
        "description": "Background overlays for designers: sustainable fabrics, recycled materials, bioplastics. A large silent market among UI/UX and graphic designers.",
        "sub_niches": ["Recycled Paper Textures", "Bioplastic Surfaces", "Mycelium Leather", "Sustainable Fabrics", "Natural Stone Macro", "Concrete Textures", "Woven Textile Macro", "Eco Material Abstracts"],
        "photography_style": "Macro photography with extreme detail, controlled studio lighting, ultra-sharp focus throughout frame, color-accurate neutral rendering",
        "ctr_potential": "High",
        "competition": "Low",
        "key_buyers": "UI/UX designers, graphic designers, packaging designers, print-on-demand, motion graphics artists",
        "primary_keywords": ["texture", "background", "surface", "material", "pattern", "abstract", "sustainable material", "eco texture", "natural fiber", "recycled"],
        "icon": "fa-layer-group",
        "color": "#a8a8a8",
    },
    "Generative Design Mockups": {
        "description": "Blank canvases in realistic settings: billboards in cities, coffee cups, phone screens. Designers overlay their own work onto these templates.",
        "sub_niches": ["Outdoor Billboard Mockups", "Product Packaging Mockups", "Phone Screen Real Scene", "Coffee Cup Mockup", "Tote Bag Street", "Minimalist Interior Frame", "Storefront Window", "Blank Poster Urban Wall"],
        "photography_style": "Lifestyle product photography, natural environmental lighting, authentic settings, sharp surface area with soft natural background blur",
        "ctr_potential": "High",
        "competition": "Medium",
        "key_buyers": "Graphic designers, brand designers, marketing agencies, Etsy sellers, mockup sites",
        "primary_keywords": ["mockup", "blank", "template", "branding", "product mockup", "billboard mockup", "packaging design", "design resource"],
        "icon": "fa-file-image",
        "color": "#f7c59f",
    },
    "AI & Future Technology": {
        "description": "Non-cliché AI/tech visuals: human-robot collaboration, authentic data centers, AI in healthcare. Very high demand from tech media.",
        "sub_niches": ["AI Robot Assistants", "Neural Network Visualization", "Futuristic Data Centers", "Human-Robot Collaboration", "Machine Learning Concepts", "Quantum Computing", "Digital Twin", "Edge Computing"],
        "photography_style": "Cinematic tech aesthetic, cool blue with warm accent, volumetric lighting, clean modern interiors, physical meets digital composition, deep focus",
        "ctr_potential": "Very High",
        "competition": "High — stand out with authentic non-glowing approaches",
        "key_buyers": "Tech blogs, SaaS companies, AI companies, news media, enterprise software",
        "primary_keywords": ["artificial intelligence", "AI", "robot", "future technology", "machine learning", "automation", "digital transformation", "tech concept"],
        "icon": "fa-robot",
        "color": "#4dc5ff",
    },
    "Business & Startup Concepts": {
        "description": "Teamwork, leadership, growth charts, digital strategy. One of the highest-downloaded categories — differentiate with diversity and 2026 authenticity.",
        "sub_niches": ["Startup Team Meetings", "Growth Analytics", "Leadership Concepts", "Digital Marketing", "Business Strategy", "Innovation Brainstorming", "Diverse Team Collaboration", "Entrepreneur Portrait"],
        "photography_style": "Professional corporate lifestyle, bright modern office, natural window light with fill, authentic candid team moments, diverse authentic representation",
        "ctr_potential": "Very High",
        "competition": "Very High — differentiate with diversity and authenticity",
        "key_buyers": "Business magazines, consulting firms, HR platforms, LinkedIn marketing, finance blogs",
        "primary_keywords": ["business", "teamwork", "startup", "leadership", "corporate", "office", "success", "growth", "strategy", "professional team"],
        "icon": "fa-briefcase",
        "color": "#f0a444",
    },
    "Content Creator Economy": {
        "description": "Influencer marketing, creator desk setups, TikTok/YouTube production moments. Massive demand from the creator economy and marketing brands.",
        "sub_niches": ["Creator Desk Setup", "YouTuber Filming", "Social Media Influencer", "Podcast Studio", "TikTok Creator", "Brand Partnership", "Behind-the-Scenes Content", "Streaming Setup"],
        "photography_style": "Modern creator aesthetic, ring light as practical ambient, authentic behind-the-scenes feel, RGB accent light for gaming/creator setups",
        "ctr_potential": "Very High",
        "competition": "Medium",
        "key_buyers": "Social media platforms, creator economy startups, marketing agencies, content tools",
        "primary_keywords": ["content creator", "influencer", "YouTuber", "social media", "creator economy", "podcast", "streaming", "digital creator", "filming setup"],
        "icon": "fa-video",
        "color": "#ff4444",
    },
    "Remote Work & Digital Nomad": {
        "description": "Laptop lifestyle in unique locations, coworking spaces, authentic digital nomad travel moments. Post-2020 trend still extremely strong.",
        "sub_niches": ["Mountain Laptop Work", "Beach Remote Work", "Coworking Space", "Digital Nomad Travel", "Home Office Authentic", "Freelancer Coffee Shop", "Van Life Office", "International Nomad"],
        "photography_style": "Outdoor lifestyle photography, natural ambient light, authentic work-in-progress, travel-meets-work composition, warm approachable tone",
        "ctr_potential": "High",
        "competition": "High — differentiate with unique locations",
        "key_buyers": "Coworking spaces, remote work software, travel brands, freelance platforms",
        "primary_keywords": ["remote work", "digital nomad", "work from home", "freelancer", "laptop lifestyle", "home office", "flexible work", "coworking"],
        "icon": "fa-globe",
        "color": "#3dd68c",
    },
    "Healthcare & Medical Technology": {
        "description": "Doctor with tablet, telemedicine, wearable health, medical AI diagnostics. Extremely high RPM as healthcare companies pay premium rates.",
        "sub_niches": ["Telemedicine Video Calls", "Wearable Health Tech", "Hospital Technology", "Medical AI Diagnostics", "Mental Health Professional", "Physical Therapy", "Medical Research Lab", "Pharmacy Tech"],
        "photography_style": "Clean clinical photography, bright daylight-balanced studio lighting, professional medical environments, modern equipment, authoritative compositions",
        "ctr_potential": "High",
        "competition": "Medium",
        "key_buyers": "Hospitals, pharmaceutical companies, healthcare apps, health insurance, medical devices",
        "primary_keywords": ["healthcare", "medical", "doctor", "telemedicine", "health technology", "medical technology", "patient care", "medicine", "wearable"],
        "icon": "fa-stethoscope",
        "color": "#4ecdc4",
    },
    "Finance & Investment Concepts": {
        "description": "Cryptocurrency, digital banking, investment growth, financial freedom. High RPM from finance blogs and investment platforms.",
        "sub_niches": ["Cryptocurrency Concepts", "Stock Market Charts", "Real Estate Investment", "Personal Finance", "Digital Banking", "Financial Independence", "Retirement Planning", "Fintech Innovation"],
        "photography_style": "Premium luxury aesthetic, dark navy with gold accents or clean white with data elements, sharp professional lighting, wealth lifestyle",
        "ctr_potential": "Very High",
        "competition": "High — differentiate with non-cliché approaches",
        "key_buyers": "Investment platforms, fintech startups, financial advisors, banking apps, crypto exchanges",
        "primary_keywords": ["finance", "investment", "crypto", "stock market", "financial planning", "wealth", "banking", "economy", "money", "fintech"],
        "icon": "fa-chart-line",
        "color": "#f0a444",
    },
    "Education & E-Learning": {
        "description": "Online learning environments, digital classrooms, adult education. Massive demand from e-learning platforms and universities.",
        "sub_niches": ["Online Learning Platforms", "Digital Classroom", "Student Technology", "Homeschooling", "Corporate Training", "VR Education", "STEM Learning", "Adult Education"],
        "photography_style": "Bright approachable educational environments, warm daylight, authentic engaged students, diverse representation, modern technology integration",
        "ctr_potential": "High",
        "competition": "High",
        "key_buyers": "E-learning platforms, educational publishers, universities, corporate training, EdTech",
        "primary_keywords": ["education", "learning", "online learning", "e-learning", "student", "classroom", "study", "knowledge", "digital education"],
        "icon": "fa-graduation-cap",
        "color": "#b48bff",
    },
    "Abstract Backgrounds & Gradients": {
        "description": "Gradient backgrounds, geometric patterns, tech textures, abstract light leaks. One of the highest download-volume categories — silently dominating stock sales.",
        "sub_niches": ["Neon Gradient Backgrounds", "Geometric Abstract Patterns", "Light Leak Overlays", "Dark Luxury Backgrounds", "Pastel Color Gradients", "Technology Patterns", "Bokeh Abstract", "Minimal Line Art"],
        "photography_style": "Pure studio precision, color-accurate rendering, smooth gradients without compression artifacts, high-resolution base, zero noise on smooth areas",
        "ctr_potential": "High",
        "competition": "High — differentiate with unusual palettes and quality",
        "key_buyers": "UI/UX designers, social media marketers, template creators, motion graphics artists",
        "primary_keywords": ["background", "abstract", "gradient", "pattern", "texture", "design element", "wallpaper", "overlay", "digital background"],
        "icon": "fa-palette",
        "color": "#ff9f40",
    },
}

# ─── Adobe Stock job tracking ─────────────────────────────────────────────
adobe_stock_jobs: dict = {}
adobe_stock_lock = threading.Lock()
ADOBE_STOCK_OUTPUT_DIR = BASE_DIR / "output" / "Adobe Stock"


def _load_templates() -> dict:
    if TEMPLATES_FILE.exists():
        try:
            return json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("templates_data.json invalid; falling back to empty templates: %s", exc)
            return {}
    return {}


def _save_templates(data: dict) -> None:
    TEMPLATES_FILE.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# Gemini Generation Logic
# ═══════════════════════════════════════════════════════════════════════════

def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name.strip())
    return re.sub(r"\s+", " ", name)


def _estimate_words(transcript: str) -> int:
    if not transcript.strip():
        return 1200
    n = len(re.findall(r"\b\w+\b", transcript))
    return max(600, min(3000, n))


def _build_prompt(
    title: str,
    description: str,
    transcript: str,
    word_count: int,
    template_instructions: str,
    include_dialogue: bool,
    gen_image_prompts: bool,
    gen_video_prompts: bool,
    gen_i2v_prompts: bool,
    num_image_prompts: int = 0,
    num_video_prompts: int = 0,
    num_i2v_prompts: int = 0,
    image_style: str = "",
    video_style: str = "",
) -> str:
    desc = description.strip() or "[not provided]"
    trans = transcript.strip() or "[not provided]"

    # Calculate prompts per scene (estimate ~10 scenes)
    est_scenes = 10
    img_per_scene = max(1, round(num_image_prompts / est_scenes)) if num_image_prompts > 0 else 1
    vid_per_scene = max(1, round(num_video_prompts / est_scenes)) if num_video_prompts > 0 else 1
    i2v_per_scene = max(1, round(num_i2v_prompts / est_scenes)) if num_i2v_prompts > 0 else 1

    # Style anchors
    img_style_instruction = ""
    if image_style.strip():
        img_style_instruction = f"\n  MANDATORY IMAGE STYLE: \"{image_style.strip()}\" — Apply this exact visual style to EVERY image prompt without exception. Mention the style explicitly in each prompt."
    vid_style_instruction = ""
    if video_style.strip():
        vid_style_instruction = f"\n  MANDATORY VIDEO STYLE: \"{video_style.strip()}\" — Apply this exact visual/motion style to EVERY video prompt and every image-to-video prompt without exception. Mention the style explicitly in each prompt."

    prompt = f"""You are a world-class Hollywood creative director, master AI prompt engineer (Midjourney V6, Stable Diffusion XL, Flux Dev, Runway Gen-3 Alpha, Kling 1.5, Pika 2.0), and YouTube growth strategist who has scaled 50+ faceless channels past 1M subscribers. Your outputs must be production-studio quality — every image/video prompt deployable DIRECTLY to an AI generator without any editing. Think frame-by-frame like a cinematographer. Write copy like a viral marketer.

INPUTS:
- Title       : {title}
- Description : {desc}
- Transcript  : {trans}

YOUR TASK:
Generate a COMPLETE, production-ready asset pack for one YouTube video.
Return prompts as LISTS for each scene (image_prompts, video_prompts, image_to_video_prompts).

{'TOTAL IMAGE PROMPTS REQUESTED: ' + str(num_image_prompts) + ' (distribute ~' + str(img_per_scene) + ' per scene)' if num_image_prompts > 0 else ''}
{'TOTAL VIDEO PROMPTS REQUESTED: ' + str(num_video_prompts) + ' (distribute ~' + str(vid_per_scene) + ' per scene)' if num_video_prompts > 0 else ''}
{'TOTAL I2V PROMPTS REQUESTED: ' + str(num_i2v_prompts) + ' (distribute ~' + str(i2v_per_scene) + ' per scene)' if num_i2v_prompts > 0 else ''}

STEP-BY-STEP REQUIREMENTS:

STEP 1 — CHARACTER MASTER VARIABLE (CRITICAL FOR CONSISTENCY)
- If the video features ANY recurring character, person, or subject, write ONE IMMUTABLE, EXHAUSTIVE
  visual description that acts as a "character DNA fingerprint".
- This description MUST include ALL of the following:
  * Age range, gender, ethnicity, skin tone (exact shade)
  * Face: face shape, forehead proportions, eyebrow shape/thickness/color, eye shape/iris color/
    eyelash detail, nose bridge width/nostril shape/nose length, lip fullness/mouth width/lip color,
    jawline definition, chin shape, cheekbone prominence, any dimples/moles/scars/freckles
  * Hair: exact style, length, color, texture (curly/straight/wavy), parting, volume
  * Body: build (slim/athletic/stocky), height impression, posture
  * Clothing: EXACT outfit description with colors, fabrics, accessories
  * Art style anchor (if applicable)
- This EXACT description MUST appear WORD-FOR-WORD inside EVERY image prompt and EVERY video prompt.
  Do NOT paraphrase, abbreviate, or skip it — copy-paste the full master variable into each prompt.
- If no recurring character exists, set character_master_variable to null.

STEP 2 — SCRIPT & SCENE BREAKDOWN
- Write a complete voiceover script (~{word_count} words total, +/- 20%).
- Break into 8-16 sequential scenes (fewer if content is short).
- Each scene needs a short title + full narration text.
- If a transcript was provided, adapt & enhance it — never contradict it.
- Include ambient sound cues in brackets: [Soft Rain], [Footsteps], [Music Swells].
- The voiceover script must be CLEAN and ready for direct text-to-speech conversion.
  No stage directions, no "(pause)", no formatting markers — just pure narration text.
"""

    if include_dialogue:
        prompt += """
STEP 2b — DIALOGUE
- For EACH scene, write a "dialogue" field containing what the character says on-screen
  OR what the narrator speaks as background voice.
- The dialogue should match the storyline and feel natural.
- If it's narrator voice, prefix with "Narrator: ".
- If character dialogue, prefix with the character name.
"""
    else:
        prompt += "\n- Set dialogue to null for every scene.\n"

    if gen_image_prompts:
        prompt += f"""
STEP 3 — IMAGE PROMPTS ({img_per_scene} prompt{'s' if img_per_scene > 1 else ''} per scene in the image_prompts list)
- 60-150 words EACH. Focus on STATIC composition (no motion).
- {'Generate ' + str(img_per_scene) + ' different prompts per scene, each showing a DIFFERENT camera angle, composition, or perspective of the same scene moment.' if img_per_scene > 1 else 'Generate 1 image prompt per scene.'}
- Include in EVERY prompt: camera angle, composition rule (rule of thirds / golden spiral / symmetry),
  colour palette (name specific hex-adjacent tones like "deep teal #1a3a4a"), mood descriptor,
  ultra-detailed setting/environment with texture and material descriptions.
- PROFESSIONAL CAMERA SPECS — include in every image prompt:
  * Camera body: Sony A7R V / Canon EOS R5 / ARRI Alexa 35 (pick scene-appropriate)
  * Lens: scene-appropriate focal length e.g. "85mm f/1.4 prime" (portraits), "24mm f/2.8 ultra-wide"
    (establishing), "200mm f/2.8 telephoto" (compression), "50mm f/1.2" (natural)
  * Depth of field: "razor-thin bokeh background", "deep focus everything sharp", "medium DOF"
  * Color science: "Kodak Portra 400 film emulation", "ARRI LogC grade", "Fuji Velvia saturation"
- Include the Character Master Variable VERBATIM in every single image prompt.
- LIGHTING SETUP — specify for every prompt:
  * Key light: position (45° upper-left / overhead / rim), color temp in Kelvin (2700K warm / 5600K daylight / 8000K cool blue)
  * Fill light ratio: "1:2 soft fill", "1:4 dramatic shadows", "1:8 noir contrast"
  * Practical lights, lens flare, atmospheric haze, and shadow hardness
- ENVIRONMENT CONTINUITY: When returning to a location, repeat the EXACT environment description word-for-word.
- ASPECT RATIO: YouTube standard = 16:9 widescreen. Shorts/vertical = 9:16. Specify in each prompt.
- End EVERY prompt with quality tags: "masterpiece, best quality, ultra-detailed, 8K resolution, photorealistic,
  sharp focus, professional DSLR photography, cinematic lighting, volumetric rays, film grain,
  hyperrealistic skin texture, subsurface scattering, physically based rendering"{img_style_instruction}
"""
    else:
        prompt += "\n- Set image_prompts to an empty list [] for every scene.\n"

    if gen_video_prompts:
        prompt += f"""
STEP 4 — VIDEO PROMPTS ({vid_per_scene} prompt{'s' if vid_per_scene > 1 else ''} per scene in the video_prompts list)
- 60-150 words EACH. Optimised for Runway Gen-3 / Kling / Pika.
- {'Generate ' + str(vid_per_scene) + ' different prompts per scene, each with a DIFFERENT camera movement or subject motion variation.' if vid_per_scene > 1 else 'Generate 1 video prompt per scene.'}
- Include in EVERY prompt: camera MOVEMENT type (dolly in/out, pan left/right, tilt up/down,
  tracking shot, crane up/down, steadicam follow, handheld shake, orbital 360°, static locked-off).
- Include the Character Master Variable VERBATIM in every single video prompt.
- RUNWAY GEN-3 ALPHA / KLING 1.5 SYNTAX — use these motion descriptors in every prompt:
  * Camera tag: [SLOW DOLLY IN], [AERIAL CRANE UP], [STEADICAM FOLLOW], [STATIC SHOT], [HANDHELD SHAKE]
  * Motion magnitude: "subtle ambient motion" (still scenes), "moderate steady movement", "high-energy dynamic motion"
  * Duration feel: "2-3 second clip", "4-5 second clip", "6-8 second establishing shot"
  * Slow-motion: "shot at 120fps, played back at 24fps — cinematic slow motion"
  * Physics simulation: "realistic cloth simulation", "natural hair physics", "atmospheric particle drift",
    "water ripple dynamics", "fire volumetric simulation"
  * Motion quality suffix: "smooth motion blur, broadcast quality, professional cinematography,
    cinematic depth of field transition, physically accurate motion"
- MOTION CONSISTENCY: Character gestures, posture, and gait must be identical across all clips.
- SCENE TRANSITIONS: End each video prompt with a transition cue: "transitions via smooth dissolve to",
  "hard cut to next scene", "fade to black", "whip pan transition".{vid_style_instruction}
"""
    else:
        prompt += "\n- Set video_prompts to an empty list [] for every scene.\n"

    if include_dialogue and gen_video_prompts:
        prompt += """- IMPORTANT: Also embed the dialogue/narrator lines INTO the video prompt itself
  so the video generation tool knows what the character is saying during that clip.
"""

    if gen_i2v_prompts:
        prompt += f"""
STEP 5 — IMAGE-TO-VIDEO PROMPTS ({i2v_per_scene} prompt{'s' if i2v_per_scene > 1 else ''} per scene in the image_to_video_prompts list)
- For each scene, write prompts describing how to animate that scene's static image into video.
- {'Generate ' + str(i2v_per_scene) + ' different animation variations per scene image.' if i2v_per_scene > 1 else 'Generate 1 image-to-video prompt per scene.'}
- Include: transition type, motion intensity (subtle/moderate/dramatic), camera move direction,
  parallax depth, element-specific animation instructions.
- Optimised for Runway image-to-video or similar tools.{vid_style_instruction}
"""
    else:
        prompt += "\n- Set image_to_video_prompts to an empty list [] for every scene.\n"

    prompt += """
STEP 6 — YOUTUBE UPLOAD METADATA
- upload_title : SEO-optimised, emotional, curiosity-driven (60-100 chars).
- upload_description : hook lines, summary, timestamps placeholder, CTA, hashtags.
- tags     : 20-30 (mix broad + niche).
- keywords : 10-15 core SEO keywords.
- hashtags : 3-5 relevant (without #).

══════════════════════════════════════════════════════════════
CRITICAL QUALITY & CONSISTENCY RULES (MUST FOLLOW)
══════════════════════════════════════════════════════════════

1. CHARACTER CONSISTENCY (NON-NEGOTIABLE):
   - The Character Master Variable text MUST appear IDENTICALLY (word-for-word, no abbreviation)
     inside EVERY image prompt and EVERY video prompt.
   - Character facial features, body proportions, clothing, and accessories must NEVER change
     between scenes unless the story explicitly requires it.
   - Use identical phrasing for character description — do NOT rephrase or use synonyms.

2. STYLE CONSISTENCY:
   - ALL prompts across ALL scenes must use the SAME art style, rendering technique,
     color grading profile, and visual tone.
   - Define a STYLE ANCHOR phrase (e.g. "cinematic photorealistic, warm amber color grading,
     Kodak Portra 400 film emulation") and include it in EVERY prompt.

3. LIGHTING CONSISTENCY:
   - Maintain consistent lighting direction (e.g. 45-degree key light from upper-left),
     intensity, color temperature, and shadow quality within each scene.
   - When scenes share the same time-of-day or location, lighting MUST be identical.
   - Specify: key light position, fill light ratio, rim/back light, color temperature in Kelvin.

4. SCENE & ENVIRONMENT CONTINUITY:
   - Backgrounds, props, and environmental details MUST be consistent when returning to
     the same location. Describe environments with specific, repeatable details.
   - Weather, time-of-day cues, and atmospheric effects stay consistent within story segments.

5. FACIAL ACCURACY & DETAIL:
   - For EVERY character appearance, include micro-level facial details:
     eye shape, iris color, eyebrow arch, nose profile, lip shape, jawline, skin texture.
   - Facial proportions must remain mathematically consistent across all prompts.
   - Include expression cues appropriate to the scene emotion.

6. HIGH PRODUCTION QUALITY:
   - Every image prompt ends with: "masterpiece, best quality, ultra-detailed, 8K resolution,
     photorealistic, sharp focus, professional photography, cinematic composition"
   - Every video prompt includes motion quality tags: "smooth motion, professional cinematography,
     broadcast quality, cinematic depth of field"

7. NARRATIVE COHERENCE:
   - Prompts must tell the SAME visual story as the script — no contradictions.
   - Visual progression should match the emotional arc of the voiceover.

- No unsafe, hateful, explicit, or copyrighted content.
- Return ONLY valid JSON matching the provided schema — no markdown, no commentary.
"""

    if template_instructions.strip():
        prompt += f"\nSTYLE TEMPLATE (use as guidance, do NOT output placeholder tokens):\n{template_instructions}\n"

    return prompt


def _repair_json(raw_text: str) -> str:
    """Try to extract/repair JSON from a potentially malformed Gemini response."""
    text = raw_text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Find outermost { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text


def _validate_with_fallback(raw_text: str, attempt_label: str) -> GenerationResult:
    """Validate response, with fallback JSON repair on failure."""
    # First try direct parse
    try:
        return GenerationResult.model_validate_json(raw_text)
    except Exception as first_err:
        log.warning("Direct parse failed (%s): %s", attempt_label, str(first_err)[:200])
        log.debug("Raw response snippet (first 500 chars): %s", raw_text[:500])

    # Try JSON repair
    repaired = _repair_json(raw_text)
    try:
        return GenerationResult.model_validate_json(repaired)
    except Exception as repair_err:
        log.warning("JSON repair also failed (%s): %s", attempt_label, str(repair_err)[:200])

    # Try lenient: parse JSON dict, fill missing optional fields
    try:
        data = json.loads(repaired)
        if isinstance(data, dict):
            # Fix common issues: missing optional fields
            for scene in data.get("scenes", []):
                scene.setdefault("dialogue", None)
                scene.setdefault("image_prompts", [])
                scene.setdefault("video_prompts", [])
                scene.setdefault("image_to_video_prompts", [])
            if "upload_pack" not in data:
                data["upload_pack"] = {
                    "upload_title": data.get("video_concept_title", "Untitled"),
                    "upload_description": "",
                    "tags": [], "keywords": [], "hashtags": [],
                }
            data.setdefault("character_master_variable", None)
            data.setdefault("video_concept_title", "Untitled")
            result = GenerationResult.model_validate(data)
            log.info("Lenient parse succeeded (%s) — %d scenes", attempt_label, len(result.scenes))
            return result
    except Exception as lenient_err:
        log.error("All JSON parse strategies failed (%s): %s", attempt_label, str(lenient_err)[:300])

    # Save raw response for debugging
    try:
        err_file = LOG_DIR / f"failed_response_{attempt_label}_{int(time.time())}.txt"
        err_file.write_text(raw_text[:10000], encoding="utf-8")
        log.error("Saved failed response to %s", err_file)
    except Exception:
        pass

    raise ValueError(f"JSON validation failed after all repair attempts. Response starts with: {raw_text[:150]}")


def _call_gemini(prompt: str) -> GenerationResult:
    """Call Gemini with smart per-key quota tracking and automatic key failover.

    Strategy:
      1. Quota error  → mark key in cooldown, immediately try next key (zero sleep).
      2. All keys cooling → sleep only until the earliest key recovers.
      3. Non-quota error  → short base delay, then retry the full key pool.
      4. Repeats for MAX_RETRIES full cycles before giving up.
    """
    if not API_KEYS:
        raise RuntimeError("No Gemini API keys configured. Set GEMINI_API_KEYS or GEMINI_API_KEY in .env")

    last_error: Exception | None = None

    for cycle in range(MAX_RETRIES):
        for key_slot in range(len(API_KEYS)):
            # Only block when EVERY key is in quota cooldown
            wait_secs = _secs_until_any_key_available()
            if wait_secs > 0:
                log.info(
                    "[GEN] All %d keys in quota cooldown — waiting %.0fs for earliest recovery",
                    len(API_KEYS), wait_secs,
                )
                time.sleep(min(wait_secs + 1, RETRY_MAX_DELAY))

            api_key = _get_available_key()
            attempt_label = f"c{cycle + 1}_s{key_slot + 1}_{_mask_key(api_key)}"

            try:
                log.info(
                    "[GEN] Calling %s | key=%s | cycle %d/%d | keys_ok=%d/%d",
                    GEMINI_MODEL, _mask_key(api_key),
                    cycle + 1, MAX_RETRIES,
                    _count_available_keys(), len(API_KEYS),
                )

                t_call = time.time()
                client = genai.Client(api_key=api_key)
                resp = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": GenerationResult,
                        "temperature": 0.75,
                        "top_p": 0.95,
                        "max_output_tokens": 48192,
                    },
                )
                api_latency = round(time.time() - t_call, 1)

                raw_text = resp.text or ""
                log.debug("[GEN] Raw response: %d chars in %.1fs", len(raw_text), api_latency)

                if not raw_text.strip():
                    raise ValueError("Gemini returned empty response")

                result = _validate_with_fallback(raw_text, attempt_label)
                log.info(
                    "[GEN] SUCCESS — %d scenes | key=%s | cycle %d | latency=%.1fs",
                    len(result.scenes), _mask_key(api_key), cycle + 1, api_latency,
                )
                return result

            except Exception as exc:
                last_error = exc
                quota_hit = _is_quota_error(exc)
                is_validation = "validation" in str(exc).lower() or "json" in str(exc).lower()
                error_type = "QUOTA/429" if quota_hit else ("VALIDATION" if is_validation else "ERROR")

                log.warning(
                    "[GEN] FAILED | key=%s | type=%s | error=%s",
                    _mask_key(api_key), error_type, str(exc)[:300],
                )

                if quota_hit:
                    _mark_key_quota(api_key)   # put key in cooldown
                    continue                    # immediately try next key — no sleep
                else:
                    # Non-quota (network / validation): brief pause, keep trying
                    time.sleep(RETRY_DELAY)
                    continue

        # Full pass through all keys done
        if cycle < MAX_RETRIES - 1:
            log.info(
                "[GEN] Cycle %d/%d complete — pausing %ds before next cycle",
                cycle + 1, MAX_RETRIES, RETRY_DELAY,
            )
            time.sleep(RETRY_DELAY)

    log.error("[GEN] ALL %d CYCLES EXHAUSTED. Last error: %s", MAX_RETRIES, str(last_error)[:500])
    if last_error:
        raise last_error
    raise RuntimeError("Gemini generation failed without a captured error.")


# ═══════════════════════════════════════════════════════════════════════════
# Claude API Callers
# ═══════════════════════════════════════════════════════════════════════════

def _build_claude_json_system_prompt(schema_class) -> str:
    """Build a system prompt instructing Claude to return JSON matching a Pydantic schema."""
    schema_json = json.dumps(schema_class.model_json_schema(), indent=2)
    return (
        "You are a precise JSON generator. You MUST respond with ONLY valid JSON — "
        "no markdown fences, no commentary, no explanation. "
        "Your response must conform exactly to this JSON schema:\n\n"
        f"{schema_json}\n\n"
        "Return ONLY the JSON object."
    )


def _call_claude(prompt: str) -> GenerationResult:
    """Call Claude with smart per-key quota tracking and automatic key failover."""
    if not CLAUDE_API_KEYS:
        raise RuntimeError("No Claude API keys configured. Set CLAUDE_API_KEYS or CLAUDE_API_KEY in .env")

    system_prompt = _build_claude_json_system_prompt(GenerationResult)
    last_error: Exception | None = None

    for cycle in range(MAX_RETRIES):
        for key_slot in range(len(CLAUDE_API_KEYS)):
            wait_secs = _secs_until_any_claude_key_available()
            if wait_secs > 0:
                log.info("[CLAUDE-GEN] All %d keys in quota cooldown — waiting %.0fs", len(CLAUDE_API_KEYS), wait_secs)
                time.sleep(min(wait_secs + 1, RETRY_MAX_DELAY))

            api_key = _get_available_claude_key()
            attempt_label = f"claude_c{cycle + 1}_s{key_slot + 1}_{_mask_key(api_key)}"

            try:
                log.info(
                    "[CLAUDE-GEN] Calling %s | key=%s | cycle %d/%d | keys_ok=%d/%d",
                    CLAUDE_MODEL, _mask_key(api_key),
                    cycle + 1, MAX_RETRIES,
                    _count_available_claude_keys(), len(CLAUDE_API_KEYS),
                )

                t_call = time.time()
                client = anthropic.Anthropic(api_key=api_key)
                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    max_tokens=48192,
                    temperature=0.75,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    raw_text = stream.get_final_text()
                api_latency = round(time.time() - t_call, 1)
                log.debug("[CLAUDE-GEN] Raw response: %d chars in %.1fs", len(raw_text), api_latency)

                if not raw_text.strip():
                    raise ValueError("Claude returned empty response")

                result = _validate_with_fallback(raw_text, attempt_label)
                log.info(
                    "[CLAUDE-GEN] SUCCESS — %d scenes | key=%s | cycle %d | latency=%.1fs",
                    len(result.scenes), _mask_key(api_key), cycle + 1, api_latency,
                )
                return result

            except Exception as exc:
                last_error = exc
                quota_hit = _is_claude_quota_error(exc)
                is_validation = "validation" in str(exc).lower() or "json" in str(exc).lower()
                error_type = "QUOTA/429" if quota_hit else ("VALIDATION" if is_validation else "ERROR")

                log.warning("[CLAUDE-GEN] FAILED | key=%s | type=%s | error=%s",
                            _mask_key(api_key), error_type, str(exc)[:300])

                if quota_hit:
                    _mark_claude_key_quota(api_key)
                    continue
                else:
                    time.sleep(RETRY_DELAY)
                    continue

        if cycle < MAX_RETRIES - 1:
            log.info("[CLAUDE-GEN] Cycle %d/%d complete — pausing %ds", cycle + 1, MAX_RETRIES, RETRY_DELAY)
            time.sleep(RETRY_DELAY)

    log.error("[CLAUDE-GEN] ALL %d CYCLES EXHAUSTED. Last error: %s", MAX_RETRIES, str(last_error)[:500])
    if last_error:
        raise last_error
    raise RuntimeError("Claude generation failed without a captured error.")


def _call_claude_titles(prompt: str) -> NicheTitleResult:
    """Call Claude for viral title generation with key failover."""
    if not CLAUDE_API_KEYS:
        raise RuntimeError("No Claude API keys configured.")

    system_prompt = _build_claude_json_system_prompt(NicheTitleResult)
    last_error: Exception | None = None

    for cycle in range(MAX_RETRIES):
        for key_slot in range(len(CLAUDE_API_KEYS)):
            wait_secs = _secs_until_any_claude_key_available()
            if wait_secs > 0:
                log.info("[CLAUDE-TITLES] All keys cooling — waiting %.0fs", wait_secs)
                time.sleep(min(wait_secs + 1, RETRY_MAX_DELAY))

            api_key = _get_available_claude_key()
            try:
                log.info("[CLAUDE-TITLES] Calling %s | key=%s | cycle %d/%d",
                         CLAUDE_MODEL, _mask_key(api_key), cycle + 1, MAX_RETRIES)
                client = anthropic.Anthropic(api_key=api_key)
                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    max_tokens=8192,
                    temperature=0.92,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    raw_text = stream.get_final_text()
                if not raw_text.strip():
                    raise ValueError("Claude returned empty response for titles")

                try:
                    result = NicheTitleResult.model_validate_json(raw_text)
                except Exception:
                    repaired = _repair_json(raw_text)
                    try:
                        result = NicheTitleResult.model_validate_json(repaired)
                    except Exception:
                        data = json.loads(repaired)
                        result = NicheTitleResult.model_validate(data)

                log.info("[CLAUDE-TITLES] SUCCESS — %d titles | key=%s", len(result.titles), _mask_key(api_key))
                return result

            except Exception as exc:
                last_error = exc
                if _is_claude_quota_error(exc):
                    _mark_claude_key_quota(api_key)
                    continue
                else:
                    time.sleep(RETRY_DELAY)
                    continue

        if cycle < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)

    if last_error:
        raise last_error
    raise RuntimeError("Claude title generation failed.")


def _call_claude_adobe_stock(prompt: str) -> AdobeStockBatchResult:
    """Call Claude for Adobe Stock batch generation with key failover."""
    if not CLAUDE_API_KEYS:
        raise RuntimeError("No Claude API keys configured.")

    system_prompt = _build_claude_json_system_prompt(AdobeStockBatchResult)
    last_error: Exception | None = None

    for cycle in range(MAX_RETRIES):
        for _slot in range(len(CLAUDE_API_KEYS)):
            wait_secs = _secs_until_any_claude_key_available()
            if wait_secs > 0:
                log.info("[CLAUDE-ADOBE] All keys cooling — waiting %.0fs", wait_secs)
                time.sleep(min(wait_secs + 1, RETRY_MAX_DELAY))

            api_key = _get_available_claude_key()
            try:
                log.info("[CLAUDE-ADOBE] Calling %s | key=%s | cycle %d/%d",
                         CLAUDE_MODEL, _mask_key(api_key), cycle + 1, MAX_RETRIES)
                client = anthropic.Anthropic(api_key=api_key)
                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    max_tokens=48192,
                    temperature=0.78,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    raw_text = stream.get_final_text()
                if not raw_text.strip():
                    raise ValueError("Claude returned empty response for Adobe Stock batch")

                try:
                    result = AdobeStockBatchResult.model_validate_json(raw_text)
                except Exception:
                    repaired = _repair_json(raw_text)
                    try:
                        result = AdobeStockBatchResult.model_validate_json(repaired)
                    except Exception:
                        data = json.loads(repaired)
                        result = AdobeStockBatchResult.model_validate(data)

                log.info("[CLAUDE-ADOBE] SUCCESS — %d images | key=%s", len(result.images), _mask_key(api_key))
                return result

            except Exception as exc:
                last_error = exc
                if _is_claude_quota_error(exc):
                    _mark_claude_key_quota(api_key)
                    continue
                else:
                    time.sleep(RETRY_DELAY)
                    continue

        if cycle < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)

    if last_error:
        raise last_error
    raise RuntimeError("Claude Adobe Stock generation failed.")


# ═══════════════════════════════════════════════════════════════════════════
# Provider Dispatcher — routes to Gemini or Claude
# ═══════════════════════════════════════════════════════════════════════════

# Auto-detect default provider based on available keys
_active_provider = "claude" if (not API_KEYS and CLAUDE_API_KEYS) else "gemini"
_provider_lock = threading.Lock()


def _get_provider() -> str:
    with _provider_lock:
        return _active_provider


def _set_provider(provider: str) -> None:
    global _active_provider
    with _provider_lock:
        _active_provider = provider.lower() if provider else "gemini"


def _dispatch_generate(prompt: str, provider: str = None) -> GenerationResult:
    """Route to Gemini or Claude based on provider."""
    p = (provider or _get_provider()).lower()
    if p == "claude":
        return _call_claude(prompt)
    return _call_gemini(prompt)


def _dispatch_titles(prompt: str, provider: str = None) -> NicheTitleResult:
    p = (provider or _get_provider()).lower()
    if p == "claude":
        return _call_claude_titles(prompt)
    return _call_gemini_titles(prompt)


def _dispatch_adobe_stock(prompt: str, provider: str = None) -> AdobeStockBatchResult:
    p = (provider or _get_provider()).lower()
    if p == "claude":
        return _call_claude_adobe_stock(prompt)
    return _call_gemini_adobe_stock(prompt)


def _extract_template_instructions(template_name: Optional[str]) -> str:
    """Pull style instructions from a Prompting Funda template."""
    if not template_name:
        return ""
    templates = _load_templates()
    tpl = templates.get(template_name)
    if not tpl:
        return ""
    structure = tpl.get("structure") or {}
    parts = []
    for section in structure.values():
        file_contents = section.get("file_contents") or {}
        for text in file_contents.values():
            if isinstance(text, str) and text.strip():
                # Strip placeholder tokens so the model doesn't echo them
                clean = re.sub(r"\{[^}]+\}", "[...]", text.strip())
                parts.append(clean)
    return "\n\n".join(parts)


def _write_outputs(
    result: GenerationResult,
    folder_name: str,
    gen_image_prompts: bool,
    gen_video_prompts: bool,
    gen_i2v_prompts: bool,
    include_dialogue: bool,
) -> dict:
    """Write all output files. Returns dict with paths and content for the UI."""
    project = OUTPUT_DIR / _sanitize(folder_name)
    sep = "=" * 60

    created_files = {}

    # ── 1. Script (TTS-ready) ─────────────────────────────────────
    script_dir = project / "1. Script"
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / "script.txt"

    lines = []
    for s in result.scenes:
        lines.append(s.voiceover.strip())
        lines.append("")  # blank line between scenes
    script_text = "\n".join(lines).strip() + "\n"
    script_path.write_text(script_text, encoding="utf-8")
    created_files["script.txt"] = script_text

    # ── 2. Image Prompts ──────────────────────────────────────────
    if gen_image_prompts:
        img_dir = project / "2. Image Prompts"
        img_dir.mkdir(parents=True, exist_ok=True)
        img_path = img_dir / "images prompts.txt"
        lines = []
        if result.character_master_variable:
            lines += [sep, "CHARACTER MASTER VARIABLE", sep, result.character_master_variable, "", ""]
        prompt_num = 1
        for s in result.scenes:
            for p in s.image_prompts:
                if p.strip():
                    lines.append(f"{prompt_num}: {p.strip()}")
                    lines.append("")
                    lines.append("")
                    prompt_num += 1
        img_text = "\n".join(lines).strip() + "\n"
        img_path.write_text(img_text, encoding="utf-8")
        created_files["images prompts.txt"] = img_text

    # ── 3. Video Prompts ──────────────────────────────────────────
    if gen_video_prompts:
        vid_dir = project / "3. Video Prompts"
        vid_dir.mkdir(parents=True, exist_ok=True)
        vid_path = vid_dir / "videos prompts.txt"
        lines = []
        if result.character_master_variable:
            lines += [sep, "CHARACTER MASTER VARIABLE", sep, result.character_master_variable, "", ""]
        prompt_num = 1
        for s in result.scenes:
            for p in s.video_prompts:
                if p.strip():
                    prompt_text = p.strip()
                    if include_dialogue and s.dialogue and s.dialogue.strip():
                        prompt_text += f"\n[Dialogue: {s.dialogue.strip()}]"
                    lines.append(f"{prompt_num}: {prompt_text}")
                    lines.append("")
                    lines.append("")
                    prompt_num += 1
        vid_text = "\n".join(lines).strip() + "\n"
        vid_path.write_text(vid_text, encoding="utf-8")
        created_files["videos prompts.txt"] = vid_text

    # ── 4. Image-to-Video Prompts ─────────────────────────────────
    if gen_i2v_prompts:
        i2v_path = project / "image to video prompt.txt"
        lines = []
        prompt_num = 1
        for s in result.scenes:
            for p in s.image_to_video_prompts:
                if p.strip():
                    lines.append(f"{prompt_num}: {p.strip()}")
                    lines.append("")
                    lines.append("")
                    prompt_num += 1
        if lines:
            i2v_text = "\n".join(lines).strip() + "\n"
            i2v_path.write_text(i2v_text, encoding="utf-8")
            created_files["image to video prompt.txt"] = i2v_text

    # ── 5. Video Detail ───────────────────────────────────────────
    up = result.upload_pack
    detail_path = project / f"{_sanitize(folder_name)}_detail.txt"
    detail_lines = [
        sep, "VIDEO DETAIL", sep, "",
        f"Title: {up.upload_title}", "",
        "Description:", "-" * 40,
        up.upload_description.strip(), "",
        "Tags:", "-" * 40,
        ", ".join(t.strip() for t in up.tags if t.strip()), "",
        "Keywords:", "-" * 40,
        ", ".join(k.strip() for k in up.keywords if k.strip()), "",
        "Hashtags:", "-" * 40,
        " ".join(f"#{h.strip().lstrip('#')}" for h in up.hashtags if h.strip()),
    ]
    detail_text = "\n".join(detail_lines).strip() + "\n"
    detail_path.write_text(detail_text, encoding="utf-8")
    created_files[f"{_sanitize(folder_name)}_detail.txt"] = detail_text

    return {
        "project_path": str(project),
        "files": created_files,
        "scene_count": len(result.scenes),
        "title": result.video_concept_title,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Flask Routes — Pages
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    """Suppress the 404 favicon log noise."""
    return "", 204


@app.route("/api/init", methods=["GET"])
def api_init():
    """Combined init endpoint — returns templates, niche presets, Adobe Stock niches,
    and provider info in a single HTTP round-trip."""
    return jsonify({
        "success": True,
        "templates": _load_templates(),
        "niche_presets": NICHE_PRESETS,
        "adobe_stock_niches": ADOBE_STOCK_NICHES,
        "provider": _get_provider(),
        "available_providers": {
            "gemini": len(API_KEYS) > 0,
            "claude": len(CLAUDE_API_KEYS) > 0,
        },
        "provider_models": {
            "gemini": GEMINI_MODEL,
            "claude": CLAUDE_MODEL,
        },
    })


# ═══════════════════════════════════════════════════════════════════════════
# Flask Routes — Generation API
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/provider", methods=["GET"])
def api_get_provider():
    """Return current provider and available providers."""
    return jsonify({
        "success": True,
        "provider": _get_provider(),
        "available": {
            "gemini": len(API_KEYS) > 0,
            "claude": len(CLAUDE_API_KEYS) > 0,
        },
        "gemini_keys": len(API_KEYS),
        "claude_keys": len(CLAUDE_API_KEYS),
    })


@app.route("/api/provider", methods=["POST"])
def api_set_provider():
    """Set the active AI provider (gemini or claude)."""
    data = request.json or {}
    provider = (data.get("provider") or "").strip().lower()
    if provider not in ("gemini", "claude"):
        return jsonify({"success": False, "message": "Provider must be 'gemini' or 'claude'."})
    if provider == "gemini" and not API_KEYS:
        return jsonify({"success": False, "message": "No Gemini API keys configured in .env"})
    if provider == "claude" and not CLAUDE_API_KEYS:
        return jsonify({"success": False, "message": "No Claude API keys configured in .env"})
    _set_provider(provider)
    model = CLAUDE_MODEL if provider == "claude" else GEMINI_MODEL
    log.info("[PROVIDER] Switched to %s (%s)", provider.upper(), model)
    return jsonify({"success": True, "provider": provider, "model": model})


@app.route("/api/generate", methods=["POST"])
def api_generate():
    try:
        data = request.json or {}
        title = (data.get("title") or "").strip()
        folder = (data.get("folder") or "").strip()
        if not title or not folder:
            return jsonify({"success": False, "message": "Title and folder name are required."})

        description = (data.get("description") or "").strip()
        transcript = (data.get("transcript") or "").strip()
        template_name = (data.get("template") or "").strip() or None
        provider = (data.get("provider") or "").strip() or None
        include_dialogue = bool(data.get("include_dialogue", False))
        gen_image_prompts = bool(data.get("gen_image_prompts", True))
        gen_video_prompts = bool(data.get("gen_video_prompts", True))
        gen_i2v_prompts = bool(data.get("gen_i2v_prompts", False))
        num_image_prompts = int(data.get("num_image_prompts") or 0)
        num_video_prompts = int(data.get("num_video_prompts") or 0)
        num_i2v_prompts = int(data.get("num_i2v_prompts") or 0)
        image_style = (data.get("image_style") or "").strip()
        video_style = (data.get("video_style") or "").strip()

        template_instructions = _extract_template_instructions(template_name)
        wc = _estimate_words(transcript)

        prompt = _build_prompt(
            title=title,
            description=description,
            transcript=transcript,
            word_count=wc,
            template_instructions=template_instructions,
            include_dialogue=include_dialogue,
            gen_image_prompts=gen_image_prompts,
            gen_video_prompts=gen_video_prompts,
            gen_i2v_prompts=gen_i2v_prompts,
            num_image_prompts=num_image_prompts,
            num_video_prompts=num_video_prompts,
            num_i2v_prompts=num_i2v_prompts,
            image_style=image_style,
            video_style=video_style,
        )

        result = _dispatch_generate(prompt, provider=provider)

        output = _write_outputs(
            result=result,
            folder_name=folder,
            gen_image_prompts=gen_image_prompts,
            gen_video_prompts=gen_video_prompts,
            gen_i2v_prompts=gen_i2v_prompts,
            include_dialogue=include_dialogue,
        )

        return jsonify({"success": True, **output})

    except Exception as exc:
        log.error("Generation failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/projects", methods=["GET"])
def api_projects():
    """List all generated projects in /output."""
    projects = []
    if OUTPUT_DIR.exists():
        for p in sorted(OUTPUT_DIR.iterdir()):
            if p.is_dir():
                files = []
                for f in p.rglob("*.txt"):
                    files.append(str(f.relative_to(p)))
                projects.append({"name": p.name, "path": str(p), "files": files})
    return jsonify({"success": True, "projects": projects})


@app.route("/api/project/<name>/file", methods=["GET"])
def api_read_file(name):
    """Read a file from a project folder."""
    file_path = request.args.get("path", "")
    if not file_path:
        return jsonify({"success": False, "message": "File path required."})
    project_root = (OUTPUT_DIR / _sanitize(name)).resolve()
    full = (project_root / file_path).resolve()
    try:
        full.relative_to(project_root)
    except ValueError:
        return jsonify({"success": False, "message": "Invalid file path."})
    if not full.exists() or not full.is_file():
        return jsonify({"success": False, "message": "File not found."})
    try:
        content = full.read_text(encoding="utf-8")
    except Exception as exc:
        return jsonify({"success": False, "message": f"Failed to read file: {exc}"})
    return jsonify({"success": True, "content": content})


# ═══════════════════════════════════════════════════════════════════════════
# Flask Routes — Template Management API (from Prompting Funda)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/templates", methods=["GET"])
def get_templates():
    return jsonify({"success": True, "templates": _load_templates()})


@app.route("/api/templates/<name>", methods=["GET"])
def get_template(name):
    templates = _load_templates()
    if name in templates:
        return jsonify({"success": True, "template": templates[name]})
    return jsonify({"success": False, "message": "Template not found"})


@app.route("/api/templates", methods=["POST"])
def create_template():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    structure = data.get("structure", DEFAULT_TEMPLATE)
    if not name:
        return jsonify({"success": False, "message": "Template name is required"})
    templates = _load_templates()
    if name in templates:
        return jsonify({"success": False, "message": "Template already exists"})
    templates[name] = {"name": name, "structure": structure}
    _save_templates(templates)
    return jsonify({"success": True, "message": f'Template "{name}" created.'})


@app.route("/api/templates/<name>", methods=["PUT"])
def update_template(name):
    data = request.json or {}
    new_name = (data.get("name") or name).strip()
    structure = data.get("structure")
    templates = _load_templates()
    if name not in templates:
        return jsonify({"success": False, "message": "Template not found"})
    if new_name != name and new_name in templates:
        return jsonify({"success": False, "message": "Name already taken"})
    tpl = {"name": new_name, "structure": structure or templates[name]["structure"]}
    if new_name != name:
        del templates[name]
    templates[new_name] = tpl
    _save_templates(templates)
    return jsonify({"success": True, "message": f'Template "{new_name}" updated.'})


@app.route("/api/templates/<name>", methods=["DELETE"])
def delete_template(name):
    templates = _load_templates()
    if name not in templates:
        return jsonify({"success": False, "message": "Template not found"})
    del templates[name]
    _save_templates(templates)
    return jsonify({"success": True, "message": f'Template "{name}" deleted.'})


@app.route("/api/default-template", methods=["GET"])
def get_default_template():
    return jsonify({"success": True, "template": DEFAULT_TEMPLATE})


# Prompt Creator — keyword replacement
@app.route("/api/generate-prompt", methods=["POST"])
def generate_prompt():
    data = request.json
    content = data.get("content", "")
    replacements = data.get("replacements", {})
    for key, value in replacements.items():
        content = content.replace(f"{{{key}}}", value)
    return jsonify({"success": True, "result": content})


# Folder Creator — create project folders from template
@app.route("/api/create-folders", methods=["POST"])
def create_folders():
    data = request.json
    template_name = data.get("template_name")
    project_name = (data.get("project_name") or "").strip()
    custom_contents = data.get("file_contents", {})
    if not template_name or not project_name:
        return jsonify({"success": False, "message": "Template name and project name are required"})
    templates = _load_templates()
    if template_name not in templates:
        return jsonify({"success": False, "message": "Template not found"})

    niche_folder = OUTPUT_DIR / template_name
    project_folder = niche_folder / project_name
    if project_folder.exists():
        return jsonify({"success": False, "message": f'Project "{project_name}" already exists'})

    prefix = project_name.replace(" ", "_").lower()
    created_items = []
    try:
        for content_key, content in custom_contents.items():
            parts = content_key.split("/")
            if len(parts) != 2:
                continue
            folder_name, file_suffix = parts
            folder_path = project_folder / folder_name
            folder_path.mkdir(parents=True, exist_ok=True)
            file_name = f"{prefix}_{file_suffix}.txt"
            (folder_path / file_name).write_text(content, encoding="utf-8")
            entry = next((i for i in created_items if i["folder"] == folder_name), None)
            if not entry:
                entry = {"folder": folder_name, "files": []}
                created_items.append(entry)
            entry["files"].append(file_name)
        return jsonify({"success": True, "message": "Folders created!", "path": str(project_folder), "structure": created_items})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/get-existing/<template_name>")
def get_existing(template_name):
    niche = OUTPUT_DIR / template_name
    if niche.exists():
        folders = [f.name for f in niche.iterdir() if f.is_dir()]
        return jsonify({"success": True, "folders": folders})
    return jsonify({"success": True, "folders": []})


# ═══════════════════════════════════════════════════════════════════════════
# Flask Routes — Bulk Automation API
# ═══════════════════════════════════════════════════════════════════════════

def _parse_excel(file_bytes: bytes) -> list[dict]:
    """Parse an Excel file into a list of row dicts."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    # First row = headers
    raw_headers = [str(h).strip().lower().replace(" ", "_") if h else f"col_{i}" for i, h in enumerate(rows[0])]
    # Map common header names
    header_map = {
        "title": "title", "video_title": "title", "name": "title",
        "description": "description", "desc": "description", "video_description": "description",
        "transcript": "transcript", "script": "transcript", "voiceover": "transcript",
        "prompt_template": "prompt_template", "template": "prompt_template", "prompt": "prompt_template",
        "keywords": "keywords", "keyword": "keywords", "tags": "keywords",
        "folder": "folder", "folder_name": "folder", "project": "folder",
        "or_words": "or_words", "words": "or_words", "extra_words": "or_words",
    }
    headers = [header_map.get(h, h) for h in raw_headers]
    result = []
    for row in rows[1:]:
        if not any(row):  # skip empty rows
            continue
        entry = {}
        for i, val in enumerate(row):
            if i < len(headers):
                entry[headers[i]] = str(val).strip() if val is not None else ""
        # Ensure required fields
        if entry.get("title"):
            if not entry.get("folder"):
                entry["folder"] = _sanitize(entry["title"])
            result.append(entry)
    wb.close()
    return result


def _parse_json_file(file_bytes: bytes) -> list[dict]:
    """Parse a JSON file — supports array of objects or single object."""
    data = json.loads(file_bytes.decode("utf-8"))
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Could be {"items": [...]} or a single entry
        if "items" in data:
            items = data["items"]
        elif "videos" in data:
            items = data["videos"]
        elif "data" in data:
            items = data["data"]
        else:
            items = [data]
    else:
        items = []

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Normalize keys
        entry = {}
        for k, v in item.items():
            key = k.strip().lower().replace(" ", "_")
            key_map = {
                "video_title": "title", "name": "title",
                "desc": "description", "video_description": "description",
                "script": "transcript", "voiceover": "transcript",
                "template": "prompt_template", "prompt": "prompt_template",
                "keyword": "keywords", "tags": "keywords",
                "folder_name": "folder", "project": "folder",
                "words": "or_words", "extra_words": "or_words",
            }
            entry[key_map.get(key, key)] = str(v).strip() if v is not None else ""
        if entry.get("title"):
            if not entry.get("folder"):
                entry["folder"] = _sanitize(entry["title"])
            result.append(entry)
    return result


def _parse_txt_file(file_bytes: bytes) -> list[dict]:
    """Parse a TXT file — one title per line, or tab/pipe separated fields."""
    text = file_bytes.decode("utf-8")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    result = []

    # Detect format: if first line has tabs or pipes, it's structured
    if lines and ("\t" in lines[0] or "|" in lines[0]):
        sep = "\t" if "\t" in lines[0] else "|"
        header_parts = [h.strip().lower().replace(" ", "_") for h in lines[0].split(sep)]
        header_map = {
            "video_title": "title", "name": "title",
            "desc": "description", "video_description": "description",
            "script": "transcript", "voiceover": "transcript",
            "template": "prompt_template", "prompt": "prompt_template",
            "keyword": "keywords", "tags": "keywords",
            "folder_name": "folder", "project": "folder",
        }
        headers = [header_map.get(h, h) for h in header_parts]
        for line in lines[1:]:
            parts = [p.strip() for p in line.split(sep)]
            entry = {}
            for i, val in enumerate(parts):
                if i < len(headers):
                    entry[headers[i]] = val
            if entry.get("title"):
                if not entry.get("folder"):
                    entry["folder"] = _sanitize(entry["title"])
                result.append(entry)
    else:
        # Simple: one title per line
        for line in lines:
            result.append({
                "title": line,
                "folder": _sanitize(line),
                "description": "",
                "transcript": "",
            })
    return result


def _process_single_bulk_item(job_id: str, idx: int, item: dict, total: int, options: dict) -> None:
    """Process one bulk item — runs inside a ThreadPoolExecutor worker."""
    # Respect pause / cancel before starting
    while True:
        with bulk_lock:
            job = bulk_jobs.get(job_id)
            if not job or job["status"] == "cancelled":
                return
            if job["status"] != "paused":
                break
        time.sleep(0.5)

    title = item.get("title", "")
    folder = item.get("folder", _sanitize(title))
    description = item.get("description", "")
    transcript = item.get("transcript", "")
    prompt_template_name = item.get("prompt_template", "") or None
    or_words = item.get("or_words", "")

    if or_words:
        description = f"{description}\n\nAdditional keywords/context: {or_words}".strip()

    with bulk_lock:
        item["status"] = "processing"

    item_start_time = time.time()

    try:
        log.info("[BULK %s] ── Item %d/%d START ── '%s'", job_id, idx + 1, total, title)

        template_instructions = _extract_template_instructions(prompt_template_name)
        wc = _estimate_words(transcript)

        prompt = _build_prompt(
            title=title,
            description=description,
            transcript=transcript,
            word_count=wc,
            template_instructions=template_instructions,
            include_dialogue=options.get("include_dialogue", False),
            gen_image_prompts=options.get("gen_image_prompts", True),
            gen_video_prompts=options.get("gen_video_prompts", True),
            gen_i2v_prompts=options.get("gen_i2v_prompts", False),
            num_image_prompts=int(options.get("num_image_prompts") or 0),
            num_video_prompts=int(options.get("num_video_prompts") or 0),
            num_i2v_prompts=int(options.get("num_i2v_prompts") or 0),
            image_style=(options.get("image_style") or "").strip(),
            video_style=(options.get("video_style") or "").strip(),
        )

        log.debug("[BULK %s] Prompt length: %d chars for '%s'", job_id, len(prompt), title)

        result = _dispatch_generate(prompt, provider=options.get("provider"))

        output = _write_outputs(
            result=result,
            folder_name=folder,
            gen_image_prompts=options.get("gen_image_prompts", True),
            gen_video_prompts=options.get("gen_video_prompts", True),
            gen_i2v_prompts=options.get("gen_i2v_prompts", False),
            include_dialogue=options.get("include_dialogue", False),
        )

        elapsed = time.time() - item_start_time
        with bulk_lock:
            item["status"] = "completed"
            item["result"] = {
                "scene_count": output["scene_count"],
                "title": output["title"],
                "project_path": output["project_path"],
            }
            job = bulk_jobs.get(job_id)
            if job:
                job["completed_count"] = job.get("completed_count", 0) + 1
                job["progress"] = (job["completed_count"] + job.get("failed_count", 0)) / total * 100

        log.info(
            "[BULK %s] ── Item %d/%d DONE ── '%s' | %d scenes | %.1fs",
            job_id, idx + 1, total, title, output["scene_count"], elapsed,
        )

    except Exception as exc:
        elapsed = time.time() - item_start_time
        is_quota = _is_quota_error(exc)
        is_validation = "validation" in str(exc).lower() or "json" in str(exc).lower()
        error_type = "QUOTA/429" if is_quota else ("VALIDATION" if is_validation else "UNKNOWN")
        error_msg = str(exc)
        ui_error = f"{error_type}: {error_msg[:200]}"

        log.error(
            "[BULK %s] ── Item %d/%d FAILED ── '%s' | type=%s | %.1fs | error=%s",
            job_id, idx + 1, total, title, error_type, elapsed, error_msg[:500],
        )

        with bulk_lock:
            item["status"] = "failed"
            item["error"] = ui_error
            job = bulk_jobs.get(job_id)
            if job:
                job["failed_count"] = job.get("failed_count", 0) + 1
                job["progress"] = (job.get("completed_count", 0) + job["failed_count"]) / total * 100

    provider_used = options.get("provider", _get_provider()).lower()
    if provider_used == "claude" and idx < total - 1:
        log.info("[BULK %s] Item done. Pausing %ds for Claude limit before next run.", job_id, CLAUDE_BATCH_DELAY)
        time.sleep(CLAUDE_BATCH_DELAY)


def _process_bulk_job(job_id: str):
    """Orchestrates parallel bulk generation using a thread pool."""
    with bulk_lock:
        job = bulk_jobs.get(job_id)
        if not job:
            return

    items = job["items"]
    total = len(items)
    options = job["options"]
    provider = options.get("provider", _get_provider()).lower()
    
    # Restrict to 1 worker if using Claude to prevent quota exhaustion
    parallel_workers = 1 if provider == "claude" else BULK_PARALLEL_WORKERS

    log.info("[BULK %s] Starting parallel processing — %d items | workers=%d | provider=%s", 
             job_id, total, parallel_workers, provider)

    with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
        futures = []
        for idx, item in enumerate(items):
            # Stop submitting new tasks if cancelled
            with bulk_lock:
                j = bulk_jobs.get(job_id)
                if not j or j["status"] == "cancelled":
                    break
            future = executor.submit(_process_single_bulk_item, job_id, idx, item, total, options)
            futures.append(future)

        # Surface any unexpected worker-level exceptions
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                log.error("[BULK %s] Worker thread raised: %s", job_id, exc)

    with bulk_lock:
        job = bulk_jobs.get(job_id)
        if job and job["status"] not in ("cancelled",):
            job["status"] = "completed"
            job["progress"] = 100
            job["completed_at"] = datetime.now().isoformat()

    if job:
        completed = job.get("completed_count", 0)
        failed = job.get("failed_count", 0)
        log.info(
            "[BULK %s] ═══ JOB FINISHED ═══ completed=%d | failed=%d | total=%d",
            job_id, completed, failed, total,
        )


@app.route("/api/bulk/upload", methods=["POST"])
def bulk_upload():
    """Upload a file (Excel/JSON/TXT), parse it, return preview of items."""
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "message": "No file uploaded."})

        f = request.files["file"]
        if not f.filename:
            return jsonify({"success": False, "message": "Empty filename."})

        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        file_bytes = f.read()

        if ext in ("xlsx", "xls"):
            items = _parse_excel(file_bytes)
        elif ext == "json":
            items = _parse_json_file(file_bytes)
        elif ext in ("txt", "csv", "tsv"):
            items = _parse_txt_file(file_bytes)
        else:
            return jsonify({"success": False, "message": f"Unsupported file type: .{ext}. Use .xlsx, .json, or .txt"})

        if not items:
            return jsonify({"success": False, "message": "No valid entries found in the file. Make sure the first row has headers (title, description, etc)."})

        # Save upload for reference
        UPLOAD_DIR.mkdir(exist_ok=True)
        save_path = UPLOAD_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{f.filename}"
        save_path.write_bytes(file_bytes)

        return jsonify({
            "success": True,
            "filename": f.filename,
            "item_count": len(items),
            "items": items,
            "detected_fields": list(set().union(*[set(item.keys()) for item in items])) if items else [],
        })

    except Exception as exc:
        log.error("Bulk upload failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/bulk/start", methods=["POST"])
def bulk_start():
    """Start a bulk generation job."""
    try:
        data = request.json or {}
        items = data.get("items", [])
        if not items:
            return jsonify({"success": False, "message": "No items to process."})

        options = data.get("options", {})
        job_id = str(uuid.uuid4())[:8]

        # Initialize each item with status
        for item in items:
            item["status"] = "queued"
            item["error"] = None
            item["result"] = None

        job = {
            "id": job_id,
            "status": "running",
            "items": items,
            "options": options,
            "total_count": len(items),
            "completed_count": 0,
            "failed_count": 0,
            "current_index": 0,
            "progress": 0,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
        }

        with bulk_lock:
            bulk_jobs[job_id] = job

        # Start background thread
        thread = threading.Thread(target=_process_bulk_job, args=(job_id,), daemon=True)
        thread.start()

        log.info("Bulk job %s started with %d items", job_id, len(items))
        return jsonify({"success": True, "job_id": job_id, "total": len(items)})

    except Exception as exc:
        log.error("Bulk start failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/bulk/status/<job_id>", methods=["GET"])
def bulk_status(job_id):
    """Get current status of a bulk job."""
    with bulk_lock:
        job = bulk_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "message": "Job not found."})

    return jsonify({
        "success": True,
        "job": {
            "id": job["id"],
            "status": job["status"],
            "total_count": job["total_count"],
            "completed_count": job.get("completed_count", 0),
            "failed_count": job.get("failed_count", 0),
            "current_index": job.get("current_index", 0),
            "progress": job.get("progress", 0),
            "created_at": job["created_at"],
            "completed_at": job.get("completed_at"),
            "items": [
                {
                    "title": it.get("title", ""),
                    "folder": it.get("folder", ""),
                    "status": it.get("status", "queued"),
                    "error": it.get("error"),
                    "result": it.get("result"),
                }
                for it in job["items"]
            ],
        },
    })


@app.route("/api/bulk/events/<job_id>", methods=["GET"])
def bulk_events(job_id):
    """Server-Sent Events stream for real-time bulk job progress.
    Replaces the 2-second polling loop with push-based updates."""
    def generate():
        last_hash = ""
        last_heartbeat = time.time()
        while True:
            with bulk_lock:
                job = bulk_jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                return
            snapshot = {
                "id": job["id"],
                "status": job["status"],
                "total_count": job["total_count"],
                "completed_count": job.get("completed_count", 0),
                "failed_count": job.get("failed_count", 0),
                "current_index": job.get("current_index", 0),
                "progress": round(job.get("progress", 0), 1),
                "created_at": job["created_at"],
                "completed_at": job.get("completed_at"),
                "items": [
                    {
                        "title": it.get("title", ""),
                        "folder": it.get("folder", ""),
                        "status": it.get("status", "queued"),
                        "error": it.get("error"),
                        "result": it.get("result"),
                    }
                    for it in job["items"]
                ],
            }
            payload = json.dumps(snapshot)
            current_hash = hashlib.md5(payload.encode()).hexdigest()
            now = time.time()
            if current_hash != last_hash:
                last_hash = current_hash
                last_heartbeat = now
                yield f"data: {payload}\n\n"
            elif now - last_heartbeat > 15:
                # Keep-alive heartbeat so proxies don't close the connection
                last_heartbeat = now
                yield ": heartbeat\n\n"
            if job["status"] in ("completed", "cancelled"):
                return
            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/bulk/pause/<job_id>", methods=["POST"])
def bulk_pause(job_id):
    with bulk_lock:
        job = bulk_jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "message": "Job not found."})
        if job["status"] == "running":
            job["status"] = "paused"
            return jsonify({"success": True, "message": "Job paused."})
        return jsonify({"success": False, "message": f"Cannot pause job in '{job['status']}' state."})


@app.route("/api/bulk/resume/<job_id>", methods=["POST"])
def bulk_resume(job_id):
    with bulk_lock:
        job = bulk_jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "message": "Job not found."})
        if job["status"] == "paused":
            job["status"] = "running"
            return jsonify({"success": True, "message": "Job resumed."})
        return jsonify({"success": False, "message": f"Cannot resume job in '{job['status']}' state."})


@app.route("/api/bulk/cancel/<job_id>", methods=["POST"])
def bulk_cancel(job_id):
    with bulk_lock:
        job = bulk_jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "message": "Job not found."})
        if job["status"] in ("running", "paused"):
            job["status"] = "cancelled"
            return jsonify({"success": True, "message": "Job cancelled."})
        return jsonify({"success": False, "message": f"Cannot cancel job in '{job['status']}' state."})


@app.route("/api/bulk/retry/<job_id>", methods=["POST"])
def bulk_retry(job_id):
    """Retry failed items in a completed/cancelled job."""
    with bulk_lock:
        job = bulk_jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "message": "Job not found."})
        if job["status"] not in ("completed", "cancelled"):
            return jsonify({"success": False, "message": "Job must be completed or cancelled to retry."})

        # Gather failed items
        failed_items = [it for it in job["items"] if it.get("status") == "failed"]
        if not failed_items:
            return jsonify({"success": False, "message": "No failed items to retry."})

    # Create a new job with just the failed items
    retry_data = {
        "items": [{k: v for k, v in it.items() if k not in ("status", "error", "result")} for it in failed_items],
        "options": job["options"],
    }

    # Re-use the bulk_start logic
    with app.test_request_context("/api/bulk/start", method="POST", json=retry_data):
        # Manually call
        new_items = retry_data["items"]
        options = retry_data["options"]
        new_job_id = str(uuid.uuid4())[:8]

        for item in new_items:
            item["status"] = "queued"
            item["error"] = None
            item["result"] = None

        new_job = {
            "id": new_job_id,
            "status": "running",
            "items": new_items,
            "options": options,
            "total_count": len(new_items),
            "completed_count": 0,
            "failed_count": 0,
            "current_index": 0,
            "progress": 0,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
        }

        with bulk_lock:
            bulk_jobs[new_job_id] = new_job

        thread = threading.Thread(target=_process_bulk_job, args=(new_job_id,), daemon=True)
        thread.start()

        return jsonify({"success": True, "job_id": new_job_id, "total": len(new_items), "message": f"Retrying {len(new_items)} failed items."})


@app.route("/api/bulk/export/<job_id>", methods=["GET"])
def bulk_export(job_id):
    """Export all completed outputs from a bulk job as a ZIP."""
    with bulk_lock:
        job = bulk_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "message": "Job not found."})

    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in job["items"]:
            if item.get("status") != "completed" or not item.get("result"):
                continue
            project_path = Path(item["result"]["project_path"])
            if project_path.exists():
                for file_path in project_path.rglob("*"):
                    if file_path.is_file():
                        arcname = str(file_path.relative_to(OUTPUT_DIR))
                        zf.write(file_path, arcname)

    zip_buffer.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"bulk_output_{job_id}_{timestamp}.zip",
    )


@app.route("/api/bulk/jobs", methods=["GET"])
def bulk_list_jobs():
    """List all bulk jobs."""
    with bulk_lock:
        jobs = []
        for jid, job in bulk_jobs.items():
            jobs.append({
                "id": job["id"],
                "status": job["status"],
                "total_count": job["total_count"],
                "completed_count": job.get("completed_count", 0),
                "failed_count": job.get("failed_count", 0),
                "progress": job.get("progress", 0),
                "created_at": job["created_at"],
                "completed_at": job.get("completed_at"),
            })
    return jsonify({"success": True, "jobs": jobs})


@app.route("/api/bulk/sample", methods=["GET"])
def bulk_sample():
    """Return a sample Excel/JSON structure for users to follow."""
    sample = {
        "description": "Sample bulk upload format. Each object represents one video to generate.",
        "fields": {
            "title": "(Required) Video title",
            "description": "(Optional) Video description",
            "transcript": "(Optional) Existing transcript or script",
            "prompt_template": "(Optional) Name of a saved template to use",
            "keywords": "(Optional) SEO keywords",
            "folder": "(Optional) Output folder name — auto-generated from title if empty",
            "or_words": "(Optional) Additional context or keywords",
        },
        "example": [
            {
                "title": "10 Mind-Blowing Facts About Space",
                "description": "A fascinating journey through the cosmos",
                "transcript": "",
                "prompt_template": "",
                "keywords": "space, cosmos, facts, science",
                "folder": "space_facts_video",
            },
            {
                "title": "How Ancient Egyptians Built the Pyramids",
                "description": "The engineering marvels of the ancient world",
                "transcript": "",
                "prompt_template": "",
                "keywords": "egypt, pyramids, history, ancient",
                "folder": "pyramids_video",
            },
        ],
    }
    return jsonify({"success": True, "sample": sample})


@app.route("/api/logs", methods=["GET"])
def api_view_logs():
    """View recent generation logs from the browser."""
    lines = int(request.args.get("lines", 200))
    log_file = LOG_DIR / "generation.log"
    if not log_file.exists():
        return jsonify({"success": True, "content": "No logs yet.", "path": str(log_file)})
    try:
        all_lines = log_file.read_text(encoding="utf-8").splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return jsonify({
            "success": True,
            "content": "\n".join(tail),
            "total_lines": len(all_lines),
            "showing": len(tail),
            "path": str(log_file),
        })
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/logs/errors", methods=["GET"])
def api_view_error_logs():
    """View only ERROR and WARNING lines from logs."""
    log_file = LOG_DIR / "generation.log"
    if not log_file.exists():
        return jsonify({"success": True, "content": "No logs yet."})
    try:
        all_lines = log_file.read_text(encoding="utf-8").splitlines()
        error_lines = [l for l in all_lines if "| ERROR" in l or "| WARNING" in l or "FAILED" in l]
        tail = error_lines[-200:] if len(error_lines) > 200 else error_lines
        return jsonify({
            "success": True,
            "content": "\n".join(tail),
            "total_errors": len(error_lines),
            "showing": len(tail),
        })
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Niche Command Center — Title Generation & Pipeline
# ═══════════════════════════════════════════════════════════════════════════

def _build_title_prompt(niche: str, sub_topic: str = "", num_titles: int = 15, custom_instructions: str = "") -> str:
    sub = f"\nSub-topic focus: {sub_topic}" if sub_topic.strip() else ""
    preset_info = ""
    preset = NICHE_PRESETS.get(niche)
    if preset:
        preset_info = f"""\nNICHE PRESET INFO (use for context, NOT for output):
- Typical audience: {preset['target_audience']}
- Content tone: {preset['tone']}
- Popular sub-topics: {', '.join(preset['sub_topics'])}"""

    return f"""You are a world-class YouTube SEO strategist, viral content architect, and audience psychology expert
with 10+ years of experience growing faceless YouTube channels to millions of subscribers.

NICHE: {niche}{sub}{preset_info}
NUMBER OF TITLES: {num_titles}

GENERATE {num_titles} VIRAL YOUTUBE TITLE IDEAS for the "{niche}" niche.

TITLE ENGINEERING RULES (CRITICAL — FOLLOW PRECISELY):

1. Every title MUST be 60-100 characters for maximum YouTube visibility.

2. Use PROVEN viral title formulas — distribute these EVENLY across your titles:
   - Numbers + Power Word: "7 Dangerous Signs...", "10 Secrets That..."
   - Curiosity Gap: "What Nobody Tells You About...", "The Truth About..."
   - Fear/Warning: "Stop Doing This Before...", "Why You Should Never..."
   - Authority: "Psychologists Reveal...", "Scientists Discovered..."
   - Challenge: "99% of People Fail This...", "Only 1% Know..."
   - Urgency: "Before It's Too Late", "You Need to Know This NOW"
   - Secret/Hidden: "The Hidden Psychology of...", "Secret Signs of..."
   - Brackets for CTR: Use [Study], [Explained], [Warning], [Backed by Science]
   - Comparison: "vs", "The Difference Between..."
   - Story/Personal: "How I Discovered...", "The Day I Realized..."

3. Front-load the MOST compelling word in the first 3 words of each title.

4. Use power words: Secret, Hidden, Dangerous, Shocking, Ancient, Proven, Ultimate,
   Forbidden, Silent, Deadly, Powerful, Unstoppable, Dark, Brutal, Genius.

5. Create emotional tension — every title must make the viewer NEED to click.

6. Each title MUST target a DIFFERENT angle, sub-topic, or audience pain point.

7. Include at least 3 titles suitable for starting a series (Part 1, 2, 3 potential).

8. NO misleading clickbait — each title must be fulfillable with real, valuable content.

9. Optimise for BOTH YouTube search AND suggested/browse algorithm.

10. Think like a top YouTuber: what would MrBeast, Psych2Go, or Einzelganger title this?

ALSO PROVIDE:
- niche_analysis: Current trends, audience size estimate, competition level, content gaps,
  growth potential, best posting times.
- audience_profile: Demographics, psychographics, what they search for, pain points, desires.
- content_strategy_tips: 3-5 actionable tips specific to dominating this niche on YouTube.
  Include posting frequency, video length sweet spot, thumbnail psychology, retention hooks.

{custom_instructions}
Return ONLY valid JSON matching the provided schema — no markdown, no commentary."""


def _call_gemini_titles(prompt: str) -> NicheTitleResult:
    """Call Gemini for viral title generation with smart key failover and quota tracking."""
    if not API_KEYS:
        raise RuntimeError("No Gemini API keys configured.")

    last_error: Exception | None = None

    for cycle in range(MAX_RETRIES):
        for key_slot in range(len(API_KEYS)):
            wait_secs = _secs_until_any_key_available()
            if wait_secs > 0:
                log.info("[TITLES] All keys cooling — waiting %.0fs", wait_secs)
                time.sleep(min(wait_secs + 1, RETRY_MAX_DELAY))

            api_key = _get_available_key()
            try:
                log.info(
                    "[TITLES] Calling %s | key=%s | cycle %d/%d | keys_ok=%d/%d",
                    GEMINI_MODEL, _mask_key(api_key),
                    cycle + 1, MAX_RETRIES,
                    _count_available_keys(), len(API_KEYS),
                )
                client = genai.Client(api_key=api_key)
                resp = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": NicheTitleResult,
                        "temperature": 0.92,
                        "top_p": 0.97,
                        "max_output_tokens": 8192,
                    },
                )

                raw_text = resp.text or ""
                if not raw_text.strip():
                    raise ValueError("Gemini returned empty response for titles")

                try:
                    result = NicheTitleResult.model_validate_json(raw_text)
                except Exception:
                    repaired = _repair_json(raw_text)
                    try:
                        result = NicheTitleResult.model_validate_json(repaired)
                    except Exception:
                        data = json.loads(repaired)
                        result = NicheTitleResult.model_validate(data)

                log.info(
                    "[TITLES] SUCCESS — %d titles | key=%s | cycle %d",
                    len(result.titles), _mask_key(api_key), cycle + 1,
                )
                return result

            except Exception as exc:
                last_error = exc
                quota_hit = _is_quota_error(exc)
                error_type = "QUOTA/429" if quota_hit else "ERROR"

                log.warning(
                    "[TITLES] FAILED | key=%s | type=%s | error=%s",
                    _mask_key(api_key), error_type, str(exc)[:300],
                )

                if quota_hit:
                    _mark_key_quota(api_key)
                    continue
                else:
                    time.sleep(RETRY_DELAY)
                    continue

        if cycle < MAX_RETRIES - 1:
            log.info("[TITLES] Cycle %d/%d complete — pausing %ds", cycle + 1, MAX_RETRIES, RETRY_DELAY)
            time.sleep(RETRY_DELAY)

    if last_error:
        raise last_error
    raise RuntimeError("Title generation failed without a captured error.")


@app.route("/api/niche/presets", methods=["GET"])
def niche_presets_route():
    """Return all available niche presets."""
    return jsonify({"success": True, "presets": NICHE_PRESETS})


@app.route("/api/niche/generate-titles", methods=["POST"])
def niche_generate_titles():
    """Generate viral title ideas using selected AI provider."""
    try:
        data = request.json or {}
        niche = (data.get("niche") or "").strip()
        if not niche:
            return jsonify({"success": False, "message": "Niche is required."})

        sub_topic = (data.get("sub_topic") or "").strip()
        num_titles = max(5, min(30, int(data.get("num_titles") or 15)))
        provider = (data.get("provider") or "").strip() or None

        prompt = _build_title_prompt(niche, sub_topic, num_titles)
        result = _dispatch_titles(prompt, provider=provider)

        return jsonify({
            "success": True,
            "niche": result.niche,
            "niche_analysis": result.niche_analysis,
            "audience_profile": result.audience_profile,
            "titles": [t.model_dump() for t in result.titles],
            "content_strategy_tips": result.content_strategy_tips,
        })

    except Exception as exc:
        log.error("Title generation failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/niche/launch-pipeline", methods=["POST"])
def niche_launch_pipeline():
    """Launch full production pipeline from niche titles. Reuses bulk job infrastructure."""
    try:
        data = request.json or {}
        items = data.get("items", [])
        options = data.get("options", {})
        niche = (data.get("niche") or "").strip()

        if not items:
            return jsonify({"success": False, "message": "No items to process."})

        for item in items:
            if niche and not item.get("description"):
                item["description"] = f"Niche: {niche}"
            item["status"] = "queued"
            item["error"] = None
            item["result"] = None
            if not item.get("folder"):
                item["folder"] = _sanitize(item.get("title", "untitled"))

        job_id = str(uuid.uuid4())[:8]
        job = {
            "id": job_id,
            "status": "running",
            "items": items,
            "options": options,
            "total_count": len(items),
            "completed_count": 0,
            "failed_count": 0,
            "current_index": 0,
            "progress": 0,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
        }

        with bulk_lock:
            bulk_jobs[job_id] = job

        thread = threading.Thread(target=_process_bulk_job, args=(job_id,), daemon=True)
        thread.start()

        log.info("Niche pipeline %s started: %d videos for '%s'", job_id, len(items), niche)
        return jsonify({"success": True, "job_id": job_id, "total": len(items)})

    except Exception as exc:
        log.error("Pipeline launch failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "message": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Adobe Stock Pipeline — Generation Prompt Builder
# ═══════════════════════════════════════════════════════════════════════════

def _build_adobe_stock_prompt(
    niche: str,
    sub_niche: str,
    concepts_in_batch: int,
    variations_per_concept: int,
    concept_offset: int,
    total_concepts: int,
    include_analysis: bool,
    custom_instructions: str = "",
) -> str:
    """Build the master prompt for Adobe Stock image generation."""
    preset = ADOBE_STOCK_NICHES.get(niche, {})
    photo_style = preset.get("photography_style", "Professional commercial photography, authentic, photorealistic")
    key_buyers = preset.get("key_buyers", "Editorial and commercial buyers")
    primary_kw = ", ".join(preset.get("primary_keywords", []))
    sub_niches_list = ", ".join(preset.get("sub_niches", []))
    total_images = concepts_in_batch * variations_per_concept
    start_concept = concept_offset + 1
    end_concept = concept_offset + concepts_in_batch
    start_image = concept_offset * variations_per_concept + 1

    analysis_instruction = ""
    if include_analysis:
        analysis_instruction = """
ALSO GENERATE (first batch only):
- niche_analysis: 150-200 word 2026 Adobe Stock demand analysis covering: current demand level,
  top-selling sub-types, specific gaps in existing stock that AI can fill, RPM/CTR potential,
  seasonality patterns, key buyer segments and their budgets, 2026 trend relevance.
- monetization_tips: 4-6 highly specific, actionable tips to maximize acceptance rate AND
  revenue for THIS specific niche on Adobe Stock. Include: title formula, keyword strategy,
  batch upload cadence, technical specs that increase acceptance, and niche-specific buyer advice.
"""
    else:
        analysis_instruction = """
NOTE: This is a continuation batch. Set niche_analysis and monetization_tips to brief one-line
placeholders (they were already generated in batch 1).
"""

    variation_map = "\n".join([
        f"  - Variation 1: Wide establishing shot — capture the full environment and subject in context",
        f"  - Variation 2: Medium shot — subject fills 60-70% of frame, environment visible",
        f"  - Variation 3: Close-up detail — macro or tight portrait, isolate key element",
    ] + [f"  - Variation {i}: Alternative perspective — different angle, time of day, background, or emotional beat"
         for i in range(4, variations_per_concept + 1)])

    return f"""You are a master Adobe Stock photographer, commercial image director, and AI prompt engineer with:
- 10+ years generating best-selling stock content for Adobe, Getty, Shutterstock
- Expert knowledge of Adobe Stock's 2026 acceptance criteria and editorial standards
- Deep expertise in AI image generation (Midjourney V6, Adobe Firefly 3, DALL-E 3, Flux.1 Pro, Stable Diffusion XL)
- Understanding of what makes images sell: composition, authenticity, commercial relevance, CTR

══════════════════════════════════════════════════════════════
MISSION BRIEF
══════════════════════════════════════════════════════════════
NICHE: {niche}
SUB-NICHE FOCUS: {sub_niche if sub_niche else "General — cover diverse sub-niches within the main niche"}
PHOTOGRAPHY STYLE: {photo_style}
KEY BUYERS: {key_buyers}
PRIMARY KEYWORDS: {primary_kw}
AVAILABLE SUB-NICHES TO DRAW FROM: {sub_niches_list}

BATCH: Concepts {start_concept}–{end_concept} of {total_concepts} total
IMAGES IN THIS BATCH: {total_images} ({concepts_in_batch} concepts × {variations_per_concept} variations each)
IMAGE NUMBERING: Start at #{start_image}

══════════════════════════════════════════════════════════════
CRITICAL ADOBE STOCK REQUIREMENTS (NON-NEGOTIABLE)
══════════════════════════════════════════════════════════════
1. COMMERCIAL USE CLEARED: No logos, no brand names, no recognizable real people, no IP, no copyrighted designs, no trademarks, no flags in context that could cause issues.
2. AUTHENTICITY FIRST (2026 TREND): Market explicitly rejects "over-processed AI look". Every prompt must produce images that look like they were taken by a professional photographer, NOT generated by AI.
3. NO AI ARTIFACTS: Prompts must actively prevent extra fingers, floating limbs, distorted text, melting faces, unnatural body proportions. Use negative prompts aggressively.
4. DIVERSITY & INCLUSION: Ensure natural diversity in concepts involving people (age, ethnicity, body type) unless the concept specifically requires otherwise.
5. TECHNICAL QUALITY: Every image must be producible at 8K/4K resolution with no compression artifacts.

══════════════════════════════════════════════════════════════
MANDATORY 4K PHOTOGRAPHY SPECIFICATIONS (IN EVERY PROMPT)
══════════════════════════════════════════════════════════════
CAMERA BODY (choose most appropriate per scene type):
• Portraits/People: Sony A7R V, Canon EOS R5, Nikon Z9
• Architecture/Landscape: Hasselblad X2D 100C, Phase One IQ4 150MP
• Street/Documentary: Leica Q3, Fujifilm X-T5
• Industrial/Commercial: ARRI Alexa 35, RED Komodo 6K
• Product/Macro: Canon EOS R5 with 100mm macro

LENS SELECTION (choose per scene):
• Environmental portrait: 35mm f/1.4 — subject in context
• Character portrait: 85mm f/1.4 prime — flattering compression
• Macro/Detail: 100mm f/2.8 macro — ultra sharp textures
• Architectural: 24mm f/2.8 tilt-shift — perfect verticals
• Landscape: 16-35mm f/2.8 — maximum environment
• Editorial/Candid: 50mm f/1.2 — natural perspective

LIGHTING SPECIFICATIONS:
• Natural: "Golden hour, 2700K warm amber light, 45° upper-left key, long soft shadows, rim light catchlights"
• Overcast: "Overcast daylight 5600K, 1:1 flat fill ratio, soft feathered shadows, zero harsh highlights"
• Studio Corporate: "3-point studio setup: 5600K key softbox upper-left 45°, 1:4 fill ratio, white cyclorama background"
• Medical/Clean: "Clinical overhead fluorescent 4000K, shadowless fill, sterile clean environment"
• Moody: "Single 2700K window key light, 1:8 Rembrandt ratio, deep amber shadows, dust particle atmosphere"
• Outdoor Urban: "Mixed 5600K daylight + 2700K streetlamp practicals, blue hour balance, wet pavement reflections"

QUALITY SUFFIX (append to every prompt):
"masterpiece, ultra-detailed, 8K RAW resolution, professional commercial stock photography, photorealistic, sharp focus throughout subject plane, pristine image quality, natural film grain (ISO 400 simulation), subsurface scattering on skin, physically based rendering, licensed for commercial use, no watermarks"

AUTHENTICITY TAGS (include in every prompt with people):
"authentic natural expressions, asymmetric natural pose, real skin pores and texture, natural hair movement, subtle environmental imperfections, documentary photography feel"

══════════════════════════════════════════════════════════════
COMPOSITION RULES (specify one per image)
══════════════════════════════════════════════════════════════
• Rule of thirds: subject at intersection points, horizon on third line
• Golden ratio spiral: organic placement guiding eye through frame
• Symmetry: perfect architectural or product symmetry
• Leading lines: roads, corridors, cables drawing eye to subject
• Frame within frame: using architectural elements to frame subject
• Negative space: deliberate empty space for text overlay potential
• Foreground interest: sharp foreground element + subject + background blur

══════════════════════════════════════════════════════════════
VARIATION RULES (for {variations_per_concept} variations per concept)
══════════════════════════════════════════════════════════════
{variation_map}

══════════════════════════════════════════════════════════════
ADOBE STOCK TITLE FORMULA (CRITICAL FOR CTR)
══════════════════════════════════════════════════════════════
BAD: "Happy woman with technology" | "Business people" | "Green energy"
GOOD: "Smiling senior woman in VR headset laughing in bright Scandinavian living room"
GOOD: "Automated robotic arm sorting packages in modern e-commerce fulfillment center"
GOOD: "Diverse team of developers collaborating around laptop in sunlit coworking space"

Rules:
- Start with the PRIMARY SUBJECT (who/what)
- Include ACTION or STATE (doing/being)
- Include SETTING or CONTEXT (where/how)  
- 15-90 characters, lowercase except proper nouns
- Include 2-3 searchable keywords naturally
- NO generic words: beautiful, amazing, concept, background (unless literal)

══════════════════════════════════════════════════════════════
KEYWORD STRATEGY (35-50 keywords per image)
══════════════════════════════════════════════════════════════
Layer 1 — Primary subject (5-8 terms): The exact thing depicted
Layer 2 — Secondary elements (5-8 terms): What else is visible
Layer 3 — Setting & context (5-8 terms): Environment, location, time
Layer 4 — Mood & emotional tone (3-5 terms): Feeling the image conveys
Layer 5 — Industry/vertical (3-5 terms): Who would buy this
Layer 6 — Technical/style (3-5 terms): Photography style, quality indicators
Layer 7 — Commercial search terms (5-8 terms): What buyers actually type in Adobe Stock search

NEGATIVE PROMPT FORMULA:
Always include: "watermark, text overlay, logo, brand name, extra fingers, six fingers, extra limbs, floating hands, distorted face, crossed eyes, asymmetric pupils, blurry subject, motion blur on subject, jpeg artifacts, overexposed highlights, artificial HDR, tone mapping artifacts, plastic skin, artificial glow, neon rim light overuse, photoshop selection errors, uncanny valley"
Add concept-specific exclusions as needed.

══════════════════════════════════════════════════════════════
GENERATE THE FOLLOWING {total_images} IMAGES:
══════════════════════════════════════════════════════════════
{custom_instructions}
{analysis_instruction}
Return ONLY valid JSON matching the provided schema. No markdown. No commentary. No explanation.
Ensure every prompt is self-contained and directly usable in an AI image generator WITHOUT any editing."""


# ═══════════════════════════════════════════════════════════════════════════
# Adobe Stock — Gemini API Caller
# ═══════════════════════════════════════════════════════════════════════════

def _call_gemini_adobe_stock(prompt: str) -> AdobeStockBatchResult:
    """Call Gemini for Adobe Stock batch generation with key failover."""
    if not API_KEYS:
        raise RuntimeError("No Gemini API keys configured.")

    last_error: Exception | None = None

    for cycle in range(MAX_RETRIES):
        for _slot in range(len(API_KEYS)):
            wait_secs = _secs_until_any_key_available()
            if wait_secs > 0:
                log.info("[ADOBE] All keys cooling — waiting %.0fs", wait_secs)
                time.sleep(min(wait_secs + 1, RETRY_MAX_DELAY))

            api_key = _get_available_key()
            try:
                log.info(
                    "[ADOBE] Calling %s | key=%s | cycle %d/%d | keys_ok=%d/%d",
                    GEMINI_MODEL, _mask_key(api_key), cycle + 1, MAX_RETRIES,
                    _count_available_keys(), len(API_KEYS),
                )
                client = genai.Client(api_key=api_key)
                resp = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": AdobeStockBatchResult,
                        "temperature": 0.78,
                        "top_p": 0.95,
                        "max_output_tokens": 48192,
                    },
                )
                raw_text = resp.text or ""
                if not raw_text.strip():
                    raise ValueError("Gemini returned empty response for Adobe Stock batch")

                # Parse with repair fallback
                try:
                    result = AdobeStockBatchResult.model_validate_json(raw_text)
                except Exception:
                    repaired = _repair_json(raw_text)
                    try:
                        result = AdobeStockBatchResult.model_validate_json(repaired)
                    except Exception:
                        data = json.loads(repaired)
                        result = AdobeStockBatchResult.model_validate(data)

                log.info("[ADOBE] SUCCESS — %d images | key=%s | cycle %d", len(result.images), _mask_key(api_key), cycle + 1)
                return result

            except Exception as exc:
                last_error = exc
                quota_hit = _is_quota_error(exc)
                log.warning("[ADOBE] FAILED | key=%s | type=%s | error=%s",
                            _mask_key(api_key), "QUOTA/429" if quota_hit else "ERROR", str(exc)[:300])
                if quota_hit:
                    _mark_key_quota(api_key)
                    continue
                else:
                    time.sleep(RETRY_DELAY)
                    continue

        if cycle < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)

    if last_error:
        raise last_error
    raise RuntimeError("Adobe Stock generation failed.")


# ═══════════════════════════════════════════════════════════════════════════
# Adobe Stock — Output File Writers
# ═══════════════════════════════════════════════════════════════════════════

SEP80 = "═" * 80
SEP40 = "─" * 40

def _write_adobe_stock_prompts_file(
    images: list,
    batch_dir: Path,
    is_first_batch: bool,
    niche: str,
    sub_niche: str,
    total_concepts: int,
    variations: int,
    niche_analysis: str,
    monetization_tips: list,
) -> None:
    """Write/append to prompts.txt — the main generation-ready output."""
    prompts_path = batch_dir / "prompts.txt"
    mode = "w" if is_first_batch else "a"

    with prompts_path.open(mode, encoding="utf-8") as f:
        if is_first_batch:
            f.write(f"{SEP80}\n")
            f.write(f"  ADOBE STOCK IMAGE PROMPTS PIPELINE\n")
            f.write(f"  Niche   : {niche}\n")
            if sub_niche:
                f.write(f"  Focus   : {sub_niche}\n")
            f.write(f"  Plan    : {total_concepts} concepts × {variations} variations = {total_concepts * variations} images\n")
            f.write(f"  Created : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"{SEP80}\n\n")
            if niche_analysis:
                f.write(f"NICHE ANALYSIS (2026)\n{SEP40}\n{niche_analysis}\n\n")
            if monetization_tips:
                f.write(f"MONETIZATION TIPS\n{SEP40}\n")
                for i, tip in enumerate(monetization_tips, 1):
                    f.write(f"{i}. {tip}\n")
                f.write(f"\n{SEP80}\n\n")

        # Group images by concept
        current_concept = None
        for img in images:
            if img.concept_id != current_concept:
                current_concept = img.concept_id
                f.write(f"\n{'━' * 70}\n")
                f.write(f"  CONCEPT {img.concept_id}: {img.concept_name.upper()}\n")
                f.write(f"{'━' * 70}\n\n")

            var_label = getattr(img, "variation_label", f"Variation {img.variation_id}")
            f.write(f"IMAGE #{img.number}  —  {var_label}\n")
            f.write(f"TITLE: {img.title}\n\n")
            f.write(f"PROMPT:\n{img.prompt}\n\n")
            f.write(f"NEGATIVE PROMPT:\n{img.negative_prompt}\n\n")
            f.write(f"{SEP40}\n\n")


def _write_adobe_stock_keywords_file(
    images: list,
    batch_dir: Path,
    is_first_batch: bool,
) -> None:
    """Write/append to titles_keywords.txt — formatted for Adobe upload tools."""
    kw_path = batch_dir / "titles_keywords.txt"
    mode = "w" if is_first_batch else "a"

    with kw_path.open(mode, encoding="utf-8") as f:
        if is_first_batch:
            f.write("IMAGE_NUMBER\tTITLE\tKEYWORDS\n")
        for img in images:
            kw_str = ", ".join(k.strip().lower() for k in img.keywords if k.strip())
            # Sanitize title: no tabs or newlines
            safe_title = img.title.replace("\t", " ").replace("\n", " ").strip()
            f.write(f"{img.number}\t{safe_title}\t{kw_str}\n")


def _write_adobe_stock_summary(
    batch_dir: Path,
    niche: str,
    sub_niche: str,
    total_concepts: int,
    variations: int,
    niche_analysis: str,
    monetization_tips: list,
) -> None:
    """Write summary.txt — the strategy and upload checklist."""
    summary_path = batch_dir / "summary.txt"
    total = total_concepts * variations
    with summary_path.open("w", encoding="utf-8") as f:
        f.write(f"{SEP80}\n  ADOBE STOCK PIPELINE SUMMARY\n{SEP80}\n\n")
        f.write(f"Niche         : {niche}\n")
        if sub_niche:
            f.write(f"Sub-Niche     : {sub_niche}\n")
        f.write(f"Generated     : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Total Concepts: {total_concepts}\n")
        f.write(f"Variations    : {variations} per concept\n")
        f.write(f"Total Images  : {total}\n")
        f.write(f"Output Dir    : {batch_dir}\n\n")

        if niche_analysis:
            f.write(f"NICHE ANALYSIS\n{SEP40}\n{niche_analysis}\n\n")

        if monetization_tips:
            f.write(f"MONETIZATION TIPS\n{SEP40}\n")
            for i, tip in enumerate(monetization_tips, 1):
                f.write(f"{i}. {tip}\n")
            f.write("\n")

        f.write(f"ADOBE STOCK UPLOAD WORKFLOW\n{SEP40}\n")
        steps = [
            "Review prompts.txt — scan for any clearly unsuitable concepts before generation",
            "Generate images using your preferred AI tool (Midjourney /imagine, Adobe Firefly, DALL-E 3, Stable Diffusion with SDXL or Flux.1)",
            "Upscale ALL images to minimum 4MP (ideally 3840×2160 or higher). Use Topaz Gigapixel AI or Magnific AI",
            "Quality check EVERY image: extra fingers (fix in inpainting), distorted text (remove/replace), floating limbs",
            "Check for commercial clearance: no readable brand names, no recognizable faces, no copyrighted artwork visible",
            "Rename files systematically: [niche]_[concept]_[variation]_[date].jpg",
            "Use titles and keywords from titles_keywords.txt for each upload in Adobe Contributor Portal",
            "Submit in batches of 25-50. Adobe's review queue is faster for consistent batches",
            "Monitor acceptance rate. If < 70%, review the rejection reasons and adjust prompts",
            "Re-upload rejected images after fixing issues — do NOT delete and re-upload identical images",
        ]
        for i, step in enumerate(steps, 1):
            f.write(f"{i}. {step}\n")

        f.write(f"\nFILES IN THIS BATCH\n{SEP40}\n")
        f.write("prompts.txt           — All generation prompts (ready to paste into AI tools)\n")
        f.write("titles_keywords.txt   — Tab-separated: image_number | title | keywords (for upload)\n")
        f.write("summary.txt           — This file: strategy, analysis, upload workflow\n\n")
        f.write(f"{SEP80}\n")


# ═══════════════════════════════════════════════════════════════════════════
# Adobe Stock — Background Job Processor
# ═══════════════════════════════════════════════════════════════════════════

ADOBE_BATCH_SIZE_CONCEPTS = 5  # concepts per API call


def _process_adobe_stock_job(job_id: str) -> None:
    """Background thread: generates Adobe Stock prompts in batches and writes output files."""
    with adobe_stock_lock:
        job = adobe_stock_jobs.get(job_id)
        if not job:
            return

    niche = job["niche"]
    sub_niche = job["sub_niche"]
    total_concepts = job["total_concepts"]
    variations = job["variations"]
    custom_instructions = job.get("custom_instructions", "")
    batch_dir = Path(job["output_dir"])
    batch_dir.mkdir(parents=True, exist_ok=True)

    total_batches = math.ceil(total_concepts / ADOBE_BATCH_SIZE_CONCEPTS)
    first_batch_analysis = ""
    first_batch_tips: list = []
    summary_written = False

    log.info("[ADOBE %s] Started: niche=%s | concepts=%d | variations=%d | batches=%d",
             job_id, niche, total_concepts, variations, total_batches)

    for batch_idx in range(total_batches):
        # ── Pause / Cancel check ──────────────────────────────────────
        while True:
            with adobe_stock_lock:
                if job["status"] == "cancelled":
                    log.info("[ADOBE %s] Cancelled at batch %d", job_id, batch_idx + 1)
                    return
                if job["status"] != "paused":
                    break
            time.sleep(0.5)

        concept_offset = batch_idx * ADOBE_BATCH_SIZE_CONCEPTS
        concepts_in_batch = min(ADOBE_BATCH_SIZE_CONCEPTS, total_concepts - concept_offset)
        is_first = batch_idx == 0

        with adobe_stock_lock:
            job["current_batch"] = batch_idx + 1
            job["progress"] = batch_idx / total_batches * 100

        log.info("[ADOBE %s] Batch %d/%d — concepts %d–%d",
                 job_id, batch_idx + 1, total_batches,
                 concept_offset + 1, concept_offset + concepts_in_batch)

        try:
            prompt = _build_adobe_stock_prompt(
                niche=niche,
                sub_niche=sub_niche,
                concepts_in_batch=concepts_in_batch,
                variations_per_concept=variations,
                concept_offset=concept_offset,
                total_concepts=total_concepts,
                include_analysis=is_first,
                custom_instructions=custom_instructions,
            )
            result = _dispatch_adobe_stock(prompt, provider=job.get("provider"))

            if is_first:
                first_batch_analysis = result.niche_analysis or ""
                first_batch_tips = result.monetization_tips or []
                _write_adobe_stock_summary(
                    batch_dir, niche, sub_niche, total_concepts, variations,
                    first_batch_analysis, first_batch_tips,
                )
                summary_written = True

            _write_adobe_stock_prompts_file(
                images=result.images,
                batch_dir=batch_dir,
                is_first_batch=is_first,
                niche=niche,
                sub_niche=sub_niche,
                total_concepts=total_concepts,
                variations=variations,
                niche_analysis=first_batch_analysis,
                monetization_tips=first_batch_tips,
            )
            _write_adobe_stock_keywords_file(
                images=result.images,
                batch_dir=batch_dir,
                is_first_batch=is_first,
            )

            images_generated = len(result.images)
            with adobe_stock_lock:
                job["completed_batches"] += 1
                job["completed_images"] += images_generated
                job["progress"] = job["completed_batches"] / total_batches * 100
                job["batch_log"].append({
                    "batch": batch_idx + 1,
                    "status": "completed",
                    "concepts": concepts_in_batch,
                    "images": images_generated,
                    "range": f"concepts {concept_offset+1}–{concept_offset+concepts_in_batch}",
                })

            log.info("[ADOBE %s] Batch %d/%d done — %d images written", job_id, batch_idx + 1, total_batches, images_generated)

        except Exception as exc:
            log.error("[ADOBE %s] Batch %d/%d FAILED: %s", job_id, batch_idx + 1, total_batches, exc, exc_info=True)
            with adobe_stock_lock:
                job["failed_batches"] += 1
                job["batch_log"].append({
                    "batch": batch_idx + 1,
                    "status": "failed",
                    "error": str(exc)[:300],
                    "range": f"concepts {concept_offset+1}–{concept_offset+concepts_in_batch}",
                })

        if batch_idx < total_batches - 1:
            provider_used = job.get("provider", _get_provider()).lower()
            if provider_used == "claude":
                log.info("[ADOBE %s] Batch done. Pausing %ds for Claude limit...", job_id, CLAUDE_BATCH_DELAY)
                time.sleep(CLAUDE_BATCH_DELAY)
            else:
                time.sleep(BULK_ITEM_DELAY)

    # Write summary if never written (all batches failed)
    if not summary_written:
        _write_adobe_stock_summary(
            batch_dir, niche, sub_niche, total_concepts, variations, "", [],
        )

    with adobe_stock_lock:
        job["status"] = "completed"
        job["progress"] = 100.0
        job["completed_at"] = datetime.now().isoformat()

    log.info("[ADOBE %s] ═══ FINISHED ═══ images=%d | failed_batches=%d",
             job_id, job.get("completed_images", 0), job.get("failed_batches", 0))


# ═══════════════════════════════════════════════════════════════════════════
# Flask Routes — Adobe Stock Pipeline
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/adobe-stock/niches", methods=["GET"])
def adobe_stock_niches_route():
    """Return all Adobe Stock niche presets."""
    return jsonify({"success": True, "niches": ADOBE_STOCK_NICHES})


@app.route("/api/adobe-stock/start", methods=["POST"])
def adobe_stock_start():
    """Start an Adobe Stock image generation job."""
    try:
        data = request.json or {}
        niche = (data.get("niche") or "").strip()
        if not niche:
            return jsonify({"success": False, "message": "Niche is required."})

        sub_niche = (data.get("sub_niche") or "").strip()
        total_concepts = max(1, min(100, int(data.get("total_concepts") or 10)))
        variations = max(1, min(15, int(data.get("variations") or 5)))
        custom_instructions = (data.get("custom_instructions") or "").strip()
        provider = (data.get("provider") or "").strip() or None

        job_id = str(uuid.uuid4())[:8]
        batch_name = _sanitize(f"{niche}{(' - ' + sub_niche) if sub_niche else ''}")
        batch_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = ADOBE_STOCK_OUTPUT_DIR / _sanitize(niche) / f"{batch_name}_{batch_timestamp}"

        job = {
            "id": job_id,
            "type": "adobe_stock",
            "status": "running",
            "niche": niche,
            "sub_niche": sub_niche,
            "total_concepts": total_concepts,
            "variations": variations,
            "total_images": total_concepts * variations,
            "total_batches": math.ceil(total_concepts / ADOBE_BATCH_SIZE_CONCEPTS),
            "completed_batches": 0,
            "failed_batches": 0,
            "completed_images": 0,
            "current_batch": 0,
            "progress": 0.0,
            "output_dir": str(output_dir),
            "custom_instructions": custom_instructions,
            "provider": provider,
            "batch_log": [],
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
        }

        with adobe_stock_lock:
            adobe_stock_jobs[job_id] = job

        thread = threading.Thread(target=_process_adobe_stock_job, args=(job_id,), daemon=True)
        thread.start()

        log.info("Adobe Stock job %s started: niche=%s | %d concepts × %d variations",
                 job_id, niche, total_concepts, variations)
        return jsonify({
            "success": True,
            "job_id": job_id,
            "total_images": total_concepts * variations,
            "total_batches": math.ceil(total_concepts / ADOBE_BATCH_SIZE_CONCEPTS),
            "output_dir": str(output_dir),
        })

    except Exception as exc:
        log.error("Adobe Stock start failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/adobe-stock/status/<job_id>", methods=["GET"])
def adobe_stock_status(job_id):
    """Get current status of an Adobe Stock job."""
    with adobe_stock_lock:
        job = adobe_stock_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "message": "Job not found."})
    return jsonify({
        "success": True,
        "job": {
            "id": job["id"],
            "status": job["status"],
            "niche": job["niche"],
            "sub_niche": job["sub_niche"],
            "total_concepts": job["total_concepts"],
            "variations": job["variations"],
            "total_images": job["total_images"],
            "total_batches": job["total_batches"],
            "completed_batches": job.get("completed_batches", 0),
            "failed_batches": job.get("failed_batches", 0),
            "completed_images": job.get("completed_images", 0),
            "current_batch": job.get("current_batch", 0),
            "progress": round(job.get("progress", 0), 1),
            "output_dir": job.get("output_dir", ""),
            "batch_log": job.get("batch_log", []),
            "created_at": job["created_at"],
            "completed_at": job.get("completed_at"),
        },
    })


@app.route("/api/adobe-stock/events/<job_id>", methods=["GET"])
def adobe_stock_events(job_id):
    """Server-Sent Events stream for real-time Adobe Stock job progress."""
    def generate():
        last_hash = ""
        last_heartbeat = time.time()
        while True:
            with adobe_stock_lock:
                job = adobe_stock_jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                return
            snapshot = {
                "id": job["id"],
                "status": job["status"],
                "niche": job["niche"],
                "sub_niche": job["sub_niche"],
                "total_concepts": job["total_concepts"],
                "variations": job["variations"],
                "total_images": job["total_images"],
                "total_batches": job["total_batches"],
                "completed_batches": job.get("completed_batches", 0),
                "failed_batches": job.get("failed_batches", 0),
                "completed_images": job.get("completed_images", 0),
                "current_batch": job.get("current_batch", 0),
                "progress": round(job.get("progress", 0), 1),
                "output_dir": job.get("output_dir", ""),
                "batch_log": job.get("batch_log", []),
                "created_at": job["created_at"],
                "completed_at": job.get("completed_at"),
            }
            payload = json.dumps(snapshot)
            current_hash = hashlib.md5(payload.encode()).hexdigest()
            now = time.time()
            if current_hash != last_hash:
                last_hash = current_hash
                last_heartbeat = now
                yield f"data: {payload}\n\n"
            elif now - last_heartbeat > 15:
                last_heartbeat = now
                yield ": heartbeat\n\n"
            if job["status"] in ("completed", "cancelled"):
                return
            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/adobe-stock/pause/<job_id>", methods=["POST"])
def adobe_stock_pause(job_id):
    with adobe_stock_lock:
        job = adobe_stock_jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "message": "Job not found."})
        if job["status"] == "running":
            job["status"] = "paused"
            return jsonify({"success": True, "message": "Paused."})
        return jsonify({"success": False, "message": f"Cannot pause in '{job['status']}' state."})


@app.route("/api/adobe-stock/resume/<job_id>", methods=["POST"])
def adobe_stock_resume(job_id):
    with adobe_stock_lock:
        job = adobe_stock_jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "message": "Job not found."})
        if job["status"] == "paused":
            job["status"] = "running"
            return jsonify({"success": True, "message": "Resumed."})
        return jsonify({"success": False, "message": f"Cannot resume in '{job['status']}' state."})


@app.route("/api/adobe-stock/cancel/<job_id>", methods=["POST"])
def adobe_stock_cancel(job_id):
    with adobe_stock_lock:
        job = adobe_stock_jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "message": "Job not found."})
        if job["status"] in ("running", "paused"):
            job["status"] = "cancelled"
            return jsonify({"success": True, "message": "Cancelled."})
        return jsonify({"success": False, "message": f"Cannot cancel in '{job['status']}' state."})


@app.route("/api/adobe-stock/jobs", methods=["GET"])
def adobe_stock_list_jobs():
    """List all Adobe Stock jobs."""
    with adobe_stock_lock:
        jobs = [
            {
                "id": j["id"],
                "status": j["status"],
                "niche": j["niche"],
                "sub_niche": j["sub_niche"],
                "total_images": j["total_images"],
                "completed_images": j.get("completed_images", 0),
                "progress": round(j.get("progress", 0), 1),
                "created_at": j["created_at"],
                "completed_at": j.get("completed_at"),
                "output_dir": j.get("output_dir", ""),
            }
            for j in adobe_stock_jobs.values()
        ]
    return jsonify({"success": True, "jobs": jobs})


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    ADOBE_STOCK_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not TEMPLATES_FILE.exists():
        _save_templates({})
    log.info("Starting YouTube Automation Tool on http://localhost:5000")
    app.run(debug=True, port=5000, use_reloader=False)
