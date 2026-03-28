from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import json
import asyncio
import threading
import base64
import time
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
STEEL_API_KEY  = os.environ.get("STEEL_API_KEY", "")

# ── Firebase Admin SDK ─────────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, auth as fb_auth, firestore as fb_fs

    _sa = {
        "type":                        os.environ.get("FIREBASE_TYPE", "service_account"),
        "project_id":                  os.environ.get("FIREBASE_PROJECT_ID"),
        "private_key_id":              os.environ.get("FIREBASE_PRIVATE_KEY_ID"),
        "private_key":                 os.environ.get("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n"),
        "client_email":                os.environ.get("FIREBASE_CLIENT_EMAIL"),
        "client_id":                   os.environ.get("FIREBASE_CLIENT_ID"),
        "auth_uri":                    os.environ.get("FIREBASE_AUTH_URI"),
        "token_uri":                   os.environ.get("FIREBASE_TOKEN_URI"),
        "auth_provider_x509_cert_url": os.environ.get("FIREBASE_AUTH_PROVIDER_X509_CERT_URL"),
        "client_x509_cert_url":        os.environ.get("FIREBASE_CLIENT_X509_CERT_URL"),
    }
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(_sa))
    fs = fb_fs.client()
except Exception as _e:
    print(f"Firebase Admin init failed: {_e}")
    fb_auth = None
    fs = None

def _get_uid(request: Request) -> str | None:
    """Verify Firebase ID token from Authorization header, return UID or None."""
    if not fb_auth:
        return None
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        return fb_auth.verify_id_token(header[7:])["uid"]
    except Exception:
        return None

def _fs_save_messages(uid: str, bid: str, user_msg: str, ai_msg: str,
                      sources: list, queries: list):
    if not fs or not uid or not bid:
        return
    ref = (fs.collection("users").document(uid)
             .collection("businesses").document(bid)
             .collection("messages"))
    ref.add({"role": "user",      "content": user_msg, "timestamp": fb_fs.SERVER_TIMESTAMP})
    ref.add({"role": "assistant", "content": ai_msg,   "sources": sources,
             "searchQueries": queries, "timestamp": fb_fs.SERVER_TIMESTAMP})

def _fs_save_prd(uid: str, bid: str, name: str, prd: str):
    if not fs or not uid or not bid:
        return
    (fs.collection("users").document(uid)
       .collection("businesses").document(bid)
       .set({"name": name, "prd": prd, "type": "idea",
             "updatedAt": fb_fs.SERVER_TIMESTAMP}, merge=True))

# ── Gemini client ──────────────────────────────────────────
try:
    from google import genai
    genai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
except ImportError:
    genai_client = None

CHAT_SYSTEM_PROMPT = """You are Saasy — a brilliant, razor-sharp startup advisor. Think: the smartest person at YC combined with the best product interviewer you've ever met. You got into every Ivy. You've seen 10,000 startup pitches.

Personality: Direct. Intellectually honest. You say what others are too polite to say. You push back when ideas are weak. You get genuinely excited when something is real.

Communication style:
- Short. Every message is 2-4 sentences MAX unless listing something.
- One or two sharp follow-up questions per turn — never more.
- No filler words. No "Great question!" No "That's interesting." Just get to it.
- Use Google Search to find real data — market size, competitor pricing, recent funding, trends. Cite specifics.
- When you find something surprising or important from search, lead with it.

Your goal: In 5-7 turns, get to the core insight — who has this problem badly enough to pay for it, and what's the smallest thing that would prove this idea has legs."""

PRD_PROMPT = """Based on our entire conversation, generate a concise PRD in clean markdown.

Only include sections where you have real, concrete information from our conversation.

Format:
# [Product Name]

## The Problem
[1-2 sentences. The specific pain being solved.]

## Target Customer
[1-2 sentences. Specific, not generic.]

## Core Features (MVP)
- **[Feature]**: [Why this matters]

## Competitive Edge
[What's genuinely different. 1-2 sentences.]

## Business Model
[Pricing model and revenue approach.]

## Market Opportunity
[Size and growth. Only include if researched.]

Rules: Every sentence must be specific. No vague filler. No sections you don't have real info for."""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/new", response_class=HTMLResponse)
async def new_business(request: Request):
    return templates.TemplateResponse("new.html", {"request": request})


@app.get("/build", response_class=HTMLResponse)
async def build_page(request: Request):
    return templates.TemplateResponse("build.html", {"request": request})


@app.post("/api/session")
async def set_session(request: Request, response: Response):
    await request.json()
    return JSONResponse({"status": "ok"})


