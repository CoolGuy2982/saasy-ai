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
import sys
from dotenv import load_dotenv

# ── Background build job registry ─────────────────────────────────────────────
# Survives page navigations and WebSocket disconnects.
# bid → {"events": [], "done": bool, "uid": str, "recv_q": Queue, "lock": Lock, "thread": Thread}
_active_builds: dict = {}
# jid → {"events": [], "done": bool, "lock": Lock}
_active_email_jobs: dict = {}
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

def _fs_save_deployed(uid: str, bid: str, url: str):
    """Mark a business as live and persist its deployed URL."""
    if not fs or not uid or not bid or not url:
        return
    try:
        (fs.collection("users").document(uid)
           .collection("businesses").document(bid)
           .set({"status": "live", "deployedUrl": url, "deployedAt": fb_fs.SERVER_TIMESTAMP},
                merge=True))
    except Exception as e:
        print(f"Failed saving deployed URL: {e}")

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

def _fs_save_pipeline_status(uid: str, bid: str, status: str, extra: dict | None = None):
    """Save pipeline status to Firestore.
    status: 'building' | 'reels' | 'emailing' | 'complete' | 'error'
    """
    if not fs or not uid or not bid:
        return
    payload = {"pipelineStatus": status, "pipelineUpdatedAt": fb_fs.SERVER_TIMESTAMP}
    if extra:
        payload.update(extra)
    try:
        (fs.collection("users").document(uid)
           .collection("businesses").document(bid)
           .set(payload, merge=True))
    except Exception as e:
        print(f"Failed saving pipeline status: {e}")

def _fs_log_event(uid: str, bid: str, event: dict):
    """Append a build event to the business's events subcollection."""
    if not fs or not uid or not bid:
        return
    try:
        ref = (fs.collection("users").document(uid)
                 .collection("businesses").document(bid)
                 .collection("build_events"))
        # Use server timestamp for ordering, but store the event itself
        ref.add({**event, "timestamp": fb_fs.SERVER_TIMESTAMP})
    except Exception as e:
        print(f"Failed logging event to Firestore: {e}")

def _fs_get_events(uid: str, bid: str) -> list:
    """Retrieve all build events for a business, ordered by timestamp."""
    if not fs or not uid or not bid:
        return []
    try:
        docs = (fs.collection("users").document(uid)
                  .collection("businesses").document(bid)
                  .collection("build_events")
                  .order_by("timestamp")
                  .stream())
        events = []
        for d in docs:
            data = d.to_dict()
            data.pop("timestamp", None)
            events.append(data)
        return events
    except Exception as e:
        print(f"Failed fetching events from Firestore: {e}")
        return []


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
    return templates.TemplateResponse(request, "index.html", context={"step": 0})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", context={"step": 0})


@app.get("/new", response_class=HTMLResponse)
async def new_business(request: Request):
    return templates.TemplateResponse(request, "new.html", context={"step": 1})


@app.get("/build", response_class=HTMLResponse)
async def build_page(request: Request):
    return templates.TemplateResponse(request, "build.html", context={"step": 2})


@app.get("/marketing", response_class=HTMLResponse)
async def marketing_page(request: Request):
    return templates.TemplateResponse(request, "marketing.html", context={"step": 3})


@app.get("/marketing/reels", response_class=HTMLResponse)
async def marketing_reels_page(request: Request):
    return templates.TemplateResponse(request, "marketing_reels.html", context={"step": 3})


@app.get("/marketing/email", response_class=HTMLResponse)
async def marketing_email_page(request: Request):
    return templates.TemplateResponse(request, "marketing_email.html", context={"step": 4})


@app.post("/api/session")
async def set_session(request: Request, response: Response):
    await request.json()
    return JSONResponse({"status": "ok"})


@app.delete("/api/session")
async def clear_session(response: Response):
    return JSONResponse({"status": "ok"})


