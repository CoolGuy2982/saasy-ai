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

GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
STEEL_API_KEY      = os.environ.get("STEEL_API_KEY", "")
STITCH_API_KEY     = os.environ.get("STITCH_API_KEY", "")

# ── Firebase Admin SDK ─────────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, auth as fb_auth, firestore as fb_fs, storage as fb_storage

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
        firebase_admin.initialize_app(credentials.Certificate(_sa), {
            "storageBucket": "saasy-aa123.firebasestorage.app"
        })
    fs = fb_fs.client()
    storage_bucket = fb_storage.bucket()
except Exception as _e:
    print(f"Firebase Admin init failed: {_e}")
    fb_auth = None
    fs = None
    storage_bucket = None

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

def _fs_get_user_doc(uid: str) -> dict:
    """Return the user's Firestore document as a dict, or {}."""
    if not fs or not uid:
        return {}
    try:
        doc = fs.collection("users").document(uid).get()
        return doc.to_dict() or {}
    except Exception:
        return {}

def _fs_get_steel_profile(uid: str) -> str | None:
    """Return the user's saved Steel.dev profile ID from Firestore, or None."""
    return _fs_get_user_doc(uid).get("steelProfileId")

def _fs_browser_setup_done(uid: str) -> bool:
    """Return True only if the user has explicitly completed the browser sign-in setup."""
    return bool(_fs_get_user_doc(uid).get("browserSigninSetup"))

def _fs_save_steel_profile(uid: str, profile_id: str):
    """Persist the user's Steel.dev profile ID to Firestore (called by builds)."""
    if not fs or not uid or not profile_id:
        return
    try:
        fs.collection("users").document(uid).set(
            {"steelProfileId": profile_id, "steelProfileUpdatedAt": fb_fs.SERVER_TIMESTAMP},
            merge=True
        )
    except Exception as e:
        print(f"Failed saving Steel profile: {e}")

def _fs_mark_browser_setup_done(uid: str, profile_id: str | None):
    """Mark that the user has explicitly completed browser sign-in setup."""
    if not fs or not uid:
        return
    try:
        payload = {"browserSigninSetup": True, "browserSigninAt": fb_fs.SERVER_TIMESTAMP}
        if profile_id:
            payload["steelProfileId"] = profile_id
        fs.collection("users").document(uid).set(payload, merge=True)
    except Exception as e:
        print(f"Failed marking browser setup: {e}")

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


@app.get("/marketing", response_class=HTMLResponse)
async def marketing_page(request: Request):
    return templates.TemplateResponse("marketing.html", {"request": request})


@app.get("/marketing/reels", response_class=HTMLResponse)
async def marketing_reels_page(request: Request):
    return templates.TemplateResponse("marketing_reels.html", {"request": request})


@app.get("/marketing/email", response_class=HTMLResponse)
async def marketing_email_page(request: Request):
    return templates.TemplateResponse("marketing_email.html", {"request": request})


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


# ── Browser sign-in setup endpoints ────────────────────────────────────────

@app.get("/api/browser-profile")
async def browser_profile(request: Request):
    """
    Return whether the user has explicitly completed the browser sign-in setup.
    A profile saved automatically by a build does NOT count — only an explicit
    user-initiated setup via the dashboard modal sets browserSigninSetup=True.
    """
    uid = _get_uid(request)
    setup_done = _fs_browser_setup_done(uid) if uid else False
    profile_id = _fs_get_steel_profile(uid) if uid else None
    return JSONResponse({"hasProfile": setup_done, "profileId": profile_id})


@app.delete("/api/browser-profile")
async def reset_browser_profile(request: Request):
    """Clear the browserSigninSetup flag so the setup modal shows again."""
    uid = _get_uid(request)
    if uid and fs:
        try:
            from google.cloud.firestore_v1 import DELETE_FIELD
            fs.collection("users").document(uid).update({
                "browserSigninSetup": DELETE_FIELD,
            })
        except Exception:
            pass
    return JSONResponse({"ok": True})


@app.post("/api/browser-setup/start")
async def browser_setup_start(request: Request):
    """
    Create a Steel session with persist_profile=True and return its debugUrl
    so the frontend can embed it as an interactive iframe. The user signs into
    Google, GitHub, bolt.new etc. directly in the iframe (no automation flags
    visible to those sites from the interaction layer).
    """
    uid = _get_uid(request)

    try:
        from steel import Steel
    except ImportError:
        return JSONResponse({"error": "steel-sdk not installed"}, status_code=500)

    if not STEEL_API_KEY:
        return JSONResponse({"error": "STEEL_API_KEY not configured"}, status_code=500)

    steel = Steel(steel_api_key=STEEL_API_KEY)

    existing_profile_id = _fs_get_steel_profile(uid) if uid else None

    try:
        session_kwargs = dict(
            persist_profile=True,
            use_proxy=True,
            solve_captcha=True,
            dimensions={"width": 1280, "height": 768},
            api_timeout=900000,  # 15 min (hobby plan max)
        )
        if existing_profile_id:
            session_kwargs["profile_id"] = existing_profile_id
        session = steel.sessions.create(**session_kwargs)
    except Exception:
        # Fallback: no proxy/captcha (may not be on paid plan)
        try:
            session_kwargs = dict(
                persist_profile=True,
                dimensions={"width": 1280, "height": 768},
                api_timeout=900000,
            )
            if existing_profile_id:
                session_kwargs["profile_id"] = existing_profile_id
            session = steel.sessions.create(**session_kwargs)
        except Exception as e:
            import traceback; traceback.print_exc()
            return JSONResponse({"error": str(e)}, status_code=500)

    debug_url = getattr(session, "debug_url", None) or getattr(session, "debugUrl", None) or ""

    return JSONResponse({
        "sessionId": str(session.id),
        "debugUrl": debug_url,
        "profileId": getattr(session, "profile_id", None),
    })


@app.post("/api/browser-setup/finish")
async def browser_setup_finish(request: Request):
    """
    Release the Steel session so the profile is uploaded, then mark the user's
    browserSigninSetup flag in Firestore.
    """
    uid = _get_uid(request)
    body = await request.json()
    session_id = body.get("sessionId")

    if not session_id:
        return JSONResponse({"error": "sessionId required"}, status_code=400)

    try:
        from steel import Steel
    except ImportError:
        return JSONResponse({"error": "steel-sdk not installed"}, status_code=500)

    steel = Steel(steel_api_key=STEEL_API_KEY)
    profile_id = None
    try:
        steel.sessions.release(session_id)
        # profile_id is only assigned AFTER release — poll until it appears (up to 15s)
        for _ in range(15):
            try:
                sess = steel.sessions.retrieve(session_id)
                profile_id = getattr(sess, "profile_id", None)
                if profile_id:
                    break
            except Exception:
                pass
            time.sleep(1)
        # Final fallback: if retrieve never returned a profile_id, try listing profiles
        if not profile_id:
            try:
                profiles = steel.profiles.list()
                for p in (profiles.profiles if hasattr(profiles, "profiles") else profiles):
                    if getattr(p, "source_session_id", None) == session_id:
                        profile_id = getattr(p, "id", None)
                        break
            except Exception:
                pass
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    if uid:
        _fs_mark_browser_setup_done(uid, profile_id)
        print(f"[browser-setup/finish] uid={uid} profile_id={profile_id}")

    return JSONResponse({"ok": True, "profileId": profile_id})