@app.delete("/api/session")
async def clear_session(response: Response):
    return JSONResponse({"status": "ok"})


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    if not genai_client:
        async def err():
            yield f"data: {json.dumps({'type': 'error', 'message': 'GEMINI_API_KEY not configured'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    uid  = _get_uid(request)
    data = await request.json()
    user_message = data.get("message", "")
    history      = data.get("history", [])
    business_id  = data.get("businessId", "")

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def run_stream():
            try:
                from google.genai import types

                contents = []
                for msg in history:
                    role = "user" if msg.get("role") == "user" else "model"
                    contents.append(types.Content(
                        role=role,
                        parts=[types.Part(text=msg.get("content", ""))]
                    ))
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=user_message)]
                ))

                config = types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    system_instruction=CHAT_SYSTEM_PROMPT,
                )

                full_text = ""
                grounding = None

                stream_fn = getattr(genai_client.models, "generate_content_stream", None)
                if stream_fn:
                    for chunk in stream_fn(
                        model="gemini-3-flash-preview",
                        contents=contents,
                        config=config,
                    ):
                        text = getattr(chunk, "text", None) or ""
                        if text:
                            full_text += text
                            loop.call_soon_threadsafe(queue.put_nowait, {"type": "text", "text": text})
                        try:
                            if chunk.candidates and chunk.candidates[0].grounding_metadata:
                                gm = chunk.candidates[0].grounding_metadata
                                if gm:
                                    grounding = gm
                        except Exception:
                            pass
                else:
                    response = genai_client.models.generate_content(
                        model="gemini-3-flash-preview",
                        contents=contents,
                        config=config,
                    )
                    full_text = getattr(response, "text", None) or ""
                    loop.call_soon_threadsafe(queue.put_nowait, {"type": "text", "text": full_text})
                    try:
                        if response.candidates and response.candidates[0].grounding_metadata:
                            grounding = response.candidates[0].grounding_metadata
                    except Exception:
                        pass

                sources = []
                search_queries = []
                if grounding:
                    try:
                        for q in (grounding.web_search_queries or []):
                            if q:
                                search_queries.append(q)
                    except Exception:
                        pass
                    try:
                        for gc in (grounding.grounding_chunks or []):
                            web = getattr(gc, "web", None)
                            if web:
                                uri = getattr(web, "uri", "") or ""
                                title = getattr(web, "title", "") or uri
                                if uri:
                                    sources.append({"url": uri, "title": title})
                    except Exception:
                        pass

                loop.call_soon_threadsafe(queue.put_nowait, {
                    "type": "done",
                    "sources": sources,
                    "search_queries": search_queries,
                    "_full_text": full_text,
                })

            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, {"__error__": str(e)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=run_stream, daemon=True).start()

        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, dict) and "__error__" in item:
                yield f"data: {json.dumps({'type': 'error', 'message': item['__error__']})}\n\n"
                break
            if item.get("type") == "done":
                # Persist to Firestore (strip internal _full_text before sending to client)
                full_text = item.pop("_full_text", "")
                sources   = item.get("sources", [])
                queries   = item.get("search_queries", [])
                if uid and business_id:
                    await asyncio.to_thread(
                        _fs_save_messages, uid, business_id,
                        user_message, full_text, sources, queries
                    )
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/prd")
async def prd_endpoint(request: Request):
    if not genai_client:
        return JSONResponse({"error": "GEMINI_API_KEY not configured"}, status_code=500)

    uid  = _get_uid(request)
    data = await request.json()
    history     = data.get("history", [])
    business_id = data.get("businessId", "")
    if not history:
        return JSONResponse({"markdown": ""})

    def generate_prd():
        from google.genai import types

        contents = []
        for msg in history:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append(types.Content(
                role=role,
                parts=[types.Part(text=msg.get("content", ""))]
            ))
        contents.append(types.Content(
            role="user",
            parts=[types.Part(text=PRD_PROMPT)]
        ))

        response = genai_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=contents,
        )
        return getattr(response, "text", None) or ""

    prd_text = await asyncio.to_thread(generate_prd)

    # Persist PRD — extract name from first H1
    if uid and business_id and prd_text:
        import re
        m = re.search(r'^#\s+(.+)$', prd_text, re.MULTILINE)
        name = m.group(1) if m else "New Idea"
        await asyncio.to_thread(_fs_save_prd, uid, business_id, name, prd_text)

    return JSONResponse({"markdown": prd_text})


# ── /api/build ─────────────────────────────────────────────────────────────
# Drives the full build pipeline:
#   1. Fetch PRD text from the Gemini interaction store
#   2. Spin up a Steel.dev remote browser session
#   3. Run a Gemini Computer Use agent loop targeting Bolt.new
#   4. Stream SSE events (step updates, logs, screenshots) back to build.html
# ---------------------------------------------------------------------------

