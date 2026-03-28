# Saasy — Build Log

## What is Saasy?
An end-to-end AI platform that builds, launches, and runs a SaaS business on autopilot. The user describes an idea, Saasy validates it, builds the app, handles distribution, and runs operations in the background — even when the app isn't open.

---

## Tech Stack
- **Backend:** FastAPI (`app.py`)
- **Frontend:** HTML + CSS + vanilla JS (`templates/`, `static/`)
- **AI:** Gemini API (`gemini-3-flash-preview` for chat/PRD, `gemini-2.5-computer-use-preview-10-2025` for build)
- **Browser automation:** Steel.dev (remote browser) + Playwright
- **Auth + Storage:** Firebase (Google sign-in, Firestore)
- **Deployment target:** Google Cloud Run

---

## Pages

| Route | File | Purpose |
|---|---|---|
| `/` | `index.html` | Landing + Google sign-in |
| `/dashboard` | `dashboard.html` | Business portfolio |
| `/new` | `new.html` | Ideation chat + live PRD canvas |
| `/build` | `build.html` | Build mode — agent pipeline + live browser |

---

## What Was Built This Session

### 1. UI Redesign (Apple/Jony Ive quality)
- Full redesign of `main.css` — tighter typographic scale, `-0.04em` letter spacing on headlines, `cubic-bezier(0.16,1,0.3,1)` easing, hairline borders (`--gray-150`), staggered `animate-in` entry animations
- **Landing page** (`index.html`): fixed blur-backdrop nav, hero badge chip, bigger headline (`clamp(44px,9vw,64px)`), sequential fade-up animations
- **Dashboard** (`dashboard.html`): refined sidebar, business cards with live/building status dots
- **New page** (`new.html`): unchanged structure, minor polish

### 2. Build Mode Page (`build.html`) — new
- Fixed header: back button, logo, build title, live status badge (pulsing dot), pause button
- **Left panel:** Pipeline (5 steps: Plan → GitHub → Build → Test → Deploy) with animated progress bar + step state machine (pending/active/done/error) + scrolling agent log with timestamps
- **Right panel:** Browser chrome (macOS dots, URL bar, spinner) + full viewport for live screenshots streamed as base64
- SSE consumer: handles `step_start`, `step_done`, `log`, `screenshot`, `url`, `browser_loading`, `done`, `heartbeat` events

### 3. `/api/build` Endpoint (`app.py`)
Full build orchestration pipeline running in a background thread, streaming SSE events:

1. **Plan** — receives PRD text, formats it into a Bolt.new task prompt
2. **Browser** — creates a Steel.dev remote browser session (15 min max, hobby plan)
3. **Gemini Computer Use loop** — up to 50 turns:
   - Takes screenshot of current browser state
   - Sends to `gemini-2.5-computer-use-preview-10-2025` with task
   - Executes returned function calls via Playwright (click, type, scroll, navigate, drag, key combos, etc.)
   - Streams screenshots + URL + log entries back as SSE
4. **Cleanup** — releases Steel.dev session in `finally` block
5. **Heartbeat** — 25s timeout keeps SSE connection alive during long Bolt.new builds

### 4. Firebase Integration (added by user)
- Firebase Admin SDK initialized from env vars
- `_get_uid()` — verifies Firebase ID token from `Authorization: Bearer` header
- `_fs_save_messages()` — persists chat turns to Firestore: `users/{uid}/businesses/{bid}/messages`
- `_fs_save_prd()` — persists PRD markdown to Firestore: `users/{uid}/businesses/{bid}`
- Both `/api/chat` and `/api/prd` accept `businessId` and auth token, save to Firestore

### 5. PRD Flow Fix
- `/api/prd` switched from `interactions.create` (Gemini server-side history) to explicit `history` array passed by client
- PRD text stored in `sessionStorage('saasy_prd')` when Build button appears
- `/api/build` reads `prd_text` directly from request body — no interaction ID lookup

---

## Environment Variables Required

```
GEMINI_API_KEY=...
STEEL_API_KEY=...           # app.steel.dev (hobby plan = 15 min sessions)

# Firebase Admin (service account)
FIREBASE_PROJECT_ID=saasy-aa123
FIREBASE_PRIVATE_KEY_ID=...
FIREBASE_PRIVATE_KEY=...
FIREBASE_CLIENT_EMAIL=...
FIREBASE_CLIENT_ID=...
FIREBASE_AUTH_URI=...
FIREBASE_TOKEN_URI=...
FIREBASE_AUTH_PROVIDER_X509_CERT_URL=...
FIREBASE_CLIENT_X509_CERT_URL=...
```

---

## Dependencies

```
fastapi
uvicorn[standard]
jinja2
python-multipart
python-dotenv
google-genai>=1.55.0
firebase-admin
steel-sdk
playwright
```

Post-install: `playwright install chromium`

---

## Known Issues / Next Steps

- [ ] Build mode — need to verify Gemini Computer Use `FunctionResponse` with screenshot parts works correctly with the SDK version in use
- [ ] Dashboard — business cards are static; need to load from Firestore
- [ ] GitHub MCP integration for pushing built app to a repo
- [ ] Google Cloud Run deployment step (currently marks as done after Bolt.new)
- [ ] Distribution features: cold email, Reddit posts, Reels generation
- [ ] PostHog + Stripe analytics integration
- [ ] Steel.dev hobby plan = 15 min sessions; upgrade for longer builds