@app.post("/api/browser-setup/navigate")
async def browser_setup_navigate(request: Request):
    """Navigate the live setup session to a URL via the Steel computer API."""
    body = await request.json()
    session_id = body.get("sessionId")
    url = body.get("url", "")

    if not session_id or not url:
        return JSONResponse({"error": "sessionId and url required"}, status_code=400)

    try:
        from steel import Steel
    except ImportError:
        return JSONResponse({"error": "steel-sdk not installed"}, status_code=500)

    steel = Steel(steel_api_key=STEEL_API_KEY)
    try:
        steel.sessions.computer(session_id, action="navigate", url=url)
    except Exception:
        # Fallback: use address bar
        try:
            steel.sessions.computer(session_id, action="press_key", keys=["Control", "l"])
            time.sleep(0.15)
            steel.sessions.computer(session_id, action="type_text", text=url)
            steel.sessions.computer(session_id, action="press_key", keys=["Return"])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"ok": True})


# ── Stitch MCP helpers ─────────────────────────────────────────────────────

STITCH_MCP_URL = "https://stitch.googleapis.com/mcp"

_stitch_session_id: str = ""   # module-level cache — reused across calls in a build


def _stitch_rpc(method: str, params: dict, api_key: str, timeout: int = 90) -> dict:
    """Raw JSON-RPC 2.0 POST to the Stitch MCP endpoint. Returns the full response body."""
    import urllib.request, urllib.error
    global _stitch_session_id
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
    }
    if _stitch_session_id:
        headers["mcp-session-id"] = _stitch_session_id
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params,
    }).encode()
    req = urllib.request.Request(STITCH_MCP_URL, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Capture session ID from response header if provided
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            if sid:
                _stitch_session_id = sid
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Stitch MCP HTTP {e.code}: {e.read().decode()[:300]}")


def _stitch_parse_result(body: dict) -> dict:
    """Extract the actual result from a JSON-RPC response body."""
    if "error" in body:
        raise RuntimeError(f"Stitch JSON-RPC error: {body['error']}")
    result = body.get("result", body)
    # MCP tools/call wraps content in result.content[0].text
    if isinstance(result, dict) and "content" in result:
        content = result["content"]
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "")
            try:
                parsed = json.loads(text)
                parsed["_raw"] = text
                return parsed
            except (json.JSONDecodeError, TypeError):
                return {"_raw": text}
    return result


def _stitch_init(api_key: str) -> list[dict]:
    """
    MCP initialization handshake + tools/list.
    Returns the list of available tools (with names and input schemas).
    """
    global _stitch_session_id
    _stitch_session_id = ""   # reset so init goes without stale session header

    # 1. initialize
    body = _stitch_rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "saasy-ai", "version": "1.0"},
    }, api_key)
    # 2. initialized notification (best-effort)
    try:
        _stitch_rpc("notifications/initialized", {}, api_key)
    except Exception:
        pass
    # 3. list tools
    tlist = _stitch_rpc("tools/list", {}, api_key)
    return tlist.get("result", {}).get("tools", [])


def _stitch_call(tool_name: str, arguments: dict, api_key: str, timeout: int = 90) -> dict:
    """Call a Stitch MCP tool and return the parsed result dict."""
    body = _stitch_rpc("tools/call", {
        "name": tool_name,
        "arguments": arguments,
    }, api_key, timeout=timeout)
    return _stitch_parse_result(body)



def _stitch_extract_id(result: dict, *keys: str) -> str:
    """Try JSON keys first, then regex on the raw text response."""
    import re
    for k in keys:
        v = result.get(k, "")
        if v and isinstance(v, str):
            return v
    raw = result.get("_raw", "")
    if raw:
        for k in keys:
            m = re.search(rf'["\']?{k}["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-]+)["\']?', raw)
            if m:
                return m.group(1)
    return ""


