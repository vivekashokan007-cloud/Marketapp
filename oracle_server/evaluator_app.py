import os
import time
import json
import asyncio
from fastapi import FastAPI, Request, HTTPException
import google.generativeai as genai
from pydantic import BaseModel
from typing import List, Dict, Any

app = FastAPI(title="Market Radar Oracle")

# Configure Gemini
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("WARNING: GEMINI_API_KEY environment variable not set.")
genai.configure(api_key=api_key)

# The models
flash_model = genai.GenerativeModel('gemini-2.5-flash')
pro_model = genai.GenerativeModel('gemini-2.5-pro')

@app.get("/health")
async def health_check():
    return {"status": "ok", "ts": time.time()}

@app.post("/elephant")
async def elephant_evaluate(request: Request):
    """
    The daily Elephant endpoint (Wave 2).
    Uses Gemini Flash to evaluate candidates. Enforces 15 max candidates.
    Returns safe-WAIT on any error.
    """
    try:
        body = await request.json()
        candidates = body.get("candidates", [])
        
        # Enforce max 15 candidates
        if len(candidates) > 15:
            candidates = candidates[:15]
            
        if not candidates:
            return {"status": "ok", "verdict": "NO_CANDIDATES"}

        prompt = f"""
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

        # We must enforce a timeout off the main thread
        # The prompt is explicitly requesting JSON.
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

        # 12 second hard timeout per the directive
        response = await asyncio.wait_for(call_gemini(), timeout=12.0)
        
        try:
            result_json = json.loads(response.text)
            return {"status": "ok", "verdict": result_json}
        except json.JSONDecodeError:
            # If the model hallucinated non-JSON
            return {"status": "WAIT", "reason": "model_returned_invalid_json"}
            
    except asyncio.TimeoutError:
        print("Elephant Evaluation timed out after 12s.")
        return {"status": "WAIT", "reason": "elephant_timeout"}
    except Exception as e:
        print(f"Elephant Error: {e}")
        return {"status": "WAIT", "reason": "internal_error"}


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
