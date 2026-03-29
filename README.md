# Saasy

**From idea to live SaaS — fully autonomous.**

Saasy is an AI platform that takes a founder from raw idea to a deployed, marketed, revenue-ready web app in a single session. You describe what you want to build. Saasy validates it, builds it, deploys it, and starts finding customers — while you watch.

---

## What it does

The platform runs a five-stage autonomous pipeline:

**1. Ideate** — An AI startup advisor (powered by Gemini with live Google Search) interrogates your idea like a YC partner. It pulls real market data, competitor pricing, and recent funding rounds in real time, then synthesizes the conversation into a Product Requirements Document that auto-populates in a live canvas beside the chat.

**2. Build** — A Gemini computer use agent opens Bolt.new in a managed Steel.dev browser session and builds your full web app — with Supabase database, Stripe payments, and auth — from the PRD. The browser is live-streamed to your screen. You watch it work.

**3. Deploy** — Bolt.new pushes the code to GitHub. Netlify deploys it. The whole thing goes live without you touching a terminal.

**4. Distribute** — Saasy takes screenshots of your deployed app, uses Gemini multimodal understanding to write cinematic Veo 3.1 video prompts tailored to the actual UI, and generates marketing reels. It also runs deep research on potential customers and drafts cold email sequences.

**5. Grow** — Emails your user list from Firebase or a spreadsheet, ranks leads by PostHog engagement data, and generates financial reports from Stripe + PostHog analytics.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Gunicorn (uvicorn worker) |
| Frontend | HTML / CSS / JS — pure black and white, Inter font |
| Auth | Firebase Google Sign-In |
| Database | Firebase Firestore |
| Storage | Firebase Storage |
| AI — Chat + Grounding | Gemini 3 Flash Preview + Google Search tool |
| AI — Builder Agent | Gemini 3 Flash Preview with Computer Use |
| AI — Analytics | Gemini 2.5 Flash |
| AI — Video | Veo 3.1 |
| AI — UI Design | Google Stitch MCP |
| Browser Automation | Steel.dev + Playwright |
| App Stack (built apps) | Bolt.new → Supabase + Stripe + Netlify |
| Deployment | Google Cloud Run |

---

## How the build agent works

The autonomous builder is a vision-action loop:

1. Gemini receives the PRD and a master prompt describing every step of the Bolt.new workflow
2. Gemini returns `ComputerUseAction` objects — clicks, keystrokes, scrolls, waits
3. Each action is executed against the Steel.dev browser session via Playwright
4. A new screenshot is taken and fed back as the next turn's input
5. The loop runs until Gemini detects a successful Netlify deploy URL in the browser

The entire session is streamed to the frontend over a WebSocket — every screenshot, every log line, every pipeline step — so you can watch the agent work in real time. If you close the tab, the build keeps running. Events are persisted to Firestore and replayed when you return.

---

## Project structure

```
saasy-ai/
├── app.py                  # FastAPI app — all routes, agents, pipelines
├── templates/
│   ├── index.html          # Landing / sign-in
│   ├── dashboard.html      # Business portfolio
│   ├── new.html            # Ideation chat + PRD canvas
│   ├── build.html          # Build pipeline live view
│   ├── marketing.html      # Marketing hub
│   ├── marketing_reels.html
│   └── marketing_email.html
├── static/                 # CSS, JS, assets
├── Dockerfile
├── cloudbuild.yaml         # Cloud Run continuous deployment
├── requirements.txt
└── .env                    # Local secrets (never committed)
```

---

## Running locally

**Prerequisites:** Python 3.12+, a Google Cloud project with Gemini API enabled, Firebase project, Steel.dev account.

```bash
git clone https://github.com/your-username/saasy-ai
cd saasy-ai
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file:

```env
GEMINI_API_KEY=your_gemini_api_key
STEEL_API_KEY=your_steel_api_key
STITCH_API_KEY=your_stitch_api_key

FIREBASE_TYPE=service_account
FIREBASE_PROJECT_ID=your_project_id
FIREBASE_PRIVATE_KEY_ID=...
FIREBASE_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n..."
FIREBASE_CLIENT_EMAIL=...
FIREBASE_CLIENT_ID=...
FIREBASE_AUTH_URI=https://accounts.google.com/o/oauth2/auth
FIREBASE_TOKEN_URI=https://oauth2.googleapis.com/token
FIREBASE_AUTH_PROVIDER_X509_CERT_URL=https://www.googleapis.com/oauth2/v1/certs
FIREBASE_CLIENT_X509_CERT_URL=...
```

```bash
uvicorn app:app --reload --port 8080
```

Open `http://localhost:8080`.

---

## Deploying to Google Cloud Run

Saasy uses Cloud Build for continuous deployment from GitHub. Every push to `main` builds and redeploys automatically.

**One-time setup:**

```bash
# Store secrets (run once per secret)
echo -n "your_value" | gcloud secrets create GEMINI_API_KEY --data-file=-
# repeat for STEEL_API_KEY, STITCH_API_KEY, and all FIREBASE_* keys

# Grant Cloud Build the required permissions
PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format="value(projectNumber)")

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$PROJECT_NUMBER@cloudbuild.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$PROJECT_NUMBER@cloudbuild.gserviceaccount.com" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$PROJECT_NUMBER@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Create Artifact Registry repo
gcloud artifacts repositories create cloud-run-source-deploy \
  --repository-format=docker \
  --location=us-central1
```

Then go to **Cloud Run > Create Service > Continuously deploy from a repository**, connect your GitHub repo, select branch `main`, and choose "Use existing `cloudbuild.yaml`."

**Why the deployment flags matter:**

- `--cpu-always-allocated` — Cloud Run normally pauses CPU between requests. Background build/email/video threads run between requests. This flag keeps them running.
- `--min-instances=1` — Prevents scale-to-zero from killing in-flight jobs.
- `--max-instances=1` — Single instance keeps in-memory job state consistent.
- `--timeout=3600` — Allows WebSocket connections to stay open for up to 1 hour during long builds.

---

## Environment variables reference

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | Google Gemini API key (enables all AI features + Veo) |
| `STEEL_API_KEY` | Steel.dev API key (managed browser sessions) |
| `STITCH_API_KEY` | Google Stitch API key (AI UI design) |
| `FIREBASE_*` | Firebase service account credentials (auth, Firestore, Storage) |

---

## Gemini capabilities used

- **Streaming generation + Google Search grounding** — ideation chat with live web research
- **Computer use** (`ENVIRONMENT_BROWSER`) — autonomous Bolt.new agent
- **Multimodal understanding** — screenshot analysis for Veo prompt generation
- **Structured output** — PRD synthesis, lead scoring, cold email drafting
- **Veo 3.1** — image-conditioned marketing video generation
- **Google Stitch MCP** — AI-generated UI design screens

---

## License

MIT