def _design_with_stitch(genai_client, prd_text: str, app_name: str, send) -> list[dict]:
    """
    Call Stitch MCP directly (no Gemini function-calling loop).
    Returns list of {name, html, screenshot} dicts, one per generated screen.
    """
    # Step 0: Initialize MCP session and discover available tools
    send("log", text="  Stitch: initializing MCP session…")
    try:
        tools = _stitch_init(STITCH_API_KEY)
        tool_names = [t.get("name", "") for t in tools]
        send("log", text=f"  Stitch: {len(tool_names)} tools ready.")
    except Exception as e:
        send("log", text=f"  Stitch init failed: {e}", level="error")
        return []

    # Resolve the actual tool names from the server's list
    def find_tool(candidates: list[str]) -> str:
        for c in candidates:
            if c in tool_names:
                return c
        return candidates[0]  # fall back to first guess

    tool_create  = find_tool(["create_project", "createProject", "stitch_create_project"])
    tool_gen     = find_tool(["generate_screen_from_text", "generateScreen", "stitch_generate_screen"])

    # Step 1: Create project (title = correct arg per schema)
    send("log", text="  Stitch: creating project…")
    try:
        proj = _stitch_call(tool_create, {"title": app_name}, STITCH_API_KEY)
        send("log", text=f"  Stitch raw: {str(proj.get('_raw', proj))[:400]}")
        # Response may be resource name "projects/12345" or a numeric projectId
        resource_name = _stitch_extract_id(proj, "name", "projectId", "project_id", "id")
        # Extract numeric/slug portion from "projects/{id}" resource name
        if resource_name and "/" in resource_name:
            project_id = resource_name.split("/")[-1]
        else:
            project_id = resource_name
        if not project_id:
            send("log", text="  Stitch: could not find projectId in response — skipping.", level="warn")
            return []
        send("log", text=f"  Stitch: project → {project_id}")
    except Exception as e:
        send("log", text=f"  Stitch create_project failed: {e}", level="error")
        return []

    # Step 2: Decide screen prompts (Gemini text generation, no function calling)
    screen_specs = [
        ("Landing Page",
         f"Modern SaaS landing page for '{app_name}'. Hero headline, key feature cards, "
         f"call-to-action button. Clean white background, accent color, Inter font."),
        ("Dashboard",
         f"Main user dashboard for '{app_name}'. Sidebar navigation, KPI metric cards, "
         f"data table or chart, top bar with user avatar. Professional light theme."),
    ]
    if genai_client and prd_text:
        try:
            resp = genai_client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=(
                    "Write exactly 2 short UI design prompts for Google Stitch based on this PRD.\n"
                    "Format: one per line, exactly like: SCREEN: <name> | PROMPT: <visual description>\n"
                    "Be specific about layout, colors, and key UI elements.\n\n"
                    f"PRD:\n{prd_text[:2000]}"
                ),
            )
            parsed = []
            for line in resp.text.strip().splitlines():
                if "SCREEN:" in line and "PROMPT:" in line:
                    name_part = line.split("SCREEN:", 1)[1].split("|")[0].strip()
                    prompt_part = line.split("PROMPT:", 1)[1].strip()
                    if name_part and prompt_part:
                        parsed.append((name_part, prompt_part))
            if len(parsed) >= 2:
                screen_specs = parsed[:2]
        except Exception:
            pass  # Fall back to defaults above

    # Step 3: Generate each screen then fetch full Screen object for HTML + screenshot
    import re as _re, base64 as _b64, urllib.request as _urlreq
    tool_get      = find_tool(["get_screen", "getScreen", "stitch_get_screen"])
    tool_list_scr = find_tool(["list_screens", "listScreens", "stitch_list_screens"])

    def _download_b64(url: str) -> str:
        """Download a URL and return base64-encoded bytes."""
        with _urlreq.urlopen(url, timeout=30) as r:
            return _b64.b64encode(r.read()).decode()

    def _extract_from_components(gen: dict) -> tuple[str, str, str]:
        """
        Walk outputComponents and return (screenshot_url, html_url, screen_name).
        Screen object fields per docs: screenshot.downloadUrl, htmlCode.downloadUrl, name.
        """
        for comp in gen.get("outputComponents", []):
            for scr in comp.get("design", {}).get("screens", []):
                shot_url = scr.get("screenshot", {}).get("downloadUrl", "")
                html_url = scr.get("htmlCode", {}).get("downloadUrl", "")
                sname    = scr.get("name", "")  # "projects/{p}/screens/{s}"
                if shot_url or sname:
                    return shot_url, html_url, sname
        # Fallback: regex raw JSON (handles truncated nested structures)
        raw = gen.get("_raw", "")
        shot_url = ""
        m = _re.search(r'"downloadUrl"\s*:\s*"(https://[^"]+(?:image|png|jpg)[^"]*)"', raw)
        if m:
            shot_url = m.group(1)
        sname = ""
        m2 = _re.search(r'"name"\s*:\s*"(projects/[^"]+/screens/[^"]+)"', raw)
        if m2:
            sname = m2.group(1)
        return shot_url, "", sname

    screens: list[dict] = []
    known_screen_ids: set[str] = set()

    for screen_name, prompt in screen_specs:
        send("design_generating", name=screen_name)
        send("log", text=f"  Stitch: generating '{screen_name}'…")
        try:
            gen = _stitch_call(tool_gen, {
                "projectId": project_id,
                "prompt": prompt,
                "modelId": "GEMINI_3_1_PRO",
            }, STITCH_API_KEY, timeout=180)

            shot_url, html_url, screen_resource = _extract_from_components(gen)

            # If no direct screenshot URL, fall back to list_screens → get_screen
            if not shot_url and not screen_resource:
                send("log", text=f"  Stitch: no screen in generate response — trying list_screens…")
                try:
                    listed = _stitch_call(tool_list_scr, {"projectId": project_id}, STITCH_API_KEY)
                    scr_list = listed.get("screens", listed.get("items", []))
                    for s in scr_list:
                        sname = s.get("name", "")
                        if sname and sname not in known_screen_ids:
                            screen_resource = sname
                            break
                except Exception as le:
                    send("log", text=f"  Stitch: list_screens failed: {le}", level="warn")

            # Fetch full screen object via get_screen if we have a resource name
            if screen_resource and (not shot_url):
                parts = screen_resource.split("/")
                # name format: projects/{p}/screens/{s}
                scr_id = parts[-1] if len(parts) >= 2 else ""
                try:
                    got = _stitch_call(tool_get, {
                        "name": screen_resource,
                        "projectId": project_id,
                        "screenId": scr_id,
                    }, STITCH_API_KEY)
                    shot_url  = got.get("screenshot", {}).get("downloadUrl", "") or shot_url
                    html_url  = got.get("htmlCode",   {}).get("downloadUrl", "") or html_url
                except Exception as ge:
                    send("log", text=f"  Stitch: get_screen failed: {ge}", level="warn")

            if not shot_url:
                send("log", text=f"  Stitch: no screenshot URL for '{screen_name}'.", level="warn")
                continue

            # Download screenshot
            send("log", text=f"  Stitch: downloading '{screen_name}' screenshot…")
            try:
                shot_b64 = _download_b64(shot_url)
            except Exception as dl_err:
                send("log", text=f"  Stitch: screenshot download failed: {dl_err}", level="error")
                continue

            # Download HTML (best-effort)
            screen_html = ""
            if html_url:
                try:
                    with _urlreq.urlopen(html_url, timeout=30) as hr:
                        screen_html = hr.read().decode("utf-8", errors="replace")
                except Exception:
                    pass

            if screen_resource:
                known_screen_ids.add(screen_resource)
            screens.append({"name": screen_name, "html": screen_html, "screenshot": shot_b64})
            send("log", text=f"  Stitch: '{screen_name}' ready")
            send("design_screen", name=screen_name, data=shot_b64, html=screen_html)

        except Exception as e:
            send("log", text=f"  Stitch generate_screen failed: {e}", level="error")

    return screens


# ── /api/build ─────────────────────────────────────────────────────────────
# Drives the full build pipeline:
#   1. Fetch PRD text from the Gemini interaction store
#   2. Spin up a Steel.dev remote browser session
#   3. Run a Gemini Computer Use agent loop targeting Bolt.new
#   4. Stream SSE events (step updates, logs, screenshots) back to build.html
# ---------------------------------------------------------------------------

BOLT_TASK_TEMPLATE = """You are an AI agent operating a remote browser on bolt.new. The product requirements have already been submitted to bolt.new. Bolt is now generating the application.

Your job:
1. Watch the screen. Bolt.new will show a file tree and code editor as it builds. Wait for it to finish — this takes 1–3 minutes. Do NOT type anything or click the input box.
2. If bolt.new asks a clarifying question or shows a prompt asking what to build, it means submission failed — in that case navigate to bolt.new, click the center text input, type the requirements below, and click Build now:

--- REQUIREMENTS (only use if bolt.new hasn't started building yet) ---
{prd}
--- END REQUIREMENTS ---

3. Once building is complete (file tree is visible and the spinner stops), look for a "Deploy" button (usually top-right or in the toolbar). Click it.
4. Wait for deployment to finish, then report the final live URL.

Important rules:
- Do NOT retype or resubmit the requirements unless bolt.new clearly hasn't started building.
- If you see a loading spinner or streaming code, just wait — do not interrupt.
- If you see a preview URL or deployed URL, report it immediately."""




def _denorm_x(x: int, w: int) -> int:
    return int(x / 1000 * w)


def _denorm_y(y: int, h: int) -> int:
    return int(y / 1000 * h)


