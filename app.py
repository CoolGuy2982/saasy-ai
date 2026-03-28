from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import json
import asyncio
import threading
import base64
import time
import queue
from dotenv import load_dotenv
from google.genai.errors import ClientError

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

IMPORTANT: If the user has merely said hello or hasn't provided enough specific details about their idea to form at least a coherent problem statement and core features, DO NOT generate a PRD. Instead, reply with exactly and only:
NOT_READY

If there IS enough information, generate the PRD using the following format:
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

Rules: Every sentence must be specific. No vague filler. No sections you don't have real info for. Output only the markdown, no introductory or concluding sentences."""


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


@app.get("/api/history")
async def get_history(request: Request, bid: str):
    uid = _get_uid(request)
    if not uid or not fs or not bid:
        return JSONResponse({"messages": []})
    try:
        docs = (fs.collection("users").document(uid)
                  .collection("businesses").document(bid)
                  .collection("messages").order_by("timestamp").stream())
        msgs = []
        for d in docs:
            data = d.to_dict()
            data.pop("timestamp", None)
            msgs.append(data)
        return JSONResponse({"messages": msgs})
    except Exception as e:
        print(f"Failed loading history: {e}")
        return JSONResponse({"messages": []})


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
                sources = []
                search_queries = []
                seen_urls = set()

                stream_fn = getattr(genai_client.models, "generate_content_stream", None)
                if stream_fn:
                    for chunk in stream_fn(
                        model="gemini-3-flash-preview",
                        contents=contents,
                        config=config,
                    ):
                        text = getattr(chunk, "text", None) or ""
                        try:
                            if chunk.candidates and chunk.candidates[0].grounding_metadata:
                                gm = chunk.candidates[0].grounding_metadata
                                if gm:
                                    q_list = [q for q in (gm.web_search_queries or []) if q]
                                    s_list = []
                                    if hasattr(gm, 'grounding_chunks') and gm.grounding_chunks:
                                        for gc in gm.grounding_chunks:
                                            web = getattr(gc, "web", None)
                                            if web:
                                                uri = getattr(web, "uri", "") or ""
                                                title = getattr(web, "title", "") or uri
                                                if uri:
                                                    if uri not in seen_urls:
                                                        seen_urls.add(uri)
                                                        s_list.append({"url": uri, "title": title})
                                                        sources.append({"url": uri, "title": title})
                                    
                                    for q in q_list:
                                        if q not in search_queries:
                                            search_queries.append(q)

                                    if q_list or s_list:
                                        # send the fully accumulated lists for the client
                                        loop.call_soon_threadsafe(queue.put_nowait, {"type": "search_query", "queries": search_queries, "sources": sources})
                        except Exception:
                            pass

                        if text:
                            full_text += text
                            loop.call_soon_threadsafe(queue.put_nowait, {"type": "text", "text": text})
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
                            gm = response.candidates[0].grounding_metadata
                            for q in (gm.web_search_queries or []):
                                if q and q not in search_queries:
                                    search_queries.append(q)
                            if hasattr(gm, 'grounding_chunks') and gm.grounding_chunks:
                                for gc in gm.grounding_chunks:
                                    web = getattr(gc, "web", None)
                                    if web:
                                        uri = getattr(web, "uri", "") or ""
                                        title = getattr(web, "title", "") or uri
                                        if uri and uri not in seen_urls:
                                            seen_urls.add(uri)
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
    if uid and business_id and prd_text and "NOT_READY" not in prd_text:
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

