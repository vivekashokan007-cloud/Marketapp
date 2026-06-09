import os
import time
import json
import asyncio
from datetime import datetime, timezone
from urllib import error as urllib_error
from urllib import request as urllib_request
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import google.generativeai as genai

app = FastAPI(title="Market Radar Oracle")

# Configure Gemini
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("WARNING: GEMINI_API_KEY environment variable not set.")
genai.configure(api_key=api_key)

# The models
flash_model = genai.GenerativeModel('gemini-2.5-flash')
pro_model = genai.GenerativeModel('gemini-2.5-pro')
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def persist_elephant_assessment(poll_timestamp: str, lane: str, assessments: dict) -> bool:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        print("ELEPHANT_PERSIST_FAIL: missing Supabase env")
        return False

    endpoint = (
        f"{SUPABASE_URL}/rest/v1/elephant_assessments"
        "?on_conflict=poll_timestamp,lane"
    )
    payload = json.dumps([{
        "poll_timestamp": poll_timestamp,
        "lane": lane,
        "assessments": assessments,
    }]).encode("utf-8")
    request = urllib_request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except urllib_error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<unreadable>"
        print(f"ELEPHANT_PERSIST_FAIL HTTP {exc.code}: {body}")
    except Exception as exc:
        print(f"ELEPHANT_PERSIST_FAIL: {exc}")
    return False


def build_elephant_prompt(candidates) -> str:
    return f"""
        You are a quantitative options trading evaluator.
        Analyze these candidate strategies and provide a JSON response.

        Candidates: {json.dumps(candidates)}

        Return exactly in this JSON schema:
        {{
            "judgments": [
                {{
                    "sellStrike": 12345,
                    "confidence": 0.85,
                    "reasoning": "Clear edge found...",
                    "approved": true
                }}
            ]
        }}
        """


async def process_elephant_async(body: dict) -> None:
    lane = str(body.get("lane", "")).strip()
    poll_timestamp = str(body.get("poll_timestamp", "")).strip()
    quality_tag = str(body.get("quality_tag", "placeholder_prompt_era")).strip() or "placeholder_prompt_era"
    observe_only = bool(body.get("observe_only", False))
    original_candidates = body.get("candidates", []) or []
    trimmed_candidates = original_candidates[:15]

    if not lane or not poll_timestamp:
        print("ELEPHANT_BACKGROUND_SKIP: missing lane/poll_timestamp")
        return

    if not trimmed_candidates:
        response_payload = {"status": "ok", "verdict": "NO_CANDIDATES"}
    else:
        prompt = build_elephant_prompt(trimmed_candidates)

        async def call_gemini():
            response = await asyncio.to_thread(
                flash_model.generate_content,
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.1,
                    response_mime_type="application/json"
                )
            )
            return response

        try:
            response = await asyncio.wait_for(call_gemini(), timeout=12.0)
            try:
                result_json = json.loads(response.text)
                response_payload = {"status": "ok", "verdict": result_json}
            except json.JSONDecodeError:
                response_payload = {"status": "WAIT", "reason": "model_returned_invalid_json"}
        except asyncio.TimeoutError:
            print("Elephant Evaluation timed out after 12s.")
            response_payload = {"status": "WAIT", "reason": "elephant_timeout"}
        except Exception as exc:
            print(f"Elephant Error: {exc}")
            response_payload = {"status": "WAIT", "reason": "internal_error"}

    assessments = {
        "request": body,
        "response": response_payload,
        "status": response_payload.get("status", "WAIT"),
        "observe_only": observe_only,
        "quality_tag": quality_tag,
        "persisted_at": now_utc_iso(),
    }
    persisted = persist_elephant_assessment(poll_timestamp, lane, assessments)
    print(
        "ELEPHANT_BACKGROUND_DONE "
        f"lane={lane} status={response_payload.get('status', 'WAIT')} persisted={persisted}"
    )


def process_elephant_background(body: dict) -> None:
    asyncio.run(process_elephant_async(body))

@app.get("/health")
async def health_check():
    return {"status": "ok", "ts": time.time()}

@app.post("/elephant")
async def elephant_evaluate(request: Request, background_tasks: BackgroundTasks):
    """
    Observe-only Elephant handoff.
    Returns immediate acceptance, then persists the real result asynchronously.
    """
    try:
        body = await request.json()
        lane = str(body.get("lane", "")).strip()
        poll_timestamp = str(body.get("poll_timestamp", "")).strip()
        if not lane or not poll_timestamp:
            raise HTTPException(status_code=400, detail="Missing lane or poll_timestamp")
        background_tasks.add_task(process_elephant_background, body)
        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "observe_only": True,
                "lane": lane,
                "poll_timestamp": poll_timestamp,
            }
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Elephant ACK Error: {exc}")
        return JSONResponse(
            status_code=500,
            content={"status": "WAIT", "reason": "ack_failed"}
        )


@app.post("/monthly_eval")
async def monthly_eval(request: Request):
    """
    The monthly deep review endpoint.
    Uses Gemini Pro.
    """
    try:
        body = await request.json()
        
        prompt = f"""
        You are a quantitative trading system supervisor.
        Review this month's data and provide structural feedback in JSON.
        
        Data: {json.dumps(body.get('data', {}))[:100000]} # Cap input to avoid blowing up memory
        
        Return JSON schema:
        {{
            "insights": ["insight 1", "insight 2"],
            "calibration_adjustments": {{"volatility_bias": -0.05}}
        }}
        """
        
        response = await asyncio.to_thread(
            pro_model.generate_content,
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,
                response_mime_type="application/json"
            )
        )
        return {"status": "ok", "review": json.loads(response.text)}
        
    except Exception as e:
        print(f"Monthly Eval Error: {e}")
        raise HTTPException(status_code=500, detail="Review failed")