def _steel_screenshot(steel_client, session_id: str, compress: bool = False) -> str:
    """Take a screenshot via Steel Computer API, return base64 string.

    If compress=True, downsample to 50% and convert to JPEG to cut vision
    token usage ~4-6x before sending to the Gemini Computer Use API.
    """
    resp = steel_client.sessions.computer(session_id, action="take_screenshot")
    b64 = resp.base64_image or ""
    if not b64 or not compress:
        return b64
    try:
        from PIL import Image
        import io
        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        img = img.resize((w // 2, h // 2), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64  # PIL not available or failed — return original


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


def _run_build(send, recv_q, prd_text: str, app_name: str, uid: str | None = None):
    """
    Runs the entire build pipeline in a background thread.
    Uses Steel.dev Computer API for all browser interactions (no Playwright CDP).
    `send(type, **kwargs)` queues an event to the websocket.
    `recv_q` receives dict messages from the websocket.
    `uid` is the Firebase UID used to load/save the persistent Steel browser profile.
    """
    # ── Step 1: Plan ───────────────────────────────────────────────────────
    send("step_start", step="plan", label="Planning", progress=5)
    if prd_text:
        send("log", text=f"PRD loaded ({len(prd_text)} chars).")
    else:
        send("log", text="No PRD provided — using app name as prompt.")

    send("step_done", step="plan", label="Planning", progress=12)

    # ── Step 2: Stitch UI design ───────────────────────────────────────────
    send("step_start", step="github", label="Designing UI", progress=14)
    stitch_screens: list[dict] = []

    if STITCH_API_KEY and genai_client:
        send("log", text="Generating UI designs with Google Stitch MCP…")
        try:
            stitch_screens = _design_with_stitch(genai_client, prd_text, app_name, send)
            send("log", text=f"Stitch: {len(stitch_screens)} screen(s) designed.")
        except Exception as _se:
            send("log", text=f"Stitch skipped: {_se}", level="warn")
    else:
        send("log", text="STITCH_API_KEY not set — skipping UI design step.")

    send("step_done", step="github", label="Designing UI", progress=18)

    # Build the bolt.new task — include Stitch HTML as design context
    prd_for_task = prd_text or f"Build a simple SaaS app called '{app_name}' with Google sign-in and a clean dashboard."
    design_section = ""
    if stitch_screens:
        combined_html = "\n\n".join(
            f"<!-- {s['name']} -->\n{s['html']}" for s in stitch_screens
        )
        design_section = (
            "\n\n## UI Design Reference (generated by Google Stitch)\n"
            "Match these designs closely when building the app:\n\n"
            + combined_html[:6000]
        )
    task = BOLT_TASK_TEMPLATE.format(prd=prd_for_task + design_section)

    # ── Step 3: Browser + build ────────────────────────────────────────────
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
    SW, SH = 1440, 900   # Recommended by Gemini Computer Use docs

    try:
        # Load saved browser profile (cookies, Google sign-in, etc.)
        existing_profile_id = _fs_get_steel_profile(uid) if uid else None
        print(f"[build] uid={uid} existing_profile_id={existing_profile_id}")

        session_kwargs = dict(
            dimensions={"width": SW, "height": SH},
            block_ads=True,
            api_timeout=900000,       # 15 min — hobby plan max
            persist_profile=True,     # always save profile on release
        )
        if existing_profile_id:
            session_kwargs["profile_id"] = existing_profile_id
            send("log", text=f"Restoring saved browser profile ({existing_profile_id[:8]}…)")
        else:
            send("log", text="No saved browser profile — continuing without saved credentials.")

        session = steel.sessions.create(**session_kwargs)
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
        time.sleep(5.0)  # wait for bolt.new to fully load

        # ── Direct PRD submission (no agent needed for this step) ──────────
        # bolt.new homepage: large textarea centered on screen, Build Now button bottom-right of it
        send("log", text="Submitting PRD to bolt.new prompt box…")
        prd_for_bolt = prd_text or f"Build a simple SaaS app called '{app_name}' with Google sign-in and a clean dashboard."

        # Click the prompt textarea (center of screen)
        steel.sessions.computer(sid, action="click_mouse", button="left", coordinates=[720, 454])
        time.sleep(0.6)
        # Select all & clear any existing text, then type the PRD
        steel.sessions.computer(sid, action="press_key", keys=["Control", "a"])
        time.sleep(0.1)
        steel.sessions.computer(sid, action="type_text", text=prd_for_bolt)
        time.sleep(0.8)

        # Screenshot to show the filled input
        img_b64 = _steel_screenshot(steel, sid)
        img_bytes = base64.b64decode(img_b64) if img_b64 else b""
        send("browser_loading", loading=False)
        if img_b64:
            send("screenshot", data=img_b64)

        send("log", text="PRD entered — clicking Build now…")
        # Click the "Build now" button (right side of the input bar, bottom)
        # At 1440x900: button is at approximately x=1050, y=537
        steel.sessions.computer(sid, action="click_mouse", button="left", coordinates=[1050, 537])
        time.sleep(3.0)
        send("log", text="Build now clicked — bolt.new is generating the app…")

        img_b64 = _steel_screenshot(steel, sid)
        img_bytes = base64.b64decode(img_b64) if img_b64 else b""
        send("log", text=f"Post-submit screenshot: {len(img_bytes)} bytes")
        if img_b64:
            send("screenshot", data=img_b64)

        current_url = "https://bolt.new"

        # ── Gemini Computer Use agent loop ─────────────────────────────────
        # Only include the screenshot if Steel returned valid image data;
        # an empty/invalid image causes 400 INVALID_ARGUMENT from the API.
        initial_parts = [gt.Part(text=task)]
        if len(img_bytes) > 1000:
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
            # A thinking-heartbeat thread sends "Still thinking…" every 15s so
            # the UI doesn't appear frozen during long Gemini response times.
            response = None
            for retry in range(3):
                send("log", text="Asking Gemini (analyzing screenshot)…")
                _thinking_stop = threading.Event()
                def _thinking_hb(stop=_thinking_stop):
                    elapsed = 0
                    while not stop.wait(timeout=15):
                        elapsed += 15
                        send("log", text=f"Still thinking… ({elapsed}s)")
                threading.Thread(target=_thinking_hb, daemon=True).start()

                try:
                    response = genai_client.models.generate_content(
                        model="gemini-3-flash-preview",
                        contents=contents,
                        config=config,
                    )
                    _thinking_stop.set()
                    break
                except ClientError as e:
                    _thinking_stop.set()
                    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                        wait_s = 30 * (retry + 1)  # 30s, 60s, 90s back-off
                        send("log", text=f"Quota exhausted (429). Retrying in {wait_s}s… ({retry+1}/3)", level="error")
                        time.sleep(wait_s)
                    else:
                        raise
                except Exception:
                    _thinking_stop.set()
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

            # Execute all function calls, collect results.
            # Per Gemini Computer Use docs, the screenshot goes INSIDE each
            # FunctionResponse.parts — NOT as a separate Part in the user turn.
            fr_list = []   # list of (fname, fc_id, result)
            last_img = ""

            for fc, _part in fn_calls:
                fname = fc.name
                fargs = dict(fc.args) if fc.args else {}
                fc_id = getattr(fc, "id", None)

                send("log", text=f"  → {fname}")

                # Handle safety_decision — pause build for user intervention
                safety_ack = {}
                if "safety_decision" in fargs:
                    sd = fargs["safety_decision"]
                    decision = sd.get("decision", "") if isinstance(sd, dict) else str(sd)
                    explanation = sd.get("explanation", "") if isinstance(sd, dict) else ""
                    reason = explanation or "The AI flagged a step that needs your review."

                    # Get the live debug URL for this session
                    session_debug_url = getattr(session, "debug_url", None) or \
                                        getattr(session, "debugUrl", None) or ""

                    send("needs_intervention",
                         debugUrl=session_debug_url,
                         reason=f"Sign-in or action needed: {reason[:150]}")

                    # Wait for the user to click "Continue" (or disconnect)
                    while True:
                        try:
                            msg = recv_q.get(timeout=300)
                        except Exception:
                            break
                        if msg.get("type") == "continue":
                            break
                        if msg.get("type") == "disconnect":
                            return

                    send("intervention_resumed")
                    safety_ack = {"safety_acknowledgement": "true"}

                result = {}
                try:
                    img, nav = _execute_action_steel(steel, sid, fname, fargs, SW, SH)
                    if nav:
                        current_url = nav
                    if img:
                        last_img = img
                except Exception as ex:
                    result = {"error": str(ex)}
                    send("log", text=f"  ✗ {ex}", level="error")
                    img = _steel_screenshot(steel, sid)
                    if img:
                        last_img = img

                fr_list.append((fname, fc_id, {**result, **safety_ack}))

            # Stream the final screenshot to the UI
            send("url", url=current_url)
            if last_img:
                send("screenshot", data=last_img)

            # Build user turn: screenshot embedded inside each FunctionResponse.parts
            # (same screenshot in each — per the official Computer Use docs pattern)
            if fr_list:
                img_bytes = base64.b64decode(last_img) if last_img else b""
                new_parts = []
                for fname, fc_id, result in fr_list:
                    fr_kwargs = dict(name=fname, response={"url": current_url, **result})
                    if fc_id:
                        fr_kwargs["id"] = fc_id
                    if len(img_bytes) > 1000:
                        fr_kwargs["parts"] = [gt.FunctionResponsePart(
                            inline_data=gt.FunctionResponseBlob(
                                mime_type="image/png",
                                data=img_bytes,
                            )
                        )]
                    new_parts.append(gt.Part(function_response=gt.FunctionResponse(**fr_kwargs)))
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
                # After release Steel finishes uploading the profile.
                # Save the profile_id so the next build restores browser state.
                profile_id = getattr(session, "profile_id", None)
                if profile_id and uid:
                    _fs_save_steel_profile(uid, profile_id)
                    send("log", text="Browser profile saved — next build will auto sign-in.")
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
    token    = data.get("token", "")

    # Resolve Firebase UID from the auth token sent by the client
    uid = None
    if fb_auth and token:
        try:
            uid = fb_auth.verify_id_token(token)["uid"]
        except Exception:
            pass

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
            _run_build(send, recv_q, prd_text, app_name, uid)
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
        import time as _time
        last_ping = _time.monotonic()
        while thread.is_alive():
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
                recv_q.put(msg)
            except asyncio.TimeoutError:
                # Send keepalive ping every 20 s so proxies don't drop idle WS
                now = _time.monotonic()
                if now - last_ping >= 20:
                    try:
                        await websocket.send_text(json.dumps({"type": "ping"}))
                        last_ping = now
                    except Exception:
                        break
    except WebSocketDisconnect:
        recv_q.put({"type": "disconnect"})
    except Exception as e:
        print("Build WS error:", e)


# ── Marketing Reels Pipeline ───────────────────────────────────────────────

def _run_reels_pipeline(send, recv_q, target_url: str, uid: str | None = None):
    """
    1. Spin up a Steel browser pointing to target_url.
    2. Take screenshots.
    3. Generate 2 distinct Veo prompts via Gemini text model.
    4. Call Veo 3.1 API (2 videos).
    5. Upload to Firebase Storage.
    """
    send("step_start", step="scrape", label="Scraping app", progress=10)
    send("log", text=f"Starting browser session for {target_url}…")

    try:
        from steel import Steel
        from google.genai import types as gt
        import uuid
    except ImportError as e:
        send("log", text=f"Missing dependency: {e}", level="error")
        return

    if not STEEL_API_KEY or not genai_client:
        send("log", text="STEEL_API_KEY or GEMINI_API_KEY missing.", level="error")
        return

    steel = Steel(steel_api_key=STEEL_API_KEY)
    session = None
    SW, SH = 1440, 900
    screenshots = []

    try:
        send("log", text="Launching Steel.dev session…")
        session = steel.sessions.create(dimensions={"width": SW, "height": SH}, block_ads=True)
        sid = session.id
        
        send("log", text=f"Navigating to {target_url}…")
        steel.sessions.computer(sid, action="press_key", keys=["Control", "l"])
        time.sleep(0.3)
        steel.sessions.computer(sid, action="type_text", text=target_url)
        time.sleep(0.2)
        steel.sessions.computer(sid, action="press_key", keys=["Return"])
        time.sleep(6) # Wait for load

        send("log", text="Capturing UI screenshots…")
        img1 = _steel_screenshot(steel, sid)
        if img1:
            screenshots.append(img1)
            send("screenshot", data=img1)
        
        steel.sessions.computer(sid, action="scroll", coordinates=[SW//2, SH//2], delta_x=0, delta_y=600)
        time.sleep(2)
        img2 = _steel_screenshot(steel, sid)
        if img2:
            screenshots.append(img2)
            send("screenshot", data=img2)

        send("step_done", step="scrape", label="Scraping app", progress=30)
        send("step_start", step="script", label="Writing Marketing Script", progress=35)
        send("log", text="Asking Gemini 3 Flash to write marketing prompts…")

        # Use Gemini to generate Veo prompts
        content_parts = [
            gt.Part(text="""You are a world-class social media marketer.
Based on these screenshots of an app, write exactly 2 prompts for a high-quality, cinematic marketing video (8 seconds) that a text-to-video AI model (Veo 3) will generate.

Format your output exactly as a JSON array of 2 strings:
[
  "Cinematic 3D render of a smartphone displaying the app interface, floating in space with neon lighting...",
  "Close-up of a person smiling while using the app, dynamic camera pan, bright colors, happy tone..."
]""")
        ]
        for b64 in screenshots:
            content_parts.append(gt.Part.from_bytes(data=base64.b64decode(b64), mime_type="image/png"))

        gemini_res = genai_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[gt.Content(role="user", parts=content_parts)]
        )
        
        try:
            raw_text = gemini_res.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:-3]
            elif raw_text.startswith("```"):
                raw_text = raw_text[3:-3]
            prompts = json.loads(raw_text.strip())
        except Exception as e:
            send("log", text=f"Failed to parse prompts, falling back to default.", level="error")
            prompts = [
                "A dynamic cinematic fly-through of a futuristic smartphone app interface, neon highlights, 8 seconds.",
                "A professional looking promotional video showing clean UI elements popping up in 3d space, bright lighting."
            ]

        send("log", text=f"Generated Prompts:\n1: {prompts[0]}\n2: {prompts[1]}")
        send("step_done", step="script", label="Marketing Script written", progress=50)

        # ── Veo Generation ──
        send("step_start", step="video", label="Generating Video (Veo 3.1)", progress=55)
        send("log", text="Submitting Veo 3.1 operations (this may take a few minutes)...")

        # Start jobs
        operations = []
        for idx, p in enumerate(prompts[:2]):
            send("log", text=f"Starting Veo Job {idx+1}...")
            # Send the first screenshot as an image reference
            img_b64 = screenshots[0] if screenshots else None
            img_ref = None
            if img_b64:
                try:
                    img_ref = gt.Image(image_bytes=base64.b64decode(img_b64), mime_type="image/png")
                except Exception:
                    img_ref = None

            op_config = gt.GenerateVideosConfig(
                aspect_ratio="9:16",
                person_generation="allow_adult"
            )
            op = genai_client.models.generate_videos(
                model="veo-3.1-generate-preview",
                prompt=p,
                image=img_ref,
                config=op_config
            )
            operations.append((idx+1, op))

        videos_final = []
        for idx, op in operations:
            send("log", text=f"Polling Job {idx}...")
            while not op.done:
                time.sleep(10)
                try:
                    op = genai_client.operations.get(op)
                    send("log", text=f"Job {idx} thinking...")
                except Exception as e:
                    send("log", text=f"Transient API error polling job {idx}, retrying...", level="error")
            
            send("log", text=f"Job {idx} Done!")
            if hasattr(op.response, "generated_videos") and op.response.generated_videos:
                video_obj = op.response.generated_videos[0]
                videos_final.append(video_obj.video)
            else:
                send("log", text=f"Job {idx} failed to yield video.", level="error")

        send("step_done", step="video", label="Generating Video", progress=80)

        # ── Upload to Firebase ──
        send("step_start", step="upload", label="Uploading to Cloud", progress=85)
        send("log", text="Uploading videos to Firebase Storage...")
        
        final_urls = []
        if not storage_bucket:
            send("log", text="Firebase Storage not configured. Skipping upload.", level="error")
        else:
            for v_idx, v in enumerate(videos_final):
                try:
                    video_bytes = v.video_bytes
                    if not video_bytes and getattr(v, "uri", None):
                        import urllib.request
                        send("log", text=f"Downloading output video {v_idx+1} from Veo API...")
                        download_url = v.uri
                        if GEMINI_API_KEY and "key=" not in download_url:
                            sep = "&" if "?" in download_url else "?"
                            download_url = f"{download_url}{sep}alt=media&key={GEMINI_API_KEY}"
                        req = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req) as resp:
                            video_bytes = resp.read()

                    if not video_bytes:
                        raise ValueError("No video bytes or valid URI found.")
                        
                    unique_id = str(uuid.uuid4())
                    uid_str = uid if uid else "anonymous"
                    blob = storage_bucket.blob(f"marketing_reels/{uid_str}/{unique_id}.mp4")
                    blob.upload_from_string(video_bytes, content_type="video/mp4")
                    import datetime
                    signed_url = blob.generate_signed_url(
                        expiration=datetime.timedelta(days=7),
                        method="GET",
                        version="v4",
                    )
                    final_urls.append(signed_url)
                    send("log", text=f"Video {v_idx+1} uploaded!")
                except Exception as e:
                    send("log", text=f"Failed to upload video {v_idx+1}: {e}", level="error")
        
        send("step_done", step="upload", label="Finished", progress=100)
        send("done", urls=final_urls)

    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"Reels error: {error_msg}")
        send("log", text=f"Reels build error: {e}", level="error")
        send("step_start", step="video", label="Error", progress=0)
    finally:
        if session:
            try:
                steel.sessions.release(session.id)
            except Exception:
                pass