BOLT_TASK_TEMPLATE = """You are an AI assistant operating a remote browser. Your task is to use the AI app builder website "bolt.new" to build a SaaS application for the user. Bolt.new is an AI app builder itself; your goal is to interact with its AI interface until the app is fully built according to these requirements.

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


def _steel_screenshot(steel_client, session_id: str) -> str:
    """Take a screenshot via Steel Computer API, return base64 string."""
    resp = steel_client.sessions.computer(session_id, action="take_screenshot")
    return resp.base64_image or ""


def _execute_action_steel(steel_client, session_id: str, fname: str, args: dict,
                          sw: int, sh: int) -> tuple:
    """
    Execute a Gemini Computer Use action via Steel.dev Computer API.
    Returns (base64_image: str, navigated_url: str | None).
    """
    def comp(action, **kw):
        return steel_client.sessions.computer(session_id, action=action, **kw)

    navigated_url = None

    if fname in ("open_web_browser", "navigate"):
        url = args.get("url", "https://bolt.new")
        navigated_url = url
        comp("press_key", keys=["Control", "l"])
        time.sleep(0.3)
        comp("type_text", text=url)
        time.sleep(0.2)
        comp("press_key", keys=["Return"])
        time.sleep(3.0)

    elif fname == "wait_5_seconds":
        time.sleep(5)

    elif fname == "go_back":
        comp("press_key", keys=["Alt", "ArrowLeft"])
        time.sleep(1.0)

    elif fname == "go_forward":
        comp("press_key", keys=["Alt", "ArrowRight"])
        time.sleep(1.0)

    elif fname == "search":
        navigated_url = "https://www.google.com"
        comp("press_key", keys=["Control", "l"])
        time.sleep(0.2)
        comp("type_text", text="https://www.google.com")
        comp("press_key", keys=["Return"])
        time.sleep(2.0)

    elif fname == "click_at":
        dx = _denorm_x(args["x"], sw)
        dy = _denorm_y(args["y"], sh)
        comp("click_mouse", button="left", coordinates=[dx, dy])
        time.sleep(0.4)

    elif fname == "hover_at":
        dx = _denorm_x(args["x"], sw)
        dy = _denorm_y(args["y"], sh)
        comp("move_mouse", coordinates=[dx, dy])
        time.sleep(0.2)

    elif fname == "type_text_at":
        dx = _denorm_x(args["x"], sw)
        dy = _denorm_y(args["y"], sh)
        comp("click_mouse", button="left", coordinates=[dx, dy])
        time.sleep(0.3)
        if args.get("clear_before_typing", True):
            comp("press_key", keys=["Control", "a"])
            time.sleep(0.1)
            comp("press_key", keys=["Backspace"])
            time.sleep(0.1)
        comp("type_text", text=args.get("text", ""))
        time.sleep(0.2)
        if args.get("press_enter", True):
            comp("press_key", keys=["Return"])
            time.sleep(0.5)

    elif fname == "key_combination":
        keys_str = args.get("keys", "")
        keys_list = [k.strip() for k in keys_str.split("+")] if "+" in keys_str else [keys_str]
        comp("press_key", keys=keys_list)
        time.sleep(0.3)

    elif fname == "scroll_document":
        direction = args.get("direction", "down")
        key = "PageDown" if direction in ("down", "right") else "PageUp"
        comp("press_key", keys=[key])
        time.sleep(0.3)

    elif fname == "scroll_at":
        dx = _denorm_x(args.get("x", 500), sw)
        dy_coord = _denorm_y(args.get("y", 500), sh)
        direction = args.get("direction", "down")
        mag = int(args.get("magnitude", 800) / 1000 * sh)
        delta_y = mag if direction == "down" else (-mag if direction == "up" else 0)
        delta_x = mag if direction == "right" else (-mag if direction == "left" else 0)
        comp("scroll", coordinates=[dx, dy_coord], delta_x=delta_x, delta_y=delta_y)
        time.sleep(0.3)

    elif fname == "drag_and_drop":
        sx = _denorm_x(args["x"], sw)
        sy = _denorm_y(args["y"], sh)
        ex = _denorm_x(args["destination_x"], sw)
        ey = _denorm_y(args["destination_y"], sh)
        comp("drag_mouse", path=[[sx, sy], [ex, ey]])
        time.sleep(0.3)

    img = _steel_screenshot(steel_client, session_id)
    return img, navigated_url


def _run_build(send, recv_q, prd_text: str, app_name: str):
    """
    Runs the entire build pipeline in a background thread.
    Uses Steel.dev Computer API for all browser interactions (no Playwright CDP).
    `send(type, **kwargs)` queues an event to the websocket.
    `recv_q` receives dict messages from the websocket.
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
        from google.genai import types as gt
    except ImportError as e:
        send("log", text=f"Missing dependency: {e}. Run: pip install steel-sdk", level="error")
        return

    if not STEEL_API_KEY:
        send("log", text="STEEL_API_KEY not configured.", level="error")
        return

    steel = Steel(steel_api_key=STEEL_API_KEY)
    session = None
    SW, SH = 1280, 768   # Steel.dev default viewport

    try:
        session = steel.sessions.create(
            dimensions={"width": SW, "height": SH},
            block_ads=True,
            api_timeout=900000,  # 15 min — hobby plan max
        )
        sid = session.id
        send("log", text=f"Steel.dev session {sid[:8]}… ready.")
        send("url", url="https://bolt.new")
        send("browser_loading", loading=True)

        # Navigate to bolt.new via keyboard — no CDP needed
        steel.sessions.computer(sid, action="press_key", keys=["Control", "l"])
        time.sleep(0.3)
        steel.sessions.computer(sid, action="type_text", text="https://bolt.new")
        time.sleep(0.2)
        steel.sessions.computer(sid, action="press_key", keys=["Return"])
        time.sleep(4.0)  # wait for bolt.new to load

        img_b64 = _steel_screenshot(steel, sid)
        img_bytes = base64.b64decode(img_b64) if img_b64 else b""
        send("log", text=f"Initial screenshot: {len(img_bytes)} bytes")
        send("browser_loading", loading=False)
        if img_b64:
            send("screenshot", data=img_b64)

        current_url = "https://bolt.new"

        # ── Gemini Computer Use agent loop ─────────────────────────────────
        # Only include the screenshot if Steel returned valid image data;
        # an empty/invalid image causes 400 INVALID_ARGUMENT from the API.
        initial_parts = [gt.Part(text=task)]
        if len(img_bytes) > 1000:  # sanity-check: real PNG is at least a few KB
            initial_parts.append(gt.Part.from_bytes(data=img_bytes, mime_type="image/png"))
        else:
            send("log", text="No valid initial screenshot — agent will navigate first.")

        contents = [gt.Content(role="user", parts=initial_parts)]

        config = gt.GenerateContentConfig(
            safety_settings=[
                gt.SafetySetting(category=gt.HarmCategory.HARM_CATEGORY_HATE_SPEECH,        threshold=gt.HarmBlockThreshold.BLOCK_NONE),
                gt.SafetySetting(category=gt.HarmCategory.HARM_CATEGORY_HARASSMENT,          threshold=gt.HarmBlockThreshold.BLOCK_NONE),
                gt.SafetySetting(category=gt.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,   threshold=gt.HarmBlockThreshold.BLOCK_NONE),
                gt.SafetySetting(category=gt.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,   threshold=gt.HarmBlockThreshold.BLOCK_NONE),
            ],
            # ComputerUse cannot be combined with custom FunctionDeclarations — keep it alone
            tools=[
                gt.Tool(computer_use=gt.ComputerUse(
                    environment=gt.Environment.ENVIRONMENT_BROWSER,
                )),
            ],
        )

        MAX_TURNS = 50
        paused = False

        for turn in range(MAX_TURNS):
            send("log", text=f"Agent turn {turn + 1}/{MAX_TURNS}…")

            # Handle user interactions / pause before each AI turn
            while not recv_q.empty() or paused:
                try:
                    msg = recv_q.get(timeout=0.2) if paused else recv_q.get_nowait()
                    m_type = msg.get("type")

                    if m_type == "pause":
                        paused = True
                        send("log", text="Agent paused. Waiting for user…")
                    elif m_type == "resume":
                        paused = False
                        send("log", text="Agent resuming…")
                    elif m_type == "chat":
                        text = msg.get("text", "")
                        contents.append(gt.Content(role="user", parts=[
                            gt.Part(text=f"User chat message: {text}")
                        ]))
                        send("log", text=f"You: {text}", level="user_msg")
                        paused = False
                    elif m_type == "click":
                        img, nav = _execute_action_steel(steel, sid, "click_at", msg, SW, SH)
                        if nav:
                            current_url = nav
                        send("screenshot", data=img)
                        img_bytes = base64.b64decode(img) if img else b""
                        contents.append(gt.Content(role="user", parts=[
                            gt.Part(text="User took control and clicked. Here is the new screen state."),
                            gt.Part.from_bytes(data=img_bytes, mime_type="image/png"),
                        ]))
                    elif m_type == "type_text":
                        steel.sessions.computer(sid, action="type_text", text=msg.get("text", ""))
                        if msg.get("press_enter"):
                            steel.sessions.computer(sid, action="press_key", keys=["Return"])
                        time.sleep(0.5)
                        img = _steel_screenshot(steel, sid)
                        send("screenshot", data=img)
                        img_bytes = base64.b64decode(img) if img else b""
                        contents.append(gt.Content(role="user", parts=[
                            gt.Part(text=f"User typed '{msg.get('text')}'. Here is the new screen state."),
                            gt.Part.from_bytes(data=img_bytes, mime_type="image/png"),
                        ]))
                    elif m_type == "disconnect":
                        send("log", text="User disconnected, halting build.")
                        return
                except queue.Empty:
                    pass

            # Retry wrapper for 429 RESOURCE_EXHAUSTED
            response = None
            for retry in range(3):
                try:
                    response = genai_client.models.generate_content(
                        model="gemini-3-flash-preview",
                        contents=contents,
                        config=config,
                    )
                    break
                except ClientError as e:
                    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                        send("log", text=f"Quota exhausted (429). Retrying in 20s… ({retry+1}/3)", level="error")
                        time.sleep(20)
                    else:
                        raise

            if not response:
                send("log", text="Max retries reached on 429. Halting.", level="error")
                break

            if not getattr(response, "candidates", None):
                send("log", text=f"No candidates. Feedback: {getattr(response, 'prompt_feedback', '?')}", level="error")
                break

            candidate = response.candidates[0]

            # Stream thoughts to the agent log, then strip them before echoing
            # content back (thought parts cannot be included in subsequent turns)
            for p in candidate.content.parts:
                if hasattr(p, "thought") and p.thought:
                    thought_text = getattr(p, "text", None) or ""
                    if thought_text:
                        send("log", text=f"💭 {thought_text[:600]}", level="thought")
            safe_parts = [
                p for p in candidate.content.parts
                if not (hasattr(p, "thought") and p.thought)
            ]
            safe_content = gt.Content(role=candidate.content.role, parts=safe_parts)
            contents.append(safe_content)

            fn_calls = [(p.function_call, p) for p in safe_parts if p.function_call]

            if not fn_calls:
                final = " ".join(p.text for p in safe_parts if getattr(p, "text", None))
                if final:
                    send("log", text=f"Agent: {final[:300]}")
                break

            new_parts = []
            for fc, _part in fn_calls:
                fname = fc.name
                fargs = dict(fc.args) if fc.args else {}
                # fc.id must be echoed back in the FunctionResponse
                fc_id = getattr(fc, "id", None)

                send("log", text=f"  → {fname}")

                result = {}
                try:
                    img, nav = _execute_action_steel(steel, sid, fname, fargs, SW, SH)
                    if nav:
                        current_url = nav
                except Exception as ex:
                    result = {"error": str(ex)}
                    send("log", text=f"  ✗ {ex}", level="error")
                    img = _steel_screenshot(steel, sid)

                send("url", url=current_url)
                if img:
                    send("screenshot", data=img)

                img_bytes = base64.b64decode(img) if img else b""
                fr_kwargs = dict(name=fname, response={"url": current_url, **result})
                if fc_id:
                    fr_kwargs["id"] = fc_id
                new_parts.append(gt.Part(function_response=gt.FunctionResponse(**fr_kwargs)))
                # Only attach screenshot if Steel returned real data (empty image → 400)
                if len(img_bytes) > 1000:
                    new_parts.append(gt.Part.from_bytes(data=img_bytes, mime_type="image/png"))

            if new_parts:
                contents.append(gt.Content(role="user", parts=new_parts))

            send("step_start", step="build", label="Building app",
                 progress=min(20 + int((turn + 1) / MAX_TURNS * 68), 88))

        send("step_done", step="build", label="Building app", progress=88)

        # ── Step 3: Deploy ─────────────────────────────────────────────────
        send("step_start", step="deploy", label="Deploying", progress=90)
        send("log", text="Build complete — retrieving deployment URL…")
        send("step_done", step="deploy", label="Deploying", progress=100)
        send("done", url=current_url)

    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"Full Build error: {error_msg}")
        send("log", text=f"Build error: {e}", level="error")
        send("step_start", step="build", label="Error", progress=0)
    finally:
        if session:
            try:
                steel.sessions.release(session.id)
                send("log", text="Steel.dev session released.")
            except Exception:
                pass


