# YT Automation Studio — Full Analysis Report
**Analyzed on:** March 7, 2026  
**Codebase:** Flask + Gemini 2.5 Flash + Single-Page App (6 core files, ~2900 lines Python, ~5000+ lines HTML/JS/CSS)

---

## PART 1 — WHAT THE TOOL CURRENTLY DOES

### Core Architecture
- **Backend:** Flask Python server (`app.py`) — all logic, Gemini API calls, file I/O
- **Frontend:** Single-page app in one `index.html` — sidebar navigation, no page reloads
- **AI Engine:** Google Gemini 2.5 Flash with structured JSON output (Pydantic schemas)
- **Key Features:** Multi-key rotation with quota/429 handling, Server-Sent Events (SSE) for real-time progress, parallel thread pools for bulk jobs

---

### MODULE 1 — Generate (Single Video)
**What it does:** Takes a title + optional description/transcript → calls Gemini → writes a complete asset pack

**Inputs:**
- Video title (required)
- Description, Transcript (optional)
- Prompt Template (optional — pulls style instructions from saved templates)
- Output folder name
- Image style / Video style (free-text style overrides)

**Output files created inside `output/<folder>/`:**
```
1. Script/script.txt                    ← TTS-ready voiceover (no stage directions)
2. Image Prompts/images prompts.txt     ← Numbered AI image generation prompts
3. Video Prompts/videos prompts.txt     ← Numbered AI video generation prompts
image to video prompt.txt               ← Runway/Kling image-to-video prompts
<folder>_detail.txt                     ← YouTube title, description, tags, keywords, hashtags
```

**Generation Schema (Pydantic):**
- `GenerationResult` → `character_master_variable`, `scenes[]`, `upload_pack`
- Each `Scene` → `voiceover`, `dialogue?`, `image_prompts[]`, `video_prompts[]`, `image_to_video_prompts[]`
- `UploadPack` → `upload_title`, `upload_description`, `tags`, `keywords`, `hashtags`

**Options toggles:** Include dialogue, Image prompts on/off, Video prompts on/off, Image-to-video on/off, custom prompt counts (0–200)

---

### MODULE 2 — Niche Hub
**What it does:** Generates viral YouTube titles for a chosen niche using psychological hook formulas → can launch those titles directly into bulk production

**10 Built-in YouTube Niche Presets** (each has image_style, video_style, sub_topics, tone, target_audience):
1. Dark Psychology
2. Stoicism & Philosophy
3. True Crime & Mystery
4. Horror & Creepypasta
5. Personal Finance & Wealth
6. Space & Cosmos
7. History & Civilizations
8. Motivation & Self-Improvement
9. Relationship Psychology
10. Conspiracy & Hidden Knowledge

**Generated output per niche analysis:**
- 15 viral titles (with: hook_angle, why_it_works, target_emotion, content_brief per title)
- niche_analysis (audience size, trends, competition, content gaps)
- audience_profile (demographics + psychographics)
- 5 content_strategy_tips

**Pipeline button:** Select titles → launches them as a bulk job

---

### MODULE 3 — Bulk Automation
**What it does:** Upload a file of video titles → generate complete asset packs for all of them in parallel

**Accepted upload formats:** `.xlsx`, `.xls`, `.json`, `.txt`, `.csv`, `.tsv`

**Supported columns:** `title`, `description`, `transcript`, `prompt_template`, `keywords`, `folder`, `or_words`

**Job Management:**
- Up to 3 parallel worker threads
- Real-time SSE progress stream (push-based, not polling)
- Pause / Resume / Cancel / Retry failed items
- Export all completed outputs as a single `.zip`
- Job history tracked in memory (lost on server restart)

---

### MODULE 4 — Adobe Stock Pipeline
**What it does:** Generates batches of stock image prompts (prompt + negative prompt + title + 35–50 keywords) for Adobe Stock submission