@app.websocket("/api/ws/marketing/reels")
async def marketing_reels_websocket(websocket: WebSocket):
    await websocket.accept()

    if not genai_client or not STEEL_API_KEY:
        await websocket.send_text(json.dumps({'type':'error','message':'API keys missing'}))
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    target_url = data.get("target_url", "https://calcgpt.ai")
    token = data.get("token", "")

    uid = None
    if fb_auth and token:
        try:
            uid = fb_auth.verify_id_token(token)["uid"]
        except Exception:
            pass

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
            _run_reels_pipeline(send, recv_q, target_url, uid)
        except Exception as e:
            import traceback
            error_msg = traceback.format_exc()
            send("log", text=f"Pipeline error: {e}\n{error_msg}", level="error")
        finally:
            send("ws_done")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    try:
        import time as _time
        last_ping = _time.monotonic()
        while thread.is_alive():
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
                recv_q.put(msg)
            except asyncio.TimeoutError:
                now = _time.monotonic()
                if now - last_ping >= 20:
                    try:
                        await websocket.send_text(json.dumps({"type": "ping"}))
                        last_ping = now
                    except Exception:
                        break
    except WebSocketDisconnect:
        recv_q.put({"type": "disconnect"})
    except Exception as e:
        print("WS error:", e)


# ── Emailer Feature ────────────────────────────────────────────────────────

