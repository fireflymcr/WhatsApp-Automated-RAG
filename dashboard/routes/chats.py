"""
Chat & message routes for the Dashboard.
"""
import os
import json
import asyncio
import math
import struct
import yaml
import logging
import requests
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from openai import OpenAI

from db import bridge_query, sql_query, sql_execute, INSTANCE

logger = logging.getLogger("dashboard.chats")
router = APIRouter()
templates = Jinja2Templates(directory="templates")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/context.yaml")


@router.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, q: str = ""):
    query_filter = ""
    params = ()
    if q:
        query_filter = "WHERE c.name LIKE ? OR c.jid LIKE ?"
        params = (f"%{q}%", f"%{q}%")
    chats = bridge_query(f"""
        SELECT c.jid, c.name, c.last_message_time,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_jid = c.jid AND m.is_from_me = 0) as msg_count,
               (SELECT content FROM messages m WHERE m.chat_jid = c.jid ORDER BY m.timestamp DESC LIMIT 1) as last_msg
        FROM chats c {query_filter}
        ORDER BY c.last_message_time DESC
        LIMIT 50
    """, params)
    return templates.TemplateResponse(request, "chats.html", {
        "page": "chats",
        "chats": chats, "search": q, "instance_name": INSTANCE,
    })


@router.get("/chats/{jid:path}", response_class=HTMLResponse)
async def chat_messages(request: Request, jid: str, page: int = 0):
    limit = 50
    offset = page * limit
    messages = bridge_query("""
        SELECT m.id, m.chat_jid, m.sender, m.content, m.timestamp,
               m.is_from_me, m.media_type, c.name as chat_name
        FROM messages m JOIN chats c ON m.chat_jid = c.jid
        WHERE m.chat_jid = ? AND m.content IS NOT NULL AND m.content != ''
        ORDER BY m.timestamp DESC LIMIT ? OFFSET ?
    """, (jid, limit, offset))
    messages.reverse()
    chat_info = bridge_query("SELECT jid, name FROM chats WHERE jid = ?", (jid,))
    chat_name = chat_info[0]["name"] if chat_info else jid
    return templates.TemplateResponse(request, "messages.html", {
        "page": "chats",
        "messages": messages, "chat_jid": jid, "chat_name": chat_name,
        "current_page": page, "instance_name": INSTANCE,
    })


@router.get("/api/sse/chats")
async def sse_chats(request: Request):
    async def event_gen():
        last_count = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                rows = bridge_query("SELECT COUNT(*) as cnt FROM messages")
                count = rows[0]["cnt"] if rows else 0
                if count != last_count:
                    last_count = count
                    chats = bridge_query("""
                        SELECT c.jid, c.name, c.last_message_time,
                               (SELECT content FROM messages m WHERE m.chat_jid = c.jid ORDER BY m.timestamp DESC LIMIT 1) as last_msg
                        FROM chats c ORDER BY c.last_message_time DESC LIMIT 20
                    """)
                    yield {"event": "chats_update", "data": json.dumps(chats)}
            except Exception:
                pass
            await asyncio.sleep(5)
    return EventSourceResponse(event_gen())


def _retrieve_rag_context(message_content: str, config: dict) -> str:
    try:
        llm_cfg = config.get("llm", {})
        base_url = llm_cfg.get("base_url", "http://host.docker.internal:12344/v1")
        api_key = llm_cfg.get("api_key", "")
        
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            
        resp = requests.post(
            f"{base_url.rstrip('/')}/embeddings",
            json={"input": message_content, "model": "text-embedding-nomic-embed-text-v1.5"},
            headers=headers,
            timeout=15
        )
        if resp.status_code != 200:
            return ""
            
        query_emb = resp.json()["data"][0]["embedding"]
        chunks = sql_query(f"SELECT question_text, answer_text, full_context, embedding FROM {INSTANCE}_training_chunks WHERE embedding IS NOT NULL")
        
        scored_chunks = []
        for chunk in chunks:
            db_emb_bytes = chunk.get("embedding")
            if not db_emb_bytes:
                continue
            
            count = len(db_emb_bytes) // 4
            chunk_emb = list(struct.unpack(f"{count}f", db_emb_bytes))
            
            if len(chunk_emb) != len(query_emb):
                continue
            
            dot = sum(a * b for a, b in zip(query_emb, chunk_emb))
            norm_q = math.sqrt(sum(a * a for a in query_emb))
            norm_c = math.sqrt(sum(c * c for c in chunk_emb))
            sim = dot / (norm_q * norm_c + 1e-8)
            
            scored_chunks.append((sim, chunk))
            
        scored_chunks.sort(key=lambda x: x[0], reverse=True)
        top_chunks = [item[1] for item in scored_chunks[:3] if item[0] > 0.4]
        
        if not top_chunks:
            return ""
            
        parts = ["=== HISTORICAL CONVERSATION EXAMPLES (For Wording, Tone & Context Reference Only) ==="]
        for i, c in enumerate(top_chunks, 1):
            q = c.get("question_text", "").strip()
            a = c.get("answer_text", "").strip()
            if q and a:
                parts.append(f"Historical Example {i}:\nCustomer query: \"{q}\"\nStandard Response: \"{a}\"")
            elif c.get("full_context"):
                parts.append(f"Historical Context {i}:\n{c['full_context'].strip()}")
        parts.append("IMPORTANT: The historical context examples above are for conversational tone and style reference ONLY. The main System Prompt rules and pricing guidelines have absolute priority.")
        parts.append("======================================================================================")
        return "\n\n".join(parts)
    except Exception as e:
        logger.error(f"Suggested reply RAG context lookup failed: {e}")
        return ""