**17 Built-in Adobe Stock Niche Presets:**
Hyper-Local Culture & Food, Authentic Messy Tech Workspaces, Active Senior Lifestyle, Green Energy Infrastructure, Neurodiversity & Inclusion, Surreal Silliness, B2B Logistics & Smart Warehousing, Mental Health & Wellness, Hyper-Realistic Textures & Materials, Generative Design Mockups, AI & Future Technology, Business & Startup Concepts, Content Creator Economy, Remote Work & Digital Nomad, Healthcare & Medical Technology, Finance & Investment Concepts, Abstract Backgrounds & Gradients + more

**Configuration per job:** niche, sub-niche, total concepts (1–100), variations per concept (1–15), custom instructions

**Output files per batch:**
```
output/Adobe Stock/<niche>/<niche_timestamp>/
  prompts.txt           ← All AI generation prompts, grouped by concept
  titles_keywords.txt   ← Tab-separated: image_number | title | keywords (for upload tool)
  summary.txt           ← Strategy, niche analysis, upload workflow checklist
```

**Processing:** Generates 5 concepts per API call, appends each batch to files. SSE progress.

---

### MODULE 5 — Projects
Lists all previously generated project folders in `output/`. Click a project → browse and read files in the browser. No download/delete from UI.

---

### MODULE 6 — Templates
**What it does:** Manage reusable prompt templates with a folder/file structure

Templates are stored in `templates_data.json` (currently empty `{}`).

**Default structure shape:**
```
Voice_Over/
  voiceover_script
Images/
  hook_images
  actual_images
Videos/
  hook_video
  actual_video
```

Templates are used in two ways:
1. In the **Generate** page: inject template file contents as style instructions into the Gemini prompt
2. In the **Folder Creator**: create the physical folder structure on disk for a project

---

### MODULE 7 — Prompt Creator
Select a template → pick a folder → pick a file → replace `{keyword}` placeholders with custom values → copy the final prompt.

---

### MODULE 8 — Folder Creator
Select a template + enter a project name → creates the physical folder hierarchy inside `output/<template_name>/<project_name>/` with text files pre-filled.

---

### MODULE 9 — Logs
View last 200 lines of `logs/generation.log` or filtered ERROR/WARNING lines in the browser.

---

## PART 2 — PROBLEMS & GAPS IDENTIFIED

### CRITICAL PROBLEM: Niche/Template Disconnect
The biggest structural gap: **Niche presets, Templates, Folder Creator, and Generate are four separate isolated systems that don't talk to each other.**

- The Niche Hub knows the image_style and video_style for "Dark Psychology" — but when you go to Generate a Dark Psychology video, you have to manually type those styles in.
- Templates have folder structures but no niche affinity — you can't say "this template is for Dark Psychology".
- The Folder Creator creates folders from a template, but the generated content from the Generate page goes to a completely different location (`output/<folder>/`) with no niche grouping.
- A "Space & Cosmos" video and a "Horror" video land in the same `output/` root — no separation.

---

### PROBLEM: No Niche-Organized Output Folder Hierarchy
All generated video projects go into one flat `output/` directory:
```
output/
  my-space-video/
  horror-story-1/
  dark-psychology-video/   ← all mixed together
  my-finance-channel/
```

There is NO per-niche, per-channel, or per-template subfolder organization. With bulk generation producing 20-50 projects, this becomes unmanageable.

---

### PROBLEM: Niche Styles Not Auto-Applied
When a niche preset is selected in Niche Hub, its `image_style` and `video_style` are NOT automatically passed to the generation pipeline. Users must manually copy-paste them into the Generate form.

---

### PROBLEM: In-Memory Job Storage
All bulk jobs and Adobe Stock jobs are stored in Python dictionaries in RAM — `bulk_jobs {}` and `adobe_stock_jobs {}`. Restarting the server wipes all job history, progress, and output references.

---

### PROBLEM: Templates System is Under-Used
`templates_data.json` is empty `{}` by default. There are no starter templates pre-loaded for any of the 10 YouTube niches or 17 Adobe Stock niches. A first-time user sees an empty list with no guidance.

---