import re as _re
import csv as _csv
import io as _io
import uuid as _uuid_mod

EXA_API_KEY         = os.environ.get("EXA_API_KEY", "")
GMAIL_CLIENT_ID     = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REDIRECT_URI  = os.environ.get("GMAIL_REDIRECT_URI", "http://localhost:8000/api/gmail/callback")

# ── Gmail / Firestore helpers ───────────────────────────────

def _fs_get_gmail_tokens(uid: str) -> dict:
    if not fs or not uid:
        return {}
    try:
        doc = (fs.collection("users").document(uid)
                 .collection("private").document("gmailTokens").get())
        return doc.to_dict() or {}
    except Exception:
        return {}


def _fs_save_gmail_tokens(uid: str, tokens: dict):
    if not fs or not uid:
        return
    try:
        (fs.collection("users").document(uid)
           .collection("private").document("gmailTokens")
           .set(tokens, merge=True))
    except Exception as e:
        print(f"Failed saving Gmail tokens: {e}")


def _get_gmail_service(uid: str):
    """Return authenticated Gmail API service for uid, or None."""
    tokens = _fs_get_gmail_tokens(uid)
    if not tokens.get("refresh_token"):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleRequest
        from googleapiclient.discovery import build as google_build

        creds = Credentials(
            token=tokens.get("access_token"),
            refresh_token=tokens["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GMAIL_CLIENT_ID,
            client_secret=GMAIL_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        if not creds.valid:
            creds.refresh(GoogleRequest())
            _fs_save_gmail_tokens(uid, {
                "access_token": creds.token,
                "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
            })
        return google_build("gmail", "v1", credentials=creds)
    except Exception as e:
        print(f"Gmail service error: {e}")
        return None


def _fs_save_campaign(uid: str, campaign_id: str, meta: dict):
    if not fs or not uid:
        return
    try:
        (fs.collection("users").document(uid)
           .collection("emailCampaigns").document(campaign_id)
           .set({**meta, "created_at": fb_fs.SERVER_TIMESTAMP}, merge=True))
    except Exception as e:
        print(f"Failed saving campaign: {e}")


def _fs_save_prospect(uid: str, campaign_id: str, prospect: dict):
    if not fs or not uid:
        return
    try:
        pid = prospect.get("id") or str(_uuid_mod.uuid4())
        (fs.collection("users").document(uid)
           .collection("emailCampaigns").document(campaign_id)
           .collection("prospects").document(pid)
           .set(prospect, merge=True))
    except Exception as e:
        print(f"Failed saving prospect: {e}")


# ── Exa helpers ─────────────────────────────────────────────

_EMAIL_RE   = _re.compile(r'[\w.+\-]+@[\w\-]+\.[a-z]{2,}', _re.IGNORECASE)
_EMAIL_SKIP = {"noreply", "no-reply", "support", "info", "hello",
               "contact", "admin", "privacy", "legal", "press", "team"}


def _extract_email(text: str | None) -> tuple[str, str]:
    if not text:
        return "", "missing"
    for m in _EMAIL_RE.findall(text):
        if m.split("@")[0].lower() not in _EMAIL_SKIP:
            return m, "direct"
    return "", "missing"


def _exa_to_prospects(results, category: str) -> list[dict]:
    out = []
    for r in results:
        text    = getattr(r, "text", "") or ""
        hl_obj  = getattr(r, "highlights", None)
        hl_text = " ".join(str(h) for h in hl_obj[:3]) if isinstance(hl_obj, list) else ""

        email, confidence = _extract_email(text or hl_text)

        title_str = getattr(r, "title", "") or ""
        name    = title_str.split(" - ")[0].strip() if " - " in title_str else title_str.split("|")[0].strip()
        job     = ""
        company = ""
        if " - " in title_str:
            parts        = title_str.split(" - ", 1)
            job_company  = parts[1].split("|")[0].strip()
            if " at " in job_company:
                job, company = job_company.split(" at ", 1)
                job     = job.strip()
                company = company.strip()
            else:
                job = job_company

        summary = (hl_text or text)[:300]
        out.append({
            "id":               str(_uuid_mod.uuid4()),
            "name":             name or "Unknown",
            "title":            job,
            "company":          company,
            "url":              getattr(r, "url", "") or "",
            "email":            email,
            "email_confidence": confidence,
            "category":         category,
            "exa_summary":      summary,
            "draft_subject":    "",
            "draft_body":       "",
            "sent":             False,
            "sent_at":          None,
        })
    return out


# ── Email pipeline ──────────────────────────────────────────

def _run_email_pipeline(send, recv_q, payload: dict, uid: str | None):
    business_desc   = payload.get("business_description", "")
    target_customer = payload.get("target_customer", "")
    cal_link        = payload.get("calendar_link", "")
    n_prospects     = max(5, int(payload.get("prospect_count", 40)))
    csv_b64         = payload.get("csv_data", "")

    n_investors  = max(1, round(n_prospects * 0.25))
    n_customers  = max(1, round(n_prospects * 0.50))
    n_lookalikes = max(1, n_prospects - n_investors - n_customers)
    campaign_id  = str(_uuid_mod.uuid4())

    try:
        from exa_py import Exa
        exa = Exa(api_key=EXA_API_KEY)
    except ImportError:
        send("log", text="exa-py not installed — run: pip install exa-py", level="error")
        return

    # ── Step 1: Analyze ────────────────────────────────────
    send("step_start", step="analyze", label="Analyzing Business", progress=5)
    send("log", text="Gemini is building your prospect search strategy…")

    analysis_prompt = f"""You are a growth expert analyzing a startup.

Business description: {business_desc}
Target customer: {target_customer}

Return ONLY a valid JSON object (no markdown, no explanation):
{{
  "investor_queries": ["query1", "query2", "query3"],
  "customer_queries": ["query1", "query2", "query3"],
  "lookalike_queries": ["query1", "query2"],
  "business_summary": "2-3 sentence summary for email personalization",
  "value_prop": "one sentence describing what problem this solves and for whom"
}}

investor_queries: Exa neural search queries to find VCs and angel investors active in this space.
customer_queries: Exa neural search queries to find professionals who would buy/use this product.
lookalike_queries: fallback queries (used when no customer CSV is provided) to find people similar to existing customers."""

    analysis: dict = {}
    try:
        resp = genai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=analysis_prompt,
        )
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        analysis = json.loads(raw)
    except Exception as e:
        send("log", text=f"Analysis partial failure: {e}. Using fallback queries.", level="warn")
        analysis = {
            "investor_queries": [f"VC investor {business_desc[:40]} seed"],
            "customer_queries": [f"{target_customer} professional LinkedIn"],
            "lookalike_queries": [f"{target_customer} manager director"],
            "business_summary": business_desc[:200],
            "value_prop": business_desc[:100],
        }

    value_prop       = analysis.get("value_prop", business_desc[:100])
    business_summary = analysis.get("business_summary", business_desc[:200])
    send("step_done", step="analyze")
    send("log", text=f"Strategy ready — {n_investors} investors · {n_customers} customers · {n_lookalikes} interviews.")

    all_prospects: list[dict] = []

    def _exa_search(query: str, category: str, count: int):
        try:
            res = exa.search_and_contents(
                query,
                type="neural",
                num_results=min(count + 3, 15),
                highlights={"num_sentences": 3},
                text={"max_characters": 2000},
            )
            return _exa_to_prospects(res.results, category)[:count]
        except Exception as e:
            send("log", text=f"Exa error for '{query[:40]}': {e}", level="warn")
            return []

    # ── Step 2: Investors ───────────────────────────────────
    send("step_start", step="investors", label="Finding Investors", progress=20)
    for q in analysis.get("investor_queries", [])[:2]:
        needed = n_investors - sum(1 for p in all_prospects if p["category"] == "investor")
        if needed <= 0:
            break
        send("log", text=f'Searching: "{q}"')
        for p in _exa_search(q, "investor", needed):
            all_prospects.append(p)
            send("prospect_found", prospect=p)
    send("step_done", step="investors")

    # ── Step 3: Customers ───────────────────────────────────
    send("step_start", step="customers", label="Finding Customers", progress=45)
    for q in analysis.get("customer_queries", [])[:2]:
        needed = n_customers - sum(1 for p in all_prospects if p["category"] == "customer")
        if needed <= 0:
            break
        send("log", text=f'Searching: "{q}"')
        for p in _exa_search(q, "customer", needed):
            all_prospects.append(p)
            send("prospect_found", prospect=p)
    send("step_done", step="customers")

    # ── Step 4: Lookalikes ──────────────────────────────────
    send("step_start", step="lookalikes", label="Finding Lookalikes", progress=65)
    seeded = False

    if csv_b64:
        try:
            csv_text = base64.b64decode(csv_b64).decode("utf-8", errors="replace")
            rows     = list(_csv.DictReader(_io.StringIO(csv_text)))[:5]
            send("log", text=f"Loaded {len(rows)} existing customers from CSV.")
            for row in rows:
                needed = n_lookalikes - sum(1 for p in all_prospects if p["category"] == "interview")
                if needed <= 0:
                    break
                url = next((row.get(k, "") for k in
                            ("website", "url", "linkedin", "Website", "URL", "LinkedIn") if row.get(k)), "")
                if url:
                    send("log", text=f"Finding lookalikes for {url}…")
                    try:
                        res = exa.find_similar_and_contents(
                            url,
                            num_results=min(needed + 2, 5),
                            highlights={"num_sentences": 3},
                            text={"max_characters": 2000},
                        )
                        for p in _exa_to_prospects(res.results, "interview")[:needed]:
                            all_prospects.append(p)
                            send("prospect_found", prospect=p)
                        seeded = True
                    except Exception as e:
                        send("log", text=f"find_similar error: {e}", level="warn")
        except Exception as e:
            send("log", text=f"CSV parse error: {e}", level="warn")

    if not seeded:
        send("log", text="Using fallback lookalike search.")
        for q in analysis.get("lookalike_queries", [])[:2]:
            needed = n_lookalikes - sum(1 for p in all_prospects if p["category"] == "interview")
            if needed <= 0:
                break
            send("log", text=f'Searching: "{q}"')
            for p in _exa_search(q, "interview", needed):
                all_prospects.append(p)
                send("prospect_found", prospect=p)
    send("step_done", step="lookalikes")

    # ── Step 5: Draft emails ────────────────────────────────
    send("step_start", step="drafting", label="Drafting Emails", progress=80)
    send("log", text=f"Gemini is personalizing {len(all_prospects)} emails…")

    if all_prospects:
        batch_input = [
            {"id": p["id"], "name": p["name"], "title": p["title"],
             "company": p["company"], "bio": p["exa_summary"][:200], "category": p["category"]}
            for p in all_prospects
        ]
        draft_prompt = f"""You are a founder writing cold outreach emails. Short (3-5 sentences), genuine, non-spammy.

Product: {value_prop}
Context: {business_summary}
Calendar link (interview emails): {cal_link or "[not provided]"}

Write a personalized email for each prospect below.
Return ONLY a valid JSON array (no markdown):
[{{"id":"...","subject":"...","body":"..."}}, ...]

Rules by category:
- investor: concrete traction signal or insight, market framing, ask for 30-min call
- customer: open with their pain point, show you understand their world, soft CTA
- interview: reference their background/company, ask for 20-min call, include calendar link

No "I hope this finds you well". No generic openers. Subject lines under 50 chars.

Prospects:
{json.dumps(batch_input)}"""

        try:
            resp = genai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=draft_prompt,
            )
            raw = resp.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            drafts    = json.loads(raw)
            draft_map = {d["id"]: d for d in drafts}
            for p in all_prospects:
                if p["id"] in draft_map:
                    p["draft_subject"] = draft_map[p["id"]].get("subject", "")
                    p["draft_body"]    = draft_map[p["id"]].get("body", "")
        except Exception as e:
            send("log", text=f"Email drafting error: {e}. Drafts left blank.", level="warn")

    send("step_done", step="drafting")

    # ── Save & done ─────────────────────────────────────────
    send("log", text="Saving campaign…")
    _fs_save_campaign(uid, campaign_id, {
        "business_description": business_desc,
        "target_customer":      target_customer,
        "calendar_link":        cal_link,
        "prospect_count":       len(all_prospects),
        "status":               "complete",
    })
    for p in all_prospects:
        _fs_save_prospect(uid, campaign_id, p)

    emails_found = sum(1 for p in all_prospects if p["email"])
    send("done", campaign_id=campaign_id, total=len(all_prospects), emails_found=emails_found)
    send("log", text=f"Complete — {len(all_prospects)} prospects, {emails_found} with emails.")