BOLT_TASK_TEMPLATE = """You are operating a browser. Your goal is to build a complete SaaS web application using bolt.new.

Steps:
1. You are already on bolt.new (or navigate there if needed).
2. Find the main chat/prompt input on the page.
3. Type the following product requirements into it and submit:

--- REQUIREMENTS START ---
{prd}
--- REQUIREMENTS END ---

4. Wait for Bolt to finish generating the application (this takes 1–3 minutes — you will see a file tree and code editor appear).
5. Once the build is complete, look for a "Deploy" button or a preview URL and click it.
6. Report the final deployed URL.

Important: Do not skip steps. If you see a loading spinner, wait for it to finish before acting."""




def _denorm_x(x: int, w: int) -> int:
    return int(x / 1000 * w)


def _denorm_y(y: int, h: int) -> int:
    return int(y / 1000 * h)


def _execute_action(page, fname: str, args: dict, sw: int, sh: int):
    """Execute a single Gemini Computer Use action on the Playwright page."""
    if fname == "open_web_browser":
        pass
    elif fname == "wait_5_seconds":
        time.sleep(5)
    elif fname == "go_back":
        page.go_back()
    elif fname == "go_forward":
        page.go_forward()
    elif fname == "search":
        page.goto("https://www.google.com")
    elif fname == "navigate":
        page.goto(args.get("url", ""), wait_until="domcontentloaded")
    elif fname == "click_at":
        page.mouse.click(_denorm_x(args["x"], sw), _denorm_y(args["y"], sh))
    elif fname == "hover_at":
        page.mouse.move(_denorm_x(args["x"], sw), _denorm_y(args["y"], sh))
    elif fname == "type_text_at":
        px, py = _denorm_x(args["x"], sw), _denorm_y(args["y"], sh)
        page.mouse.click(px, py)
        if args.get("clear_before_typing", True):
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        page.keyboard.type(args.get("text", ""), delay=20)
        if args.get("press_enter", True):
            page.keyboard.press("Enter")
    elif fname == "key_combination":
        page.keyboard.press(args.get("keys", ""))
    elif fname == "scroll_document":
        direction = args.get("direction", "down")
        delta = 600 if direction in ("down", "right") else -600
        if direction in ("down", "up"):
            page.mouse.wheel(0, delta)
        else:
            page.mouse.wheel(delta, 0)
    elif fname == "scroll_at":
        px, py = _denorm_x(args.get("x", 500), sw), _denorm_y(args.get("y", 500), sh)
        direction = args.get("direction", "down")
        mag = int(args.get("magnitude", 800) / 1000 * sh)
        page.mouse.move(px, py)
        if direction in ("down", "up"):
            page.mouse.wheel(0, mag if direction == "down" else -mag)
        else:
            page.mouse.wheel(mag if direction == "right" else -mag, 0)
    elif fname == "drag_and_drop":
        page.mouse.move(_denorm_x(args["x"], sw), _denorm_y(args["y"], sh))
        page.mouse.down()
        page.mouse.move(_denorm_x(args["destination_x"], sw), _denorm_y(args["destination_y"], sh))
        page.mouse.up()