@app.delete("/api/business/{bid}")
async def delete_business(request: Request, bid: str):
    uid = _get_uid(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not fs or not bid:
        return JSONResponse({"error": "Bad request"}, status_code=400)
    try:
        biz_ref = (fs.collection("users").document(uid)
                     .collection("businesses").document(bid))
        # Delete subcollections (messages)
        msgs = biz_ref.collection("messages").stream()
        for m in msgs:
            m.reference.delete()
        biz_ref.delete()
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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

                def _extract_grounding(gm, seen_urls, sources, search_queries):
                    """Extract sources and queries from a grounding metadata object."""
                    s_list = []
                    q_list = [q for q in (gm.web_search_queries or []) if q]
                    chunks = getattr(gm, "grounding_chunks", None) or []
                    for gc in chunks:
                        web = getattr(gc, "web", None)
                        if web:
                            uri = getattr(web, "uri", "") or ""
                            title = getattr(web, "title", "") or uri
                            if uri and uri not in seen_urls:
                                seen_urls.add(uri)
                                s_list.append({"url": uri, "title": title})
                                sources.append({"url": uri, "title": title})
                    for q in q_list:
                        if q not in search_queries:
                            search_queries.append(q)
                    return q_list, s_list

                stream_fn = getattr(genai_client.models, "generate_content_stream", None)
                if stream_fn:
                    last_chunk = None
                    for chunk in stream_fn(
                        model="gemini-3-flash-preview",
                        contents=contents,
                        config=config,
                    ):
                        last_chunk = chunk
                        text = getattr(chunk, "text", None) or ""
                        try:
                            cands = getattr(chunk, "candidates", None) or []
                            if cands and getattr(cands[0], "grounding_metadata", None):
                                gm = cands[0].grounding_metadata
                                q_list, s_list = _extract_grounding(gm, seen_urls, sources, search_queries)
                                if q_list or s_list:
                                    loop.call_soon_threadsafe(queue.put_nowait, {
                                        "type": "search_query",
                                        "queries": list(search_queries),
                                        "sources": list(sources),
                                    })
                        except Exception:
                            pass

                        if text:
                            full_text += text
                            loop.call_soon_threadsafe(queue.put_nowait, {"type": "text", "text": text})

                    # Final pass: extract grounding from the last chunk (often only populated here)
                    if last_chunk and not sources:
                        try:
                            cands = getattr(last_chunk, "candidates", None) or []
                            if cands and getattr(cands[0], "grounding_metadata", None):
                                _extract_grounding(cands[0].grounding_metadata, seen_urls, sources, search_queries)
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
                        cands = getattr(response, "candidates", None) or []
                        if cands and getattr(cands[0], "grounding_metadata", None):
                            _extract_grounding(cands[0].grounding_metadata, seen_urls, sources, search_queries)
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
    # The frontend sends the profileId it received from /browser-setup/start
    # (Steel assigns it at session creation with persist_profile=True).
    profile_id = body.get("profileId") or None
    try:
        steel.sessions.release(session_id)
        print(f"[browser-setup/finish] released session {session_id}, client profileId={profile_id}")
        # If the client didn't send a profileId, poll until Steel populates it (up to 30s)
        if not profile_id:
            for _pi in range(30):
                try:
                    sess = steel.sessions.retrieve(session_id)
                    profile_id = getattr(sess, "profile_id", None)
                    print(f"[browser-setup/finish] poll {_pi+1}: profile_id={profile_id}")
                    if profile_id:
                        break
                except Exception as e:
                    print(f"[browser-setup/finish] poll error: {e}")
                time.sleep(1)
            # Final fallback: list profiles and match by source session
            if not profile_id:
                try:
                    profiles = steel.profiles.list()
                    for p in (profiles.profiles if hasattr(profiles, "profiles") else profiles):
                        if getattr(p, "source_session_id", None) == session_id:
                            profile_id = getattr(p, "id", None)
                            break
                except Exception:
                    pass
        print(f"[browser-setup/finish] final profile_id={profile_id}")
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
        steel.sessions.computer(session_id, action="press_key", keys=["Control", "l"])
        time.sleep(0.3)
        steel.sessions.computer(session_id, action="press_key", keys=["Control", "a"])
        time.sleep(0.1)
        steel.sessions.computer(session_id, action="type_text", text=url)
        time.sleep(0.5)
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


def _design_with_stitch(genai_client, prd_text: str, app_name: str, send, recv_q=None) -> list[dict]:
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
    send("log", text="  Stitch: creating new project…")
    try:
        proj = _stitch_call(tool_create, {"title": app_name}, STITCH_API_KEY)
        # Response may be resource name "projects/12345" or a numeric projectId
        resource_name = _stitch_extract_id(proj, "name", "projectId", "project_id", "id")
        if resource_name and "/" in resource_name:
            project_id = resource_name.split("/")[-1]
        else:
            project_id = resource_name
        if not project_id:
            send("log", text="  Stitch: could not find projectId in response.", level="warn")
            return []
        send("log", text=f"  Stitch: project created (ID: {project_id})")
    except Exception as e:
        send("log", text=f"  Stitch create_project failed: {e}", level="error")
        return []

    # Step 2: Decide screen prompts
    send("log", text="  Stitch: planning screen layouts with Gemini…")
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

    import queue as _q
    import threading as _threading

    def _check_skip():
        """Returns True if a skip_stitch message is in the queue."""
        if recv_q is None:
            return False
        try:
            msg = recv_q.get_nowait()
            if msg.get("type") == "skip_stitch":
                return True
            recv_q.put(msg)
        except _q.Empty:
            pass
        return False

    screens: list[dict] = []
    known_screen_ids: set[str] = set()
    total_screens = len(screen_specs)

    for idx, (screen_name, prompt) in enumerate(screen_specs):
        # Check for skip before starting
        if _check_skip():
            send("log", text="  Stitch: generation skipped by user.", level="warn")
            return screens

        send("design_generating", name=screen_name, index=idx, total=total_screens)
        send("log", text=f"  Stitch: generating '{screen_name}' ({idx+1}/{total_screens})…")
        try:
            # Run the blocking Stitch call in a thread so we can check for skip
            _gen_result: list = [None]
            _gen_error:  list = [None]
            def _do_gen(r=_gen_result, e=_gen_error, p=prompt):
                try:
                    # Stitch generateScreen is the most time-consuming call (1-3 min)
                    r[0] = _stitch_call(tool_gen, {
                        "projectId": project_id,
                        "prompt": p,
                        "modelId": "GEMINI_3_1_PRO",
                    }, STITCH_API_KEY, timeout=180)
                except Exception as ex:
                    e[0] = ex
            _gen_thread = _threading.Thread(target=_do_gen, daemon=True)
            _gen_thread.start()
            while _gen_thread.is_alive():
                _gen_thread.join(timeout=1.0)
                if _check_skip():
                    send("log", text="  Stitch: skipped by user.", level="warn")
                    return screens
            if _gen_error[0]:
                raise _gen_error[0]
            gen = _gen_result[0]

            shot_url, html_url, screen_resource = _extract_from_components(gen)

            # If no direct screenshot URL, fall back to list_screens → get_screen
            if not shot_url and not screen_resource:
                send("log", text=f"  Stitch: screen not found in generate response, polling list_screens…")
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
                send("log", text=f"  Stitch: fetching full screen metadata ({screen_resource.split('/')[-1]})…")
                parts = screen_resource.split("/")
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
                send("log", text=f"  Stitch: no screenshot available for '{screen_name}'.", level="warn")
                continue

            # Download screenshot
            send("log", text=f"  Stitch: downloading preview for '{screen_name}'…")
            try:
                shot_b64 = _download_b64(shot_url)
            except Exception as dl_err:
                send("log", text=f"  Stitch: download failed: {dl_err}", level="error")
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
            send("log", text=f"  Stitch: '{screen_name}' ready.", level="success")
            send("design_screen", name=screen_name, data=shot_b64, html=screen_html)

        except Exception as e:
            send("log", text=f"  Stitch failed for '{screen_name}': {e}", level="error")

    return screens


# ── /api/build ─────────────────────────────────────────────────────────────
# Drives the full build pipeline:
#   1. Fetch PRD text from the Gemini interaction store
#   2. Spin up a Steel.dev remote browser session
#   3. Run a Gemini Computer Use agent loop targeting Bolt.new
#   4. Stream SSE events (step updates, logs, screenshots) back to build.html
# ---------------------------------------------------------------------------

BOLT_TASK_TEMPLATE = """You are an AI agent monitoring bolt.new. The PRD has already been submitted and bolt.new should be generating the app now.

Your job:

STEP 1 — Wait for bolt.new to finish building:
  - You will see a file tree on the left and streaming code. This takes 1–5 minutes.
  - Do NOT click, type, or interrupt while code is streaming.
  - If you see a spinner or progress indicator, keep waiting.
  - If bolt.new shows an error in the code or terminal, try to fix it by typing in the chat.

STEP 2 — Deploy:
  - Once building is complete (file tree visible, spinner stopped), find the "Deploy" or "Publish" button (usually top-right or in the toolbar).
  - Click it and wait for deployment to finish.
  - When the deployed URL appears (e.g. something like "myapp-abc123.bolt.host"), copy it from the panel.
  - CRITICAL: Use the navigate action to open that deployed URL in the browser address bar. This confirms the deployment and captures the live URL.

STEP 3 — If bolt.new is NOT building yet (still shows the empty prompt box):
  - Click the prompt textarea, type the requirements below, and click Build now.

REQUIREMENTS (only use if bolt.new has not started building):
{prd}

Rules:
- Once you see the deployed URL in the publish panel, navigate to it immediately.
- Never retype requirements if building is already in progress.
- Your final message MUST include the full deployed URL (e.g. https://something.bolt.host)."""




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


def _run_build(send, recv_q, prd_text: str, app_name: str, uid: str | None = None, bid: str = ""):
    """
    Runs the entire build pipeline in a background thread.
    Uses Steel.dev Computer API for all browser interactions (no Playwright CDP).
    `send(type, **kwargs)` queues an event to the websocket.
    `recv_q` receives dict messages from the websocket.
    `uid` is the Firebase UID used to load/save the persistent Steel browser profile.
    """
    # ── Step 1: Plan ───────────────────────────────────────────────────────
    send("step_start", step="plan", label="Planning", progress=5)

    # Recovery: if prd_text is empty, try to fetch from Firestore
    if not prd_text and fs and uid and bid:
        send("log", text="PRD empty in request — attempting recovery from Firestore…")
        try:
            biz_doc = fs.collection("users").document(uid).collection("businesses").document(bid).get()
            if biz_doc.exists:
                data = biz_doc.to_dict() or {}
                prd_text = data.get("prd", "")
                if not app_name:
                    app_name = data.get("name", "My App")
        except Exception as e:
            send("log", text=f"Firestore recovery failed: {e}", level="warn")

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
            stitch_screens = _design_with_stitch(genai_client, prd_text, app_name, send, recv_q)
            send("log", text=f"Stitch: {len(stitch_screens)} screen(s) designed.")
            # Small grace period for designs to be received by UI before switching to browser
            time.sleep(5)
        except Exception as _se:
            send("log", text=f"Stitch skipped: {_se}", level="warn")
    else:
        send("log", text="STITCH_API_KEY not set — skipping UI design step.")

    send("step_done", step="github", label="Designing UI", progress=18)

    # Build the bolt.new task — include Stitch HTML as design context
    prd_for_task = prd_text or f"Build a simple SaaS app called '{app_name}' with Google sign-in and a clean dashboard."
    design_section = ""
    screens_with_html = [s for s in stitch_screens if s.get("html")]
    if screens_with_html:
        # Cap each screen at 8 000 chars, total at 40 000 to stay within Bolt's prompt limit
        PER_SCREEN_LIMIT = 8_000
        TOTAL_LIMIT = 40_000
        screen_blocks = []
        total = 0
        for s in screens_with_html:
            block = f"### {s['name']}\n```html\n{s['html'][:PER_SCREEN_LIMIT]}\n```"
            if total + len(block) > TOTAL_LIMIT:
                break
            screen_blocks.append(block)
            total += len(block)
        if screen_blocks:
            design_section = (
                "\n\n## UI Design Reference (generated by Google Stitch)\n"
                "Use the following HTML for each page — match these designs exactly:\n\n"
                + "\n\n".join(screen_blocks)
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
            # No persist_profile here — build sessions load the profile read-only.
            # persist_profile=True would overwrite the saved logins with whatever
            # state the browser ends up in (e.g. a sign-in modal = logged out).
        )
        if existing_profile_id:
            session_kwargs["profile_id"] = existing_profile_id
            send("log", text=f"Restoring saved browser profile ({existing_profile_id[:8]}…)")
        else:
            send("log", text="No saved browser profile — continue without saved credentials.")

        session = steel.sessions.create(**session_kwargs)
        sid = session.id
        send("log", text=f"Steel.dev session {sid[:8]}… ready.")
        send("url", url="https://bolt.new")
        send("browser_loading", loading=True)

        # Navigate to bolt.new with the PRD pre-filled via URL parameter
        # bolt.new supports ?prompt=... to pre-populate the input box
        import urllib.parse as _urlparse
        prd_for_bolt = prd_text or f"Build a simple SaaS app called '{app_name}' with Google sign-in and a clean dashboard."
        bolt_url = "https://bolt.new/?prompt=" + _urlparse.quote(prd_for_bolt, safe="")
        send("log", text="Navigating to bolt.new with PRD pre-filled…")
        send("url", url="https://bolt.new")

        steel.sessions.computer(sid, action="press_key", keys=["Control", "l"])
        time.sleep(0.5)
        steel.sessions.computer(sid, action="press_key", keys=["Control", "a"])
        time.sleep(0.2)
        steel.sessions.computer(sid, action="type_text", text=bolt_url)
        time.sleep(2.0)  # wait for full URL to be typed before pressing Return
        steel.sessions.computer(sid, action="press_key", keys=["Return"])

        # Poll until bolt.new has fully rendered (screenshot > 150 KB) or 60s timeout
        send("log", text="Waiting for bolt.new to load…")
        img_b64 = ""
        img_bytes = b""
        for _attempt in range(20):
            time.sleep(3.0)
            img_b64 = _steel_screenshot(steel, sid)
            img_bytes = base64.b64decode(img_b64) if img_b64 else b""
            send("log", text=f"  bolt.new: {len(img_bytes):,} bytes…")
            if img_b64:
                send("screenshot", data=img_b64)
            if len(img_bytes) > 150_000:
                send("log", text="bolt.new loaded.")
                break
        else:
            send("log", text="bolt.new slow to load — continuing anyway.", level="warn")

        # The ?prompt= URL auto-fills the textarea.
        # Take a screenshot first so we can see what loaded, then click Build now.
        send("log", text="Submitting PRD…")
        img_b64 = _steel_screenshot(steel, sid)
        if img_b64:
            send("screenshot", data=img_b64)
        # Click the Build now button — it's always to the right of the input bar
        # Use Tab to focus the button and Enter to press it (keyboard-safe approach)
        steel.sessions.computer(sid, action="press_key", keys=["Tab"])
        time.sleep(0.3)
        steel.sessions.computer(sid, action="press_key", keys=["Return"])
        send("log", text="PRD submitted — waiting for bolt.new to start generating…")
        time.sleep(5.0)

        img_b64 = _steel_screenshot(steel, sid)
        img_bytes = base64.b64decode(img_b64) if img_b64 else b""
        if img_b64:
            send("screenshot", data=img_b64)
        send("log", text=f"Post-submit screenshot: {len(img_bytes):,} bytes — handing off to agent.")

        send("browser_loading", loading=False)
        current_url = "https://bolt.new"

        # ── Gemini Computer Use agent loop ─────────────────────────────────
        # Agent receives the post-submit screenshot. Bolt should already be building.
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
                        pass  # Build continues in background when browser closes
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
                    # Extract deployed URL from agent's final message
                    import re as _re
                    _url_match = _re.search(r'https?://[^\s"\'<>]+\.bolt\.host[^\s"\'<>]*', final)
                    if not _url_match:
                        _url_match = _re.search(r'https?://[^\s"\'<>]*bolt\.host[^\s"\'<>]*', final)
                    if _url_match:
                        current_url = _url_match.group(0).rstrip(".,)")
                        send("log", text=f"Deployed URL captured: {current_url}")
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
        send("log", text=f"Build complete — deployed at: {current_url}")
        if uid and bid and current_url and "bolt.new" not in current_url:
            _fs_save_deployed(uid, bid, current_url)
            # Auto-start marketing + email pipeline in the background
            threading.Thread(
                target=_run_auto_pipeline,
                args=(uid, bid, current_url, prd_text),
                daemon=True,
            ).start()
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


@app.get("/api/build/status")
async def build_status_endpoint(request: Request, bid: str = ""):
    """Return the current build/pipeline status for a business.
    Checks in-memory registry first, then Firebase."""
    uid = _get_uid(request)
    if not uid or not bid:
        return JSONResponse({"status": "idle"})

    if bid in _active_builds:
        job = _active_builds[bid]
        return JSONResponse({
            "status": "running" if not job["done"] else "done",
            "eventCount": len(job["events"]),
        })

    if fs:
        try:
            doc = (fs.collection("users").document(uid)
                     .collection("businesses").document(bid).get())
            data = doc.to_dict() or {}
            ps  = data.get("pipelineStatus", "")
            url = data.get("deployedUrl", "")
            if ps:
                return JSONResponse({"status": ps, "deployedUrl": url})
        except Exception:
            pass

    return JSONResponse({"status": "idle"})


@app.websocket("/api/ws/build")
async def build_websocket(websocket: WebSocket):
    await websocket.accept()

    if not genai_client or not STEEL_API_KEY:
        await websocket.send_text(json.dumps({'type': 'error', 'message': 'GEMINI_API_KEY or STEEL_API_KEY missing'}))
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    prd_text     = data.get("prd_text", "")
    app_name     = data.get("app_name", "My App")
    token        = data.get("token", "")
    bid          = data.get("bid", "")
    event_offset = int(data.get("event_offset", 0))  # replay from this index on reconnect

    uid = None
    if fb_auth and token:
        try:
            uid = fb_auth.verify_id_token(token)["uid"]
        except Exception:
            pass

    # ── Locate or create background job ────────────────────────────────────────
    if bid and bid in _active_builds and not _active_builds[bid]["done"]:
        # Reconnect to an already-running build
        job = _active_builds[bid]
    else:
        # Load any existing events from Firestore for this business
        existing_events = _fs_get_events(uid, bid) if (uid and bid) else []

        # Start a brand-new build job (or resume if there are already events)
        job = {
            "events": existing_events,
            "done":   False,
            "uid":    uid,
            "recv_q": queue.Queue(),
            "lock":   threading.Lock(),
            "thread": None,
        }

        if bid and (bid not in _active_builds or _active_builds[bid]["done"]):
            _active_builds[bid] = job
            if not existing_events:
                _fs_save_pipeline_status(uid, bid, "building")

            def send(evt_type, **kwargs):
                """Store event in registry and Firestore — WS handler polls this list."""
                payload = {"type": evt_type, **kwargs}
                with job["lock"]:
                    job["events"].append(payload)
                # Persist event to Firestore for reconnection durability
                if uid and bid:
                    _fs_log_event(uid, bid, payload)

            def run():
                if sys.platform == "win32":
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                    asyncio.set_event_loop(asyncio.ProactorEventLoop())
                try:
                    # Only run a fresh build if we haven't already finished it
                    # (In a real system, we'd also check if it's already running in another thread)
                    _run_build(send, job["recv_q"], prd_text, app_name, uid, bid)
                except Exception as e:
                    import traceback
                    print(f"Runner error: {traceback.format_exc()}")
                    send("log", text=f"Build error: {e}", level="error")
                finally:
                    send("ws_done")
                    job["done"] = True

            # If no thread and not done, start it (this handles fresh start and resumed after server restart)
            t = threading.Thread(target=run, daemon=True)
            job["thread"] = t
            t.start()
        else:
            # Re-fetch the existing job if it was created between our check and now
            job = _active_builds.get(bid, job)

    # ── Stream events to this WebSocket client ─────────────────────────────────
    # Poll job["events"] for new items; incoming WS messages go to job["recv_q"].
    # On disconnect: just exit this handler — the build keeps running.
    poll_idx = event_offset
    try:
        while True:
            # Send any events produced since last poll
            with job["lock"]:
                new_events = job["events"][poll_idx:]
                poll_idx   = len(job["events"])
            for ev in new_events:
                await websocket.send_text(json.dumps(ev))

            # If job finished and we've sent everything, we're done
            if job["done"] and poll_idx >= len(job["events"]):
                break

            # Non-blocking receive: relay user interactions to the build thread
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=0.05)
                job["recv_q"].put(msg)
            except asyncio.TimeoutError:
                pass

            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass  # Build continues running in the background
    except Exception as e:
        print("Build WS error:", e)