# ── Gmail OAuth routes ──────────────────────────────────────

@app.get("/api/gmail/auth")
async def gmail_auth(request: Request):
    uid = _get_uid(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET:
        return JSONResponse({"error": "Gmail OAuth not configured on server"}, status_code=500)
    try:
        from google_auth_oauthlib.flow import Flow
        import secrets

        flow = Flow.from_client_config(
            {"web": {
                "client_id":     GMAIL_CLIENT_ID,
                "client_secret": GMAIL_CLIENT_SECRET,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
                "redirect_uris": [GMAIL_REDIRECT_URI],
            }},
            scopes=[
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
            redirect_uri=GMAIL_REDIRECT_URI,
        )
        state = f"{uid}:{secrets.token_urlsafe(16)}"
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", state=state,
        )
        if fs:
            fs.collection("users").document(uid).set({"gmailOAuthState": state}, merge=True)
        return JSONResponse({"auth_url": auth_url})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/gmail/callback")
async def gmail_callback(request: Request):
    code  = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not state or ":" not in state:
        return HTMLResponse("<p>Invalid callback parameters.</p>", status_code=400)

    uid = state.split(":")[0]
    if fs:
        try:
            saved = (fs.collection("users").document(uid).get().to_dict() or {}).get("gmailOAuthState", "")
            if saved != state:
                return HTMLResponse("<p>State mismatch — please try again.</p>", status_code=400)
        except Exception:
            pass

    try:
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build as google_build

        flow = Flow.from_client_config(
            {"web": {
                "client_id":     GMAIL_CLIENT_ID,
                "client_secret": GMAIL_CLIENT_SECRET,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
                "redirect_uris": [GMAIL_REDIRECT_URI],
            }},
            scopes=[
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
            redirect_uri=GMAIL_REDIRECT_URI,
            state=state,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        gmail_address = ""
        try:
            svc  = google_build("oauth2", "v2", credentials=creds)
            info = svc.userinfo().get().execute()
            gmail_address = info.get("email", "")
        except Exception:
            pass

        _fs_save_gmail_tokens(uid, {
            "access_token":  creds.token,
            "refresh_token": creds.refresh_token,
            "token_expiry":  creds.expiry.isoformat() if creds.expiry else None,
            "gmail_address": gmail_address,
        })

        close_script = f"window.opener && window.opener.postMessage({{type:'gmail_connected',email:{json.dumps(gmail_address)}}},'*'); window.close();"
        return HTMLResponse(f"""<!DOCTYPE html><html><head>
<script>{close_script}</script>
</head><body style="font-family:sans-serif;padding:40px;text-align:center">
<p>Gmail connected as <strong>{gmail_address}</strong>.<br>You can close this window.</p>
</body></html>""")
    except Exception as e:
        return HTMLResponse(f"<p>OAuth error: {e}</p>", status_code=500)


@app.get("/api/gmail/status")
async def gmail_status(request: Request):
    uid = _get_uid(request)
    if not uid:
        return JSONResponse({"connected": False})
    tokens = _fs_get_gmail_tokens(uid)
    return JSONResponse({
        "connected": bool(tokens.get("refresh_token")),
        "email":     tokens.get("gmail_address", ""),
    })


@app.post("/api/email/send")
async def email_send(request: Request):
    uid = _get_uid(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body        = await request.json()
    to_addr     = body.get("to", "")
    subject     = body.get("subject", "")
    email_body  = body.get("body", "")
    campaign_id = body.get("campaign_id", "")
    prospect_id = body.get("prospect_id", "")

    if not to_addr:
        return JSONResponse({"error": "No recipient address"}, status_code=400)

    svc = _get_gmail_service(uid)
    if not svc:
        return JSONResponse({"error": "Gmail not connected"}, status_code=400)

    try:
        import email.mime.text
        import email.mime.multipart

        msg = email.mime.multipart.MIMEMultipart()
        msg["To"]      = to_addr
        msg["Subject"] = subject
        msg.attach(email.mime.text.MIMEText(email_body, "plain"))
        raw    = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = svc.users().messages().send(userId="me", body={"raw": raw}).execute()

        if fs and campaign_id and prospect_id:
            try:
                (fs.collection("users").document(uid)
                   .collection("emailCampaigns").document(campaign_id)
                   .collection("prospects").document(prospect_id)
                   .set({"sent": True, "sent_at": fb_fs.SERVER_TIMESTAMP}, merge=True))
            except Exception:
                pass

        return JSONResponse({"ok": True, "message_id": result.get("id", "")})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/email/campaign/{campaign_id}/csv")
async def email_campaign_csv(campaign_id: str, request: Request):
    uid = _get_uid(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not fs:
        return JSONResponse({"error": "Database unavailable"}, status_code=500)

    try:
        docs = (fs.collection("users").document(uid)
                  .collection("emailCampaigns").document(campaign_id)
                  .collection("prospects").stream())
        rows = [doc.to_dict() for doc in docs]

        out    = _io.StringIO()
        fields = ["name", "title", "company", "email", "email_confidence",
                  "category", "url", "draft_subject", "draft_body", "sent"]
        writer = _csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})

        from fastapi.responses import Response as _FResponse
        return _FResponse(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="campaign_{campaign_id[:8]}.csv"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Emailer WebSocket ───────────────────────────────────────

@app.websocket("/api/ws/marketing/email")
async def marketing_email_websocket(websocket: WebSocket):
    await websocket.accept()

    if not genai_client:
        await websocket.send_text(json.dumps({"type": "error", "message": "GEMINI_API_KEY missing"}))
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    token = data.get("token", "")
    uid   = None
    if fb_auth and token:
        try:
            uid = fb_auth.verify_id_token(token)["uid"]
        except Exception:
            pass

    recv_q = queue.Queue()
    loop   = asyncio.get_running_loop()

    def send(evt_type, **kwargs):
        payload = json.dumps({"type": evt_type, **kwargs})
        try:
            asyncio.run_coroutine_threadsafe(websocket.send_text(payload), loop)
        except Exception:
            pass

    def run():
        import sys
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            asyncio.set_event_loop(asyncio.ProactorEventLoop())
        try:
            _run_email_pipeline(send, recv_q, data, uid)
        except Exception as e:
            import traceback
            send("log", text=f"Pipeline error: {e}\n{traceback.format_exc()}", level="error")
        finally:
            send("ws_done")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    try:
        import time as _time
        last_ping = _time.monotonic()
        while thread.is_alive():
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
                recv_q.put(msg)
            except asyncio.TimeoutError:
                now = _time.monotonic()
                if now - last_ping >= 20:
                    try:
                        await websocket.send_text(json.dumps({"type": "ping"}))
                        last_ping = now
                    except Exception:
                        break
    except WebSocketDisconnect:
        recv_q.put({"type": "disconnect"})
    except Exception as e:
        print("Email WS error:", e)
