I am working on a build feature for my app Saasy.   
    



   Here is the rundown from my ideation notes:

The user can sign in with Firebase and everything is saved to Firebase or Github.
This whole thing will be deployed through google cloud so we need to set up robust measures to gemini CLI gets integrated well.

An end to end platform that uses the Gemini CLI to build a web app (super constrained, a flask web app with all the security measures, HTML frontend, firebase sign in with google, firebase MCP or skill, posthog skill/MCP). ORRR it can use bolt.new via browser use so we don't have to deal with.

Sitch -> Github -> Bolt (gemini computer use)-> Github -> Google Cloud -> gemini CLI for maintenance prolly

Bolt method + steel.dev (or any other browser agent with your own playwright instance):

The prompt it needs to be solid from the jump so 

The user inputs their idea. Then, it builds an entire plan of what the app should do and look like, what's the business case, pricing models, financial projects. Possibly using stitch by google MCP to design the UI. 

If they already have an app, just link the github repo and the github mcp should handle the rest.

The agent interacts with the Gemini CLI and gets notified whenever Gemini CLI is done with its task. Then it can look at the screen and use browser use, or gemini computer use, to test it out, make sure everything is alright.

Once the app MVP is ready, it is launch time! The Github MCP is used and the app is deployed. The app is deployed to google cloud run via the google cloud MCP. 

Ok so now the app is launched, but how will distribution work?

Three channels: 
Cold email with a deep research for partnerships
Reddit Posts via browser use or available MCPs
Reels -> Playwright MCP and Remotion to get videos and screenshots of ur web app -> Nano Banana of screenshots to get more images -> Gemini Script writing, GOOD MARKETING SCRIPT - Veo 3.1 10 videos. User has to upload them, but we can make a good UI for them to watch and select which ones are best

Learns from your emails, emails customers and pulls customer list from firebase or you can upload spreadsheet. Then it emails all users, and even looks at posthog to find which users are using the most and should email first. 
Sets up user interviews, and it knows when to notify you for time negotiation, and even asks if you want to set up a cal link and then give it the cal link so users can just pick. It should use the Gmail or outlook MCP, the user's choice. 

Financial projects. Ask the user to connect stripe to posthog. Then we can get an agent to scrape all data from posthog website (browser u signed in through) and then analyze it, create financial reports and strategy.


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