# ── Marketing Reels Pipeline ───────────────────────────────────────────────

def _fs_log_reels_event(uid: str, bid: str, event: dict):
    """Persist a reels marketing event to Firestore under the business."""
    if not fs or not uid or not bid:
        return
    try:
        ref = (fs.collection("users").document(uid)
                 .collection("businesses").document(bid)
                 .collection("reels_logs"))
        ref.add({**event, "timestamp": fb_fs.SERVER_TIMESTAMP})
    except Exception as e:
        print(f"Failed logging reels event: {e}")

def _run_reels_pipeline(send_orig, recv_q, target_url: str, uid: str | None = None, bid: str | None = None):
    # Wrap send to also persist to Firestore logs
    def send(evt_type, **kwargs):
        send_orig(evt_type, **kwargs)
        if uid and bid:
            _fs_log_reels_event(uid, bid, {"type": evt_type, **kwargs})

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
        
        if uid and bid and final_urls:
            _fs_save_pipeline_status(uid, bid, "reels_complete", {"marketingReelUrls": final_urls})
            
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


def _run_auto_pipeline(uid: str, bid: str, deployed_url: str, prd_text: str = ""):
    """
    Autonomous background pipeline: marketing reels → email outreach.
    Fires automatically after a build completes with a deployed URL.
    Saves status updates to Firebase; no WebSocket required.
    """
    import sys as _sys
    if _sys.platform == "win32":
        import asyncio as _asyncio
        _asyncio.set_event_loop_policy(_asyncio.WindowsProactorEventLoopPolicy())
        _asyncio.set_event_loop(_asyncio.ProactorEventLoop())

    def _silent_send(evt_type, **kwargs):
        """Absorb all events silently (no WS connected). Update Firebase on key events."""
        if evt_type in ("step_start", "step_done", "done"):
            print(f"[auto_pipeline] {evt_type}: {kwargs.get('label') or kwargs.get('step') or ''}")

    # ── 1. Marketing reels ─────────────────────────────────────────────────────
    print(f"[auto_pipeline] Starting reels for {deployed_url}")
    _fs_save_pipeline_status(uid, bid, "reels")
    try:
        _run_reels_pipeline(_silent_send, queue.Queue(), deployed_url, uid, bid)
        print("[auto_pipeline] Reels done.")
    except Exception as e:
        print(f"[auto_pipeline] Reels error: {e}")

    # ── 2. Email outreach ──────────────────────────────────────────────────────
    print("[auto_pipeline] Starting email outreach.")
    _fs_save_pipeline_status(uid, bid, "emailing")
    try:
        payload = {
            "business_description": prd_text[:500] if prd_text else f"App at {deployed_url}",
            "target_customer": "",
            "calendar_link": "",
            "prospect_count": 20,
        }
        _run_email_pipeline(_silent_send, queue.Queue(), payload, uid, bid)
        print("[auto_pipeline] Email outreach done.")
    except Exception as e:
        print(f"[auto_pipeline] Email error: {e}")

    _fs_save_pipeline_status(uid, bid, "complete")
    print(f"[auto_pipeline] Pipeline complete for bid={bid}")


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

    target_url = data.get("target_url", "")
    token = data.get("token", "")
    bid = data.get("bid", "")

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
            _run_reels_pipeline(send, recv_q, target_url, uid, bid)
        except Exception as e:
            import traceback
            error_msg = traceback.format_exc()
            send("log", text=f"Pipeline error: {e}\n{error_msg}", level="error")
        finally:
            send("ws_done")
        # After reels finish, kick off email pipeline in background
        if uid and bid:
            def _silent_send(evt_type, **kwargs):
                if evt_type in ("step_start", "step_done", "done"):
                    print(f"[reels_ws→email] {evt_type}: {kwargs.get('label') or kwargs.get('step') or ''}")
            def _run_email_bg():
                import sys as _sys, asyncio as _asyncio
                if _sys.platform == "win32":
                    _asyncio.set_event_loop_policy(_asyncio.WindowsProactorEventLoopPolicy())
                    _asyncio.set_event_loop(_asyncio.ProactorEventLoop())
                _fs_save_pipeline_status(uid, bid, "emailing")
                try:
                    prd_text = ""
                    if fs_db:
                        try:
                            biz_doc = fs_db.collection("users").document(uid).collection("businesses").document(bid).get()
                            if biz_doc.exists:
                                prd_text = biz_doc.to_dict().get("prd", "")
                        except Exception:
                            pass
                    payload = {
                        "business_description": prd_text[:500] if prd_text else f"App at {target_url}",
                        "target_customer": "",
                        "calendar_link": "",
                        "prospect_count": 20,
                    }
                    _run_email_pipeline(_silent_send, queue.Queue(), payload, uid, bid)
                    print("[reels_ws→email] Email outreach done.")
                except Exception as e:
                    print(f"[reels_ws→email] Email error: {e}")
                _fs_save_pipeline_status(uid, bid, "complete")
            threading.Thread(target=_run_email_bg, daemon=True).start()

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