### PROBLEM: Adobe Stock Output Not Downloadable from UI
The prompts/keywords files are written to disk, but there's no download button in the Adobe Stock UI. Users must navigate the filesystem manually.

---

### PROBLEM: Short-Form Content Missing
Every output format is optimized for long-form YouTube (8–16 scenes, 1200+ words). There's no YouTube Shorts / TikTok / Reels mode (single scene, 30–60 second vertical format).

---

### PROBLEM: No Thumbnail Generation
YouTube thumbnail prompts are the most important single asset for CTR — yet they're not generated. There's no dedicated thumbnail prompt with text overlay area, face close-up, color contrast specifications.

---

### PROBLEM: No YouTube Chapter Timestamps
The upload_description mentions "timestamps placeholder" but the system doesn't calculate actual timestamps from scene voiceover word counts. Channels with chapters get significantly better engagement.

---

### PROBLEM: No CSV Export for YouTube Upload
YouTube Studio allows bulk CSV upload of video metadata. The tool generates title/description/tags in a `.txt` file but not in the YouTube bulk upload `.csv` format.

---

### PROBLEM: Multiple API Callers with Duplicate Logic
`_call_gemini()`, `_call_gemini_titles()`, and `_call_gemini_adobe_stock()` are three near-identical functions (95% the same code) — only the Pydantic schema differs. This is a maintenance problem.

---

## PART 3 — WHAT CAN BE IMPROVED (Existing Features)

### IMPROVEMENT 1: Niche-Scoped Output Folder Structure
**Current:** `output/<folder_name>/`  
**Improved:** `output/<niche>/<folder_name>/`

When generating from Niche Hub or Bulk with a niche selected, automatically scope all output under the niche folder:
```
output/
  Dark Psychology/
    narcissism-red-flags/
    gaslighting-signs/
  Space & Cosmos/
    black-holes-explained/
  Adobe Stock/
    Mental Health & Wellness/
      batch_20260307_1430/
```
This is the #1 most requested improvement you mentioned and it's a straightforward change to `_write_outputs()` — add an optional `niche` parameter that prepends the niche as a parent folder.

---

### IMPROVEMENT 2: Auto-Apply Niche Styles on Generation
When a niche is selected (in Niche Hub or passed to bulk), automatically inject the niche preset's `image_style` and `video_style` into the generation options. No manual copy-paste required.

---

### IMPROVEMENT 3: Niche-Specific Starter Templates
Pre-populate `templates_data.json` with one default template per niche that includes:
- Niche-specific folder structure (e.g., "Dark Psychology" has a `Manipulation Tactics/` subfolder)
- Style guidance pre-filled
- Placeholder prompts (`{character_description}`, `{manipulation_type}`, etc.)

Example for Dark Psychology:
```
Dark Psychology Template/
  Scripts/
    hook_script       ← {manipulation_type} awareness hook
    main_script       ← educational breakdown of {psychology_concept}
  Image Prompts/
    cover_image       ← thumbnail-style noir portrait
    scene_images      ← cinematic psychological thriller stills
  Videos/
    hook_clip         ← 0-30s attention grabber
    scene_clips       ← full video footage
  Metadata/
    upload_pack       ← SEO-optimized title, description, tags
```

---

### IMPROVEMENT 4: Persist Jobs to Disk
Save `bulk_jobs` and `adobe_stock_jobs` to JSON files in `logs/` on every status change. Load them on app startup. Zero data loss on server restart.

---

### IMPROVEMENT 5: Merge Duplicate Gemini Callers
Create one generic `_call_gemini_schema(prompt, schema, temperature, max_tokens)` function that all three callers use. Reduces 300 lines to ~100.

---

### IMPROVEMENT 6: Download Buttons for Adobe Stock Output
Add a `/api/adobe-stock/download/<job_id>` endpoint that streams a ZIP of all output files. Add a download button to the Adobe Stock UI card.

---