@router.post("/api/chats/{chat_jid:path}/suggest-reply")
async def suggest_reply(chat_jid: str):
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
            
        llm_cfg = config.get("llm", {})
        base_url = llm_cfg.get("base_url", "http://host.docker.internal:12344/v1")
        api_key = llm_cfg.get("api_key", "")
        model = llm_cfg.get("model", "qwen/qwen3.6-27b")
        temperature = llm_cfg.get("temperature", 0.7)
        max_tokens = llm_cfg.get("max_tokens", 2000)
        
        history = bridge_query("""
            SELECT content, is_from_me, sender
            FROM messages
            WHERE chat_jid = ? AND content IS NOT NULL AND content != ''
            ORDER BY timestamp DESC LIMIT 30
        """, (chat_jid,))
        history.reverse()
        
        if not history:
            return {"success": False, "error": "No chat history found"}
            
        latest_cust_msg = ""
        for m in reversed(history):
            if not m["is_from_me"]:
                latest_cust_msg = m["content"]
                break
        
        if not latest_cust_msg:
            latest_cust_msg = history[-1]["content"]
            
        rag_context = _retrieve_rag_context(latest_cust_msg, config)
        
        system_prompt = config.get("system_prompt", "")
        if rag_context:
            system_prompt += f"\n\n{rag_context}"
            
        system_prompt += (
            "\n\n=== MAIN BOT CONFIGURATION SYSTEM PROMPT DIRECTIVE (SUPREME PRIORITY) ===\n"
            "1. The configured SYSTEM PROMPT rules and CONTACT DETAILS above are your ABSOLUTE AND SUPREME AUTHORITIES.\n"
            "2. Do NOT copy, imitate, or rely on any conflicting prices, deposit details, bank accounts, or procedures from any historical examples below. The main rules above MUST override any historical examples.\n"
            "3. The 'HISTORICAL CONVERSATION EXAMPLES' provided below are ONLY to help you understand general vocabulary, tone of voice, and stylistic preferences. They are pure reference material. The actual rules, bank details, and procedures in the main System Prompt are the absolute truth.\n"
            "4. Specifically, for booking confirmations: ALWAYS follow the 'BOOKING RULES & CONFIRMATIONS' section in the main system prompt. If they have not provided all 5 details (Name, Address, Date/Time, Clean Type, Price), do NOT confirm the booking! Ask them to confirm the missing details first.\n"
            "============================================================"
        )
            
        openai_client = OpenAI(base_url=base_url, api_key=api_key)
        
        history_lines = []
        for m in history:
            sender_label = "Business (Us)" if m["is_from_me"] else "Customer"
            history_lines.append(f"[{sender_label}]: {m['content']}")
        history_text = "\n".join(history_lines)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Here is the recent conversation history:\n"
                    f"=========================================\n"
                    f"{history_text}\n"
                    f"=========================================\n\n"
                    f"Write the next response as the business. "
                    f"Keep it concise, specific, and natural for WhatsApp. "
                    f"Follow all configured system prompt rules strictly."
                )
            }
        ]
        
        response = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )
        
        suggestion = response.choices[0].message.content.strip()
        return {"success": True, "suggestion": suggestion}
        
    except Exception as e:
        logger.error(f"Failed to suggest reply: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/chats/{chat_jid:path}/send")
async def send_dashboard_reply(chat_jid: str, message: str = Form(...)):
    try:
        url = "http://bridge-cleaner:8080/api/send"
        payload = {"recipient": chat_jid, "message": message}
        resp = requests.post(url, json=payload, timeout=15)
        
        if resp.status_code == 200 and resp.json().get("success"):
            return {"success": True}
        else:
            return {"success": False, "error": f"Bridge returned status {resp.status_code}: {resp.text}"}
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return {"success": False, "error": str(e)}