EXA_API_KEY        = os.environ.get("EXA_API_KEY", "")
GMAIL_SENDER       = os.environ.get("GMAIL_SENDER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# ── Gmail draft helper (IMAP append — no OAuth needed) ──────

def _gmail_save_draft(to_addr: str, subject: str, body: str) -> str | None:
    """Save a draft to Gmail via IMAP. Returns None on success, error string on failure."""
    if not GMAIL_SENDER or not GMAIL_APP_PASSWORD:
        return "GMAIL_SENDER or GMAIL_APP_PASSWORD not set in .env"
    import imaplib
    import email.mime.text
    import email.mime.multipart
    import time
    try:
        msg = email.mime.multipart.MIMEMultipart()
        msg["From"]    = GMAIL_SENDER
        msg["To"]      = to_addr
        msg["Subject"] = subject
        msg["Date"]    = email.utils.formatdate()
        msg.attach(email.mime.text.MIMEText(body, "plain"))
        raw = msg.as_bytes()
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            imap.append("[Gmail]/Drafts", "\\Draft", imaplib.Time2Internaldate(time.time()), raw)
        return None
    except Exception as e:
        return str(e)


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
    if not fs or not uid or not campaign_id:
        return
    try:
        pid = prospect.get("id") or _uuid_mod.uuid4().hex
        (fs.collection("users").document(uid)
           .collection("emailCampaigns").document(campaign_id)
           .collection("prospects").document(pid)
           .set({**prospect, "id": pid}, merge=True))
    except Exception as e:
        print(f"Failed saving prospect: {e}")


def _fs_log_email_event(uid: str, campaign_id: str, event: dict):
    """Persist a marketing email event to Firestore."""
    if not fs or not uid or not campaign_id:
        return
    try:
        ref = (fs.collection("users").document(uid)
                 .collection("emailCampaigns").document(campaign_id)
                 .collection("logs"))
        ref.add({**event, "timestamp": fb_fs.SERVER_TIMESTAMP})
    except Exception as e:
        print(f"Failed logging email event: {e}")


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

def _run_email_pipeline(send_orig, recv_q, payload: dict, uid: str | None, bid: str | None = None):
    # Wrap send to also persist to Firestore logs
    campaign_id = None # will be set shortly

    def send(evt_type, **kwargs):
        nonlocal campaign_id
        send_orig(evt_type, **kwargs)
        if uid and campaign_id:
            _fs_log_email_event(uid, campaign_id, {"type": evt_type, **kwargs})

    business_desc   = payload.get("business_description", "")
    target_customer = payload.get("target_customer", "")
    cal_link        = payload.get("calendar_link", "")
    n_prospects     = max(5, int(payload.get("prospect_count", 40)))
    csv_b64         = payload.get("csv_data", "")

    n_investors  = max(1, round(n_prospects * 0.25))
    n_customers  = max(1, round(n_prospects * 0.50))
    n_lookalikes = max(1, n_prospects - n_investors - n_customers)
    campaign_id  = str(_uuid_mod.uuid4()) # Set the nonlocal variable

    # Persist campaign link to business immediately if we have a bid
    if uid and bid:
        _fs_save_pipeline_status(uid, bid, "emailing", {"currentEmailCampaignId": campaign_id})

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

    # Initialize campaign record early for the UI to find
    if uid:
        _fs_save_campaign(uid, campaign_id, {
            "business_description": business_desc,
            "target_customer":      target_customer,
            "calendar_link":        cal_link,
            "status":               "searching",
        })

    all_prospects: list[dict] = []

    def _exa_search(query: str, category: str, count: int):
        if not query or not query.strip():
            return []
        try:
            res = exa.search_and_contents(
                query,
                type="auto",
                num_results=min(count + 3, 15),
                category="people",
                highlights={"max_characters": 1200},
                text={"max_characters": 2000},
            )
            return _exa_to_prospects(res.results, category)[:count]
        except Exception as e:
            send("log", text=f"Exa error for '{query[:40]}': {e}", level="warn")
            return []

    # ── Step 2: Investors ───────────────────────────────────
    send("step_start", step="investors", label="Finding Investors", progress=20)
    for q in analysis.get("investor_queries", [])[:2]:
        if not q or not q.strip(): continue
        needed = n_investors - sum(1 for p in all_prospects if p["category"] == "investor")
        if needed <= 0:
            break
        send("log", text=f'Searching: "{q}"')
        for p in _exa_search(q, "investor", needed):
            all_prospects.append(p)
            if uid: _fs_save_prospect(uid, campaign_id, p)
            send("prospect_found", prospect=p)
    send("step_done", step="investors")

    # ── Step 3: Customers ───────────────────────────────────
    send("step_start", step="customers", label="Finding Customers", progress=45)
    for q in analysis.get("customer_queries", [])[:2]:
        if not q or not q.strip(): continue
        needed = n_customers - sum(1 for p in all_prospects if p["category"] == "customer")
        if needed <= 0:
            break
        send("log", text=f'Searching: "{q}"')
        for p in _exa_search(q, "customer", needed):
            all_prospects.append(p)
            if uid: _fs_save_prospect(uid, campaign_id, p)
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
                            highlights={"max_characters": 1200},
                            text={"max_characters": 2000},
                        )
                        for p in _exa_to_prospects(res.results, "interview")[:needed]:
                            all_prospects.append(p)
                            if uid: _fs_save_prospect(uid, campaign_id, p)
                            send("prospect_found", prospect=p)
                        seeded = True
                    except Exception as e:
                        send("log", text=f"find_similar error: {e}", level="warn")
        except Exception as e:
            send("log", text=f"CSV parse error: {e}", level="warn")

    if not seeded:
        send("log", text="Using fallback lookalike search.")
        for q in analysis.get("lookalike_queries", [])[:2]:
            if not q or not q.strip(): continue
            needed = n_lookalikes - sum(1 for p in all_prospects if p["category"] == "interview")
            if needed <= 0:
                break
            send("log", text=f'Searching: "{q}"')
            for p in _exa_search(q, "interview", needed):
                all_prospects.append(p)
                if uid: _fs_save_prospect(uid, campaign_id, p)
                send("prospect_found", prospect=p)
    send("step_done", step="lookalikes")

    # ── Step 5: Draft emails ────────────────────────────────
    send("step_start", step="drafting", label="Drafting Emails", progress=80)
    send("log", text=f"Gemini is personalizing {len(all_prospects)} emails…")

    def _extract_json_array(text: str):
        """Robustly extract a JSON array from Gemini output."""
        text = text.strip()
        # Strip markdown fences
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("["):
                    text = part
                    break
        # Find the outermost [ ... ] array
        start = text.find("[")
        end   = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            text = text[start:end+1]
        return json.loads(text)

    DRAFT_SYSTEM = f"""You are a founder writing cold outreach emails. Short (3-5 sentences), genuine, non-spammy.

Product: {value_prop}
Context: {business_summary}
Calendar link (interview emails): {cal_link or "[not provided]"}

Rules by category:
- investor: concrete traction signal or insight, market framing, ask for 30-min call
- customer: open with their pain point, show you understand their world, soft CTA
- interview: reference their background/company, ask for 20-min call, include calendar link

No "I hope this finds you well". No generic openers. Subject lines under 50 chars.
Return ONLY a valid JSON array — no markdown, no explanation:
[{{"id":"...","subject":"...","body":"..."}}, ...]"""

    BATCH_SIZE = 10
    if all_prospects:
        for batch_start in range(0, len(all_prospects), BATCH_SIZE):
            batch = all_prospects[batch_start:batch_start + BATCH_SIZE]
            batch_input = [
                {"id": p["id"], "name": p["name"], "title": p["title"],
                 "company": p["company"], "bio": (p.get("exa_summary") or "")[:200],
                 "category": p["category"]}
                for p in batch
            ]
            prompt = DRAFT_SYSTEM + f"\n\nProspects:\n{json.dumps(batch_input)}"
            try:
                resp = genai_client.models.generate_content(
                    model="gemini-3-flash-preview",
                    contents=prompt,
                )
                drafts    = _extract_json_array(resp.text)
                draft_map = {d["id"]: d for d in drafts}
                for p in batch:
                    d = draft_map.get(p["id"])
                    if d:
                        p["draft_subject"] = d.get("subject", "")
                        p["draft_body"]    = d.get("body", "")
                        if uid: _fs_save_prospect(uid, campaign_id, p)
                send("log", text=f"Drafted {len(draft_map)} emails (batch {batch_start//BATCH_SIZE + 1}).", level="success")
            except Exception as e:
                send("log", text=f"Drafting error (batch {batch_start//BATCH_SIZE + 1}): {e}", level="warn")
                if uid:
                    _fs_log_email_event(uid, campaign_id, {"type": "log", "text": f"Drafting error: {e}", "level": "warn"})

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
    # final sync just in case
    for p in all_prospects:
        _fs_save_prospect(uid, campaign_id, p)

    if uid and bid:
        _fs_save_pipeline_status(uid, bid, "complete")

    emails_found = sum(1 for p in all_prospects if p["email"])
    send("done", campaign_id=campaign_id, total=len(all_prospects), emails_found=emails_found)
    send("log", text=f"Complete — {len(all_prospects)} prospects, {emails_found} with emails.")

    # ── Auto-save drafts to Gmail ────────────────────────────
    if GMAIL_SENDER and GMAIL_APP_PASSWORD:
        send("log", text="Saving drafts to Gmail…")
        drafted = 0
        for p in all_prospects:
            if p.get("email") and p.get("draft_subject") and p.get("draft_body"):
                err = _gmail_save_draft(p["email"], p["draft_subject"], p["draft_body"])
                if err:
                    send("log", text=f"Draft failed for {p.get('name','?')}: {err}", level="warn")
                else:
                    drafted += 1
                    if uid:
                        _fs_save_prospect(uid, campaign_id, {**p, "drafted": True})
        send("log", text=f"{drafted} drafts saved to Gmail.", level="success")