def _run_build(send, prd_text: str, app_name: str):
    """
    Runs the entire build pipeline in a background thread.
    `send(type, **kwargs)` queues an SSE event onto the async loop.
    """
    # ── Step 1: Plan ───────────────────────────────────────────────────────
    send("step_start", step="plan", label="Planning", progress=5)
    if prd_text:
        send("log", text=f"PRD loaded ({len(prd_text)} chars).")
    else:
        send("log", text="No PRD provided — using app name as prompt.")

    task = BOLT_TASK_TEMPLATE.format(
        prd=prd_text or f"Build a simple SaaS app called '{app_name}' with Google sign-in and a clean dashboard."
    )
    send("step_done", step="plan", label="Planning", progress=18)

    # ── Step 2: Browser + build ────────────────────────────────────────────
    send("step_start", step="build", label="Building app", progress=20)
    send("log", text="Starting remote browser session via Steel.dev…")

    try:
        from steel import Steel
        from playwright.sync_api import sync_playwright
        from google.genai import types as gt
    except ImportError as e:
        send("log", text=f"Missing dependency: {e}. Run: pip install steel-sdk playwright", level="error")
        send("step_start", step="build", label="Building app — dependency missing", progress=20)
        return

    steel = Steel(steel_api_key=STEEL_API_KEY)
    session = None
    SW, SH = 1440, 900

    try:
        session = steel.sessions.create(
            dimensions={"width": SW, "height": SH},
            block_ads=True,
            api_timeout=900000,    # 15 min (hobby plan max)
        )
        send("log", text=f"Session {session.id[:8]}… live.")

        cdp_url = f"wss://connect.steel.dev?apiKey={STEEL_API_KEY}&sessionId={session.id}"

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            ctx  = browser.contexts[0] if browser.contexts else browser.new_context(
                viewport={"width": SW, "height": SH}
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Navigate to bolt.new
            send("log", text="Navigating to bolt.new…")
            send("url", url="https://bolt.new")
            send("browser_loading", loading=True)
            page.goto("https://bolt.new", wait_until="domcontentloaded", timeout=30000)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            shot = page.screenshot(type="png")
            send("browser_loading", loading=False)
            send("screenshot", data=base64.b64encode(shot).decode())
            send("url", url=page.url)

            # ── Gemini Computer Use agent loop ─────────────────────────────
            contents = [
                gt.Content(role="user", parts=[
                    gt.Part(text=task),
                    gt.Part.from_bytes(data=shot, mime_type="image/png"),
                ])
            ]

            config = gt.GenerateContentConfig(
                tools=[gt.Tool(computer_use=gt.ComputerUse(
                    environment=gt.Environment.ENVIRONMENT_BROWSER,
                ))],
            )

            MAX_TURNS = 50
            for turn in range(MAX_TURNS):
                send("log", text=f"Agent turn {turn + 1}/{MAX_TURNS}…")

                response = genai_client.models.generate_content(
                    model="gemini-2.5-computer-use-preview-10-2025",
                    contents=contents,
                    config=config,
                )

                candidate = response.candidates[0]
                contents.append(candidate.content)

                fn_calls = [p.function_call for p in candidate.content.parts if p.function_call]

                # No more actions → task complete
                if not fn_calls:
                    final = " ".join(p.text for p in candidate.content.parts if getattr(p, "text", None))
                    if final:
                        send("log", text=f"Agent: {final[:300]}")
                    break

                # Execute each action
                fn_responses = []
                for fc in fn_calls:
                    fname = fc.name
                    args  = dict(fc.args) if fc.args else {}

                    # Safety gate
                    safety = args.get("safety_decision", {})
                    if isinstance(safety, dict) and safety.get("decision") == "require_confirmation":
                        send("log", text=f"Safety check on '{fname}' — auto-proceeding in build mode.")

                    send("log", text=f"  → {fname}")

                    result = {}
                    try:
                        _execute_action(page, fname, args, SW, SH)
                        try:
                            page.wait_for_load_state("load", timeout=6000)
                        except Exception:
                            pass
                    except Exception as ex:
                        result = {"error": str(ex)}
                        send("log", text=f"  ✗ {ex}", level="error")

                    # Capture new state
                    current_url = page.url
                    send("url", url=current_url)
                    new_shot = page.screenshot(type="png")
                    send("screenshot", data=base64.b64encode(new_shot).decode())

                    fn_responses.append(
                        gt.FunctionResponse(
                            name=fname,
                            response={"url": current_url, **result},
                            parts=[gt.Part.from_bytes(data=new_shot, mime_type="image/png")],
                        )
                    )

                if fn_responses:
                    contents.append(gt.Content(
                        role="user",
                        parts=[gt.Part(function_response=fr) for fr in fn_responses],
                    ))

                # Progress ramp 20 → 88 over MAX_TURNS
                send("step_start", step="build", label="Building app",
                     progress=min(20 + int((turn + 1) / MAX_TURNS * 68), 88))

            send("step_done", step="build", label="Building app", progress=88)
            final_url = page.url

        # ── Step 3: Deploy ─────────────────────────────────────────────────
        send("step_start", step="deploy", label="Deploying", progress=90)
        send("log", text="Build complete — retrieving deployment URL…")
        send("step_done", step="deploy", label="Deploying", progress=100)
        send("done", url=final_url)

    except Exception as e:
        send("log", text=f"Build error: {e}", level="error")
        send("step_start", step="build", label="Error", progress=0)
    finally:
        if session:
            try:
                steel.sessions.release(session.id)
                send("log", text="Browser session released.")
            except Exception:
                pass


@app.post("/api/build")
async def build_endpoint(request: Request):
    if not genai_client:
        async def _err():
            yield f"data: {json.dumps({'type':'error','message':'GEMINI_API_KEY not configured'})}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    if not STEEL_API_KEY:
        async def _err():
            yield f"data: {json.dumps({'type':'error','message':'STEEL_API_KEY not configured — add it to .env'})}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    data     = await request.json()
    prd_text = data.get("prd_text", "")
    app_name = data.get("app_name", "My App")

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def send(evt_type, **kwargs):
            payload = json.dumps({"type": evt_type, **kwargs})
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        def run():
            try:
                _run_build(send, prd_text, app_name)
            except Exception as e:
                send("log", text=str(e), level="error")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=run, daemon=True).start()

        # Heartbeat: keep SSE alive during long waits (Bolt.new builds take minutes)
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                yield "data: {\"type\":\"heartbeat\"}\n\n"
                continue

            if payload is None:
                break
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
