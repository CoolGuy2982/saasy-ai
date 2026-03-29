# Saasy

## Inspiration

Building a SaaS business from zero to launch involves dozens of disconnected tools, platforms, and decisions - ideas die in the gap between "I have an app idea" and "users are paying me." We wanted to collapse that entire journey into a single platform. What if you could go from a napkin idea to a live, marketed SaaS in one session?

## What it does

Saasy is an end-to-end AI platform that turns an idea into a running SaaS business. You describe your idea, and Saasy:

1. **Validates and plans it** - AI chat refines your concept with live Google Search, then auto-generates a full PRD (product requirements doc) in a live canvas beside the conversation
2. **Builds the app** - A Gemini computer use agent opens Bolt.new in a Steel.dev browser session and builds your app with Firebase auth, Supabase for the database, Stripe for payments, and a proper HTML frontend - live-streamed to your screen
3. **Deploys it** - Bolt.new pushes the code to GitHub, Netlify deploys it instantly
4. **Distributes it** - Cold email outreach with deep research, Reddit posts, and AI-generated Reels (Remotion + Veo video generation from screenshots of your app)
5. **Grows it** - Emails your user list (pulling from Firebase or a spreadsheet), schedules user interviews, and generates financial reports from PostHog + Stripe data

## How we built it

- **Frontend:** Pure HTML/CSS/JS - black and white, Apple-quality design with Inter font
- **Backend:** FastAPI serving all pages and orchestrating agents
- **Auth:** Firebase Google Sign-In
- **AI:** Gemini Interactions API (`gemini-3-flash-preview` for chat/planning, `gemini-2.5-pro` for complex reasoning, Deep Research agent for market/outreach research)
- **Browser automation:** Steel.dev for managed browser sessions + Gemini computer use to operate Bolt.new and test the built app
- **Built app stack:** Bolt.new generates apps with Supabase (database), Stripe (payments), and Firebase auth out of the box
- **Deployment:** Netlify via Bolt.new's GitHub integration
- **Persistence:** Firebase Firestore for all session state, logs, and marketing data

## Challenges we ran into

- Keeping the browser agent loop reliable - Gemini computer use needs precise prompting and screenshot feedback to not go off-rails inside Bolt.new
- Streaming live browser screenshots back to the frontend in real-time without overwhelming the connection
- Stitching together a coherent multi-step pipeline (ideate → build → deploy → distribute) where each phase hands off state cleanly to the next
- Generating video content (Reels) programmatically - getting Remotion + Veo to produce something that actually looks good without human intervention

## Accomplishments that we're proud of

- A genuinely end-to-end demo: you type an idea and watch an agent build and deploy a real web app, live on screen
- The two-panel ideation UI where the PRD canvas populates in real-time as you chat feels surprisingly good to use
- Every app Saasy builds comes pre-wired with Supabase, Stripe, and auth - it's not just a deployed placeholder, it's a functioning SaaS skeleton
- The distribution pipeline is something no one else has - AI-generated Reels from your own app screenshots is a novel idea that actually works

## What we learned

- Gemini computer use is powerful but requires a tight feedback loop - screenshot → reasoning → action cycles need careful prompt engineering to stay on task
- Steel.dev's managed browser sessions are the right abstraction for this; trying to self-host Playwright for a live-streamed agent is a reliability nightmare
- Bolt.new's Netlify + Supabase + Stripe defaults are a huge unlock - the agent doesn't have to configure infra from scratch, it just has to build the product
- The biggest UX challenge isn't the AI - it's giving the user just enough control (which videos to post, which emails to send) without breaking the autonomy of the pipeline

## What's next for Saasy

- Let users link an existing GitHub repo and skip straight to the launch/distribution phase
- PostHog + Stripe financial dashboards with AI-generated growth strategy reports
- Automated user interview scheduling via Gmail/Outlook MCP with Cal.com link integration
- A mobile app so founders can watch their agent work from their phone