@app.websocket("/api/ws/build")
async def build_websocket(websocket: WebSocket):
    await websocket.accept()

    if not genai_client or not STEEL_API_KEY:
        await websocket.send_text(json.dumps({'type':'error','message':'GEMINI_API_KEY or STEEL_API_KEY missing'}))
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    prd_text = data.get("prd_text", "")
    app_name = data.get("app_name", "My App")

    recv_q = queue.Queue()
    loop = asyncio.get_running_loop()

    def send(evt_type, **kwargs):
        payload = json.dumps({"type": evt_type, **kwargs})
        try:
            asyncio.run_coroutine_threadsafe(websocket.send_text(payload), loop)
        except Exception:
            pass

    def run():
        import sys, asyncio
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            loop_instance = asyncio.ProactorEventLoop()
            asyncio.set_event_loop(loop_instance)
        try:
            _run_build(send, recv_q, prd_text, app_name)
        except Exception as e:
            import traceback
            error_msg = traceback.format_exc()
            print(f"Runner error: {error_msg}")
            send("log", text=f"Build error: {e}\n{error_msg}", level="error")
        finally:
            send("ws_done")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    try:
        while thread.is_alive():
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
                recv_q.put(msg)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        recv_q.put({"type": "disconnect"})
    except Exception as e:
        print("WS error:", e)