# ── Gmail status + send routes ──────────────────────────────

@app.get("/api/gmail/status")
async def gmail_status(request: Request):
    configured = bool(GMAIL_SENDER and GMAIL_APP_PASSWORD)
    return JSONResponse({"configured": configured, "email": GMAIL_SENDER if configured else ""})


@app.post("/api/email/draft")
async def email_draft(request: Request):
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

    err = _gmail_save_draft(to_addr, subject, email_body)
    if err:
        return JSONResponse({"error": err}, status_code=500)

    if fs and campaign_id and prospect_id:
        try:
            (fs.collection("users").document(uid)
               .collection("emailCampaigns").document(campaign_id)
               .collection("prospects").document(prospect_id)
               .set({"drafted": True, "drafted_at": fb_fs.SERVER_TIMESTAMP}, merge=True))
        except Exception:
            pass

    return JSONResponse({"ok": True})


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


# ── Email pipeline status endpoint ──────────────────────────

@app.get("/api/email/pipeline/status")
async def email_pipeline_status(jid: str = ""):
    if not jid or jid not in _active_email_jobs:
        return JSONResponse({"status": "idle"})
    job = _active_email_jobs[jid]
    return JSONResponse({
        "status":     "done" if job["done"] else "running",
        "eventCount": len(job["events"]),
    })


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

    token        = data.get("token", "")
    jid          = data.get("job_id", "")
    bid          = data.get("bid", "")
    event_offset = int(data.get("event_offset", 0))
    uid          = None
    if fb_auth and token:
        try:
            uid = fb_auth.verify_id_token(token)["uid"]
        except Exception:
            pass

    # ── Locate or create background job ───────────────────────
    if jid and jid in _active_email_jobs and not _active_email_jobs[jid]["done"]:
        # Reconnect to already-running pipeline
        job = _active_email_jobs[jid]
    else:
        # Start a fresh pipeline job
        job = {"events": [], "done": False, "lock": threading.Lock()}
        if jid:
            _active_email_jobs[jid] = job

        def send(evt_type, **kwargs):
            """Store event in job registry — WS handler polls this list."""
            payload = {"type": evt_type, **kwargs}
            with job["lock"]:
                job["events"].append(payload)

        def run():
            import sys
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                asyncio.set_event_loop(asyncio.ProactorEventLoop())
            try:
                _run_email_pipeline(send, queue.Queue(), data, uid, bid)
            except Exception as e:
                import traceback
                send("log", text=f"Pipeline error: {e}\n{traceback.format_exc()}", level="error")
            finally:
                with job["lock"]:
                    job["done"] = True
                send("ws_done")

        threading.Thread(target=run, daemon=True).start()

    # ── Stream events to this WebSocket client ─────────────────
    # Polls job["events"] for new items. On disconnect the pipeline
    # keeps running; the client can reconnect and replay from offset.
    poll_idx = event_offset
    try:
        import time as _time
        last_ping = _time.monotonic()
        while True:
            with job["lock"]:
                new_events = job["events"][poll_idx:]
                poll_idx   = len(job["events"])
            for ev in new_events:
                await websocket.send_text(json.dumps(ev))

            if job["done"] and poll_idx >= len(job["events"]):
                break

            try:
                await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
            except asyncio.TimeoutError:
                now = _time.monotonic()
                if now - last_ping >= 20:
                    try:
                        await websocket.send_text(json.dumps({"type": "ping"}))
                        last_ping = now
                    except Exception:
                        break
    except WebSocketDisconnect:
        pass  # Pipeline keeps running; client may reconnect
    except Exception as e:
        print("Email WS error:", e)