### IMPROVEMENT 7: Actual YouTube Chapter Timestamps
In `_write_outputs()`, calculate estimated timestamps from voiceover word count:
- Average TTS speed: ~145 words per minute
- Scene 1 starts at 0:00, Scene 2 at word_count/145 minutes, etc.
- Inject these into the upload_description as real timestamps instead of the placeholder.

---

### IMPROVEMENT 8: Thumbnail Image Prompts
Add a `thumbnail_prompt` field to the `UploadPack` schema and generate it automatically:
- Emotion-first composition (face close-up, 70% subject)
- Bold text overlay placement area
- High-contrast color palette
- YouTube aspect ratio (16:9 at 1280×720)
- Background vs. foreground separation

---

### IMPROVEMENT 9: Niche Hub — Pre-select and Transfer to Generate
After generating titles, clicking a title in the Niche Hub should open the Generate page with:
- Title pre-filled
- content_brief pre-filled as description
- niche image_style and video_style pre-filled
- A suggested folder name pre-generated

Currently these are disconnected — you have to copy manually.

---

### IMPROVEMENT 10: Project Folder — Download & Delete Buttons
The Projects page shows files but has no actions. Add:
- Download project as ZIP
- Delete project folder
- Open project folder in OS file explorer (via shell:// link on Windows)

---

## PART 4 — NEW FEATURES TO ADD

### NEW FEATURE 1: Per-Niche Template Folder System ⭐ (Your Priority)
**What:** Each niche gets its own dedicated template that defines both the generation style AND the physical folder hierarchy. Selecting a niche automatically uses its template.

**How it works:**
1. User creates a "Niche Template" that binds a niche name to a folder structure + style config
2. When generating for that niche, the folder structure is pre-applied
3. All outputs for that niche go into `output/<niche>/<project>/` with the niche-defined subfolder structure

**Proposed folder structures by niche:**

**Dark Psychology:**
```
Dark Psychology/
  <video-title>/
    1. Script/
    2. Hook Prompts/      ← first 30-second attention-grabbing images
    3. Image Prompts/
    4. Video Prompts/
    5. Metadata/
    6. Thumbnail/
```

**Space & Cosmos:**
```
Space & Cosmos/
  <video-title>/
    1. Script/
    2. Space Visuals/     ← nebula, blackhole, planet images
    3. Animation Prompts/
    4. B-Roll/
    5. Metadata/
    6. Thumbnail/
```

**Adobe Stock per niche:**
```
Adobe Stock/
  Mental Health & Wellness/
    batch_20260307_1430/
      prompts.txt
      titles_keywords.txt
      summary.txt
  Content Creator Economy/
    batch_20260308_0900/
```

---

### NEW FEATURE 2: YouTube Shorts / Reels Generator
**What:** A dedicated generation mode for short-form vertical content

**Spec:**
- Aspect ratio: 9:16
- Length: 30–60 seconds (200–450 word voiceover)
- Single scene or 3-scene structure
- Hook in first 3 seconds (pattern interrupt)
- Generates: hook text overlay prompt, vertical image prompts, vertical video prompts, short description with hashtags
- Output folder: `output/<niche>/Shorts/<title>/`

---

### NEW FEATURE 3: Thumbnail Generator
**What:** Dedicated thumbnail creation prompt generator

**Inputs:** Video title, niche, hook emotion, character description (optional)

**Output:** 3–5 thumbnail image prompts, each following proven CTR formulas:
- Formula A: Shocked face + bold text + arrow
- Formula B: Before/after split
- Formula C: Curiosity gap (obscured element + text)
- Formula D: Authority shot (person looking at camera + credentials)
- Formula E: Number-driven (big bold number + context)

Each prompt includes: exact dimensions (1280×720), text placement zone, color contrast spec, font style, subject positioning.

---

### NEW FEATURE 4: Content Calendar & Series Planner
**What:** Plan a full channel's content schedule from bulk-generated titles

**Features:**
- Set posting schedule (e.g., 3 videos/week)
- Auto-assign generated titles to dates
- Mark which videos are: Planned / Generated / Uploaded
- Export as Google Sheets-compatible CSV
- Series detection: identify titles that could be Part 1/2/3 and group them
- Visual calendar view in the UI

---

### NEW FEATURE 5: YouTube CSV Export (Bulk Upload Format)
**What:** Export generated metadata in the exact format YouTube Studio accepts for bulk upload

**Output columns:** `Playlist`, `Video title`, `Video description`, `Tags`, `Language`, `Made for kids`

**Use case:** After bulk-generating 20 videos, export one CSV → bulk upload all metadata to YouTube in one step.

---

### NEW FEATURE 6: Script-to-SRT Subtitle Generator
**What:** Convert the generated voiceover script into properly timed `.srt` subtitle file

**Logic:**
- Average TTS speed: 145 words/minute (configurable)
- Split voiceover into 5–7 word subtitle chunks
- Auto-calculate timestamps from word count
- Output: `<project>/subtitles.srt` — ready to upload to YouTube

**Use case:** YouTube auto-captions are poor quality. Having an `.srt` file ready improves accessibility and SEO.

---

### NEW FEATURE 7: Hook Script Writer (0–30 Second Hook Generator)
**What:** Generate powerful 30-second hooks separately from the main script

**Why:** The first 30 seconds determine 80% of viewer retention. A dedicated hook generator with different psychological formulas is critical.

**Hook formulas to generate (pick 3–5 variations):**
- The Shocking Stat: Open with a mind-blowing number
- The Contrarian: "Everything you know about X is wrong"
- The Story: Start mid-action, "I was about to lose everything when..."
- The Curiosity Gap: "There's something hiding in plain sight that..."
- The Social Proof: "Over 10 million people make this mistake every day"

**Output:** 5 hook script variations → user picks one → it becomes Scene 1 of their script.

---

### NEW FEATURE 8: Multi-Channel Management
**What:** Organize projects by YouTube channel (not just niche)

**Why:** Many creators run multiple channels simultaneously (e.g., a Dark Psychology channel + a History channel)

**Structure:**
```
output/
  Channels/
    MyDarkPsychChannel/
      Dark Psychology/
        Video1/
        Video2/
    MySpaceChannel/
      Space & Cosmos/
        Video3/
```

**Features:**
- Create named channels with a niche assignment and branding preferences
- All generation for that channel respects its style settings
- Channel-level export ZIP
- Analytics dashboard showing videos per channel, total assets generated

---

### NEW FEATURE 9: Adobe Stock Image Style Presets (Custom Niches)
**What:** Let users define their own Adobe Stock niches beyond the 17 built-in ones

**Features:**
- Form to create custom niche: name, description, photography_style, primary_keywords, sub_niches
- Custom niche saved to `templates_data.json`
- Appears in the Adobe Stock niche selector
- Full pipeline support identical to built-in niches

---

### NEW FEATURE 10: Prompt Variation Testing (A/B Prompt Lab)
**What:** Generate 3–5 variations of the same video concept with different prompt styles → compare outputs

**Use case:** Test whether "cinematic photorealistic" vs "anime illustration" vs "oil painting" style produces better results for a given niche.

**How it works:**
- Enter one title
- Select 2–5 style presets to test
- System generates all variations in parallel
- UI shows them side-by-side in tabs
- User picks the winner → saves to project

---

### NEW FEATURE 11: Voiceover Script Quality Checker
**What:** After generation, run an AI quality check on the voiceover script

**Checks:**
- Word count vs. target duration
- Reading level (aim for Grade 7–9 for broad YouTube audience)
- Hook strength score (does it establish curiosity in first 3 sentences?)
- Retention curve check (does the script have a mid-video pattern interrupt?)
- CTA presence (does the script mention to subscribe/like/comment?)
- Flag any awkward TTS text (abbreviations, symbols, unclear pronunciation)

---

### NEW FEATURE 12: Batch Adobe Stock → CSV for Bulk Upload Tools
**What:** Export `titles_keywords.txt` in the exact CSV format compatible with:
- Adobe Contributor Portal bulk upload tool
- Microstockr
- Filezilla batch metadata injector

**Format:**
```csv
Filename,Title,Category,Keywords,Description
image_001.jpg,"Senior woman using VR headset...",Technology,"senior woman,vr,lifestyle,...","Stock photo of..."
```

---

### NEW FEATURE 13: Dark/Light Mode Toggle
The UI is currently dark-only. A light mode option would help users working in bright environments and during daytime streaming/screensharing.

---

### NEW FEATURE 14: API Key Health Dashboard
**What:** Visual dashboard showing status of all configured API keys

**Shows per key:**
- Key suffix (last 4 chars)
- Current status: Active / Cooling Down / All-time quota calls
- Cooldown countdown timer
- Approximate calls made today
- Color-coded status indicator

**Why:** With multiple API keys, it's hard to know why generation is slow — this shows exactly which keys are throttled.

---

### NEW FEATURE 15: Saved Output Templates (per Niche)
**What:** Pre-made output structures that are automatically applied when generating for a specific niche

**Implementation:**
- In the Templates page, add a "Bind to Niche" option
- When "Dark Psychology" niche is selected anywhere in the app, its bound template auto-applies
- Template controls: which subfolders to create, what files to generate, file naming conventions
- Per-niche default style settings (image_style, video_style, tone) that auto-fill the Generate form

---

## PART 5 — SPECIFIC IMPLEMENTATION PLAN (Priority Order)

### Priority 1 — Niche-Organized Folder Structure (Your #1 Request)
**Files to change:** `app.py`  
**Changes:**
- Add `niche: str = ""` parameter to `_write_outputs()`
- When niche is provided, output path becomes `output/<niche>/<folder_name>/` instead of `output/<folder_name>/`
- Pass niche through from `/api/generate`, `/api/bulk/start`, and `/api/niche/launch-pipeline`
- Add niche column to bulk upload format
- Add niche dropdown to the Generate form in the UI

**Estimated complexity:** Low — surgical change to ~4 functions

---

### Priority 2 — Auto-Apply Niche Styles
**Files to change:** `app.py`, `index.html`  
**Changes:**
- When a niche is selected in Generate page or Bulk, look up `NICHE_PRESETS[niche]` and prefill `image_style` and `video_style` fields
- Add "Apply niche defaults" button next to niche selector
- On Niche Hub pipeline launch, pass niche styles in `options`

**Estimated complexity:** Low

---

### Priority 3 — Per-Niche Default Templates (Pre-populated)
**Files to change:** `app.py` (add `NICHE_DEFAULT_TEMPLATES` dict), `templates_data.json`  
**Changes:**
- Define `NICHE_DEFAULT_TEMPLATES` in `app.py` with folder structures for all 10 YouTube niches
- On first run (or via a "Initialize Templates" button), populate `templates_data.json`
- Bind each niche preset to its template

**Estimated complexity:** Medium

---

### Priority 4 — Job Persistence (No Data Loss on Restart)
**Files to change:** `app.py`  
**Changes:**
- Add `_save_jobs_to_disk()` and `_load_jobs_from_disk()` functions
- Save to `logs/bulk_jobs.json` and `logs/adobe_jobs.json`
- Call save on every status change (completed, failed, cancelled)
- Call load on app startup

**Estimated complexity:** Low–Medium

---

### Priority 5 — Thumbnail Prompt Generator
**Files to change:** `app.py` (add to `GenerationResult` schema), `index.html`  
**Changes:**
- Add `thumbnail_prompts: list[str]` to `UploadPack` schema
- Add thumbnail generation instructions to `_build_prompt()`
- Write `6. Thumbnail/thumbnail_prompts.txt` in `_write_outputs()`
- Display in UI output tabs

**Estimated complexity:** Low

---

### Priority 6 — YouTube Shorts Mode
**Files to change:** `app.py`, `index.html`  
**Changes:**
- Add `short_form: bool` parameter to `_build_prompt()`
- When short_form=True: target 200–450 word voiceover, 1–3 scenes, 9:16 aspect ratio
- Add "Shorts Mode" toggle in Generate form
- Output folder suffix: `_Shorts`

**Estimated complexity:** Medium

---

### Priority 7 — Actual Timestamps in Descriptions
**Files to change:** `app.py`  
**Changes:**
- In `_write_outputs()`, calculate running word count per scene
- Convert to MM:SS timestamps (at 145 wpm)
- Replace "timestamps placeholder" in upload_description with real timestamps

**Estimated complexity:** Very Low

---

### Priority 8 — YouTube CSV Export
**Files to change:** `app.py`, `index.html`  
**Changes:**
- Add `/api/project/<name>/export-csv` endpoint
- Reads `_detail.txt`, reformats to YouTube CSV columns
- Returns downloadable `.csv`
- Add "Export CSV" button to Projects page

**Estimated complexity:** Low

---

## PART 6 — QUICK WINS (Can Be Done in < 1 Hour Each)

| # | Quick Win | Where |
|---|-----------|-------|
| 1 | Add "Copy All" button to each output tab | index.html |
| 2 | Add project folder download ZIP button | projects page |
| 3 | Add total word count + estimated video duration to output | app.py |
| 4 | Add "Delete Project" button with confirmation | projects page |
| 5 | Show niche preset's image/video style as tooltip in Niche Hub | index.html |
| 6 | Add Adobe Stock job download ZIP button | adobe page |
| 7 | Auto-generate folder name from title (slugify in real-time) | index.html JS |
| 8 | Add "Copy Prompt" button to each individual prompt in output | index.html |
| 9 | Add estimated image count to image prompts output tab header | app.py |
| 10 | Pre-populate a sample title in the Generate form | index.html |
| 11 | Add keyboard shortcut Ctrl+Enter to trigger Generate | index.html JS |
| 12 | Show API key count and status in sidebar footer | index.html |
| 13 | Add bulk job creation timestamp to jobs list | index.html |
| 14 | Add "View in Explorer" link for Adobe Stock output folder | index.html |

---

## PART 7 — ARCHITECTURE SUMMARY

### What's Solid
- Gemini multi-key failover with quota tracking — production-grade
- SSE streaming for real-time progress — much better than polling
- Parallel bulk processing with pause/resume/cancel/retry — complete job lifecycle
- Pydantic schema validation with JSON repair fallback — robust
- Responsive UI design (sidebar collapses on mobile) — good UX
- Character Master Variable for visual consistency across prompts — unique value
- Adobe Stock prompts are extremely detailed (camera body, lens, Kelvin temp, composition rule) — high quality
- Exponential backoff with per-key cooldown — smart rate limit handling

### What Needs Work
- Niche/Template/Generation systems are siloed — no interconnection
- All output in one flat `output/` directory — poor organization at scale
- Job state in RAM — not durable
- Three near-identical Gemini caller functions — maintenance debt
- No file-level operations from the UI (delete, download, rename)
- No short-form content mode
- No thumbnail generation
- Templates page starts empty with no guidance

---

## SUMMARY TABLE

| Category | Current State | Improvement Needed |
|----------|--------------|-------------------|
| Output Organization | Flat `output/` folder | Per-niche subfolder hierarchy |
| Niche → Style | Manual copy-paste | Auto-apply from niche preset |
| Templates | Empty, isolated | Pre-filled per niche, bound to presets |
| YouTube Metadata Export | `.txt` file only | `.csv` for YouTube bulk upload |
| Thumbnail | Not generated | Dedicated thumbnail prompt tab |
| Short-form | Not supported | YouTube Shorts / Reels mode |
| Chapter Timestamps | Placeholder text | Auto-calculated from word count |
| Job Persistence | RAM only | Saved to disk |
| Adobe Stock Download | Manual file access | UI download button + ZIP |
| Hook Writing | Part of script | Dedicated 5-variation hook generator |
| Multi-Channel | Not supported | Channel management layer |
| API Key Status | Sidebar text only | Key health dashboard |
| Content Calendar | Not supported | Calendar view + CSV export |

---

*End of Analysis Report*
