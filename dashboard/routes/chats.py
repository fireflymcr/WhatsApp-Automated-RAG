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
from typing import List, Optional
from fastapi import APIRouter, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from openai import OpenAI

from db import bridge_query, sql_query, sql_execute, INSTANCE
from .bot import DEFAULT_EMAIL_SUBJECT, DEFAULT_EMAIL_BODY_HTML, safe_format

logger = logging.getLogger("dashboard.chats")
router = APIRouter()
templates = Jinja2Templates(directory="templates")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/context.yaml")
def ensure_chat_status_table():
    try:
        sql_execute(f"""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{INSTANCE}_chat_status' AND xtype='U')
            BEGIN
                CREATE TABLE {INSTANCE}_chat_status (
                    jid NVARCHAR(255) PRIMARY KEY,
                    is_pinned BIT DEFAULT 0,
                    is_deleted BIT DEFAULT 0,
                    custom_name NVARCHAR(255) NULL
                )
            END
            ELSE
            BEGIN
                IF NOT EXISTS (SELECT * FROM syscolumns WHERE id=object_id('{INSTANCE}_chat_status') AND name='custom_name')
                BEGIN
                    ALTER TABLE {INSTANCE}_chat_status ADD custom_name NVARCHAR(255) NULL
                END
            END
        """)
    except Exception as e:
        logger.error(f"Failed to ensure {INSTANCE}_chat_status table exists: {e}")


@router.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, q: str = ""):
    ensure_chat_status_table()
    query_filter = ""
    params = ()
    if q:
        query_filter = "WHERE c.name LIKE ? OR c.jid LIKE ?"
        params = (f"%{q}%", f"%{q}%")
    
    # Increase limit to 150 to account for deleted chats filtered out in Python
    chats = bridge_query(f"""
        SELECT c.jid, c.name, c.last_message_time,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_jid = c.jid AND m.is_from_me = 0) as msg_count,
               (SELECT content FROM messages m WHERE m.chat_jid = c.jid ORDER BY m.timestamp DESC LIMIT 1) as last_msg
        FROM chats c {query_filter}
        ORDER BY c.last_message_time DESC
        LIMIT 150
    """, params)

    # Fetch chat status settings from SQL Server
    chat_status = {}
    try:
        status_rows = sql_query(f"SELECT jid, is_pinned, is_deleted, custom_name FROM {INSTANCE}_chat_status")
        chat_status = {row["jid"]: row for row in status_rows}
    except Exception as e:
        logger.error(f"Failed to fetch chat status from SQL Server: {e}")

    filtered_chats = []
    for c in chats:
        jid = c["jid"]
        status = chat_status.get(jid, {})
        c["is_pinned"] = bool(status.get("is_pinned", 0))
        c["is_deleted"] = bool(status.get("is_deleted", 0))
        c["custom_name"] = status.get("custom_name")
        if c["custom_name"]:
            c["name"] = c["custom_name"]
        if not c["is_deleted"]:
            filtered_chats.append(c)

    # Sort: Pinned first (1 before 0), then last_message_time descending
    filtered_chats.sort(key=lambda x: (1 if x["is_pinned"] else 0, x.get("last_message_time") or ""), reverse=True)

    return templates.TemplateResponse(request, "chats.html", {
        "page": "chats",
        "chats": filtered_chats[:50], "search": q, "instance_name": INSTANCE,
    })


@router.get("/chats/{jid:path}", response_class=HTMLResponse)
async def chat_messages(request: Request, jid: str, page: int = 0):
    limit = 50
    offset = page * limit
    messages = bridge_query("""
        SELECT m.id, m.chat_jid, m.sender, m.content, m.timestamp,
               m.is_from_me, m.media_type, m.filename, c.name as chat_name
        FROM messages m JOIN chats c ON m.chat_jid = c.jid
        WHERE m.chat_jid = ? AND ((m.content IS NOT NULL AND m.content != '') OR (m.media_type IS NOT NULL AND m.media_type != ''))
        ORDER BY m.timestamp DESC LIMIT ? OFFSET ?
    """, (jid, limit, offset))
    messages.reverse()
    
    # Preprocess messages to set media flags and ensure filenames
    for msg in messages:
        if msg.get("media_type"):
            filename = msg.get("filename")
            media_type = msg.get("media_type") or ""
            
            # If filename is empty, assign a default with correct extension
            if not filename:
                ext = ".bin"
                if "image" in media_type:
                    ext = ".jpg"
                elif "video" in media_type:
                    ext = ".mp4"
                elif "audio" in media_type:
                    ext = ".ogg"
                filename = f"media_{msg['id']}{ext}"
                msg["filename"] = filename
                
            filename_lower = filename.lower()
            is_image = "image" in media_type or filename_lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'))
            is_video = "video" in media_type or filename_lower.endswith(('.mp4', '.mov', '.m4v', '.webm', '.3gp'))
            is_audio = "audio" in media_type or filename_lower.endswith(('.ogg', '.mp3', '.wav', '.m4a', '.aac'))
            
            msg["is_image"] = is_image
            msg["is_video"] = is_video
            msg["is_audio"] = is_audio
            msg["is_document"] = not (is_image or is_video or is_audio)
                    
    chat_info = bridge_query("SELECT jid, name FROM chats WHERE jid = ?", (jid,))
    chat_name = chat_info[0]["name"] if chat_info else jid
    
    # Check for custom name in SQL Server
    ensure_chat_status_table()
    try:
        status_rows = sql_query(f"SELECT custom_name FROM {INSTANCE}_chat_status WHERE jid = %s", (jid,))
        if status_rows and status_rows[0].get("custom_name"):
            chat_name = status_rows[0]["custom_name"]
    except Exception as e:
        logger.error(f"Failed to check custom name for {jid} in SQL Server: {e}")

    return templates.TemplateResponse(request, "messages.html", {
        "page": "chats",
        "messages": messages, "chat_jid": jid, "chat_name": chat_name,
        "current_page": page, "instance_name": INSTANCE,
    })


@router.get("/media-file/{chat_jid}/{msg_id}/{filename}")
def get_media_file(chat_jid: str, msg_id: str, filename: str):
    chat_folder = chat_jid.replace(":", "_")
    local_dir = f"/data/bridge-store/{chat_folder}"
    local_path = f"{local_dir}/{filename}"
    
    # If the file doesn't exist, try to download it on-demand
    if not os.path.exists(local_path):
        try:
            logger.info(f"On-demand downloading media {filename} for message {msg_id} in chat {chat_jid}")
            resp = requests.post("http://bridge-cleaner:8080/api/download", json={
                "message_id": msg_id,
                "chat_jid": chat_jid
            }, timeout=30)
            
            # Check if download succeeded and file now exists
            if resp.status_code == 200:
                data = resp.json()
                new_filename = data.get("filename")
                if new_filename:
                    filename = new_filename
                    local_path = f"{local_dir}/{filename}"
        except Exception as ex:
            logger.error(f"Failed to download media on-demand for msg {msg_id}: {ex}")
            
    if os.path.exists(local_path):
        # Infer content type based on extension
        filename_lower = filename.lower()
        media_type = "application/octet-stream"
        if filename_lower.endswith(('.jpg', '.jpeg')):
            media_type = "image/jpeg"
        elif filename_lower.endswith('.png'):
            media_type = "image/png"
        elif filename_lower.endswith('.webp'):
            media_type = "image/webp"
        elif filename_lower.endswith('.gif'):
            media_type = "image/gif"
        elif filename_lower.endswith('.mp4'):
            media_type = "video/mp4"
        elif filename_lower.endswith('.mov'):
            media_type = "video/quicktime"
        elif filename_lower.endswith('.ogg'):
            media_type = "audio/ogg"
        elif filename_lower.endswith('.mp3'):
            media_type = "audio/mpeg"
        
        return FileResponse(local_path, media_type=media_type)
        
    return Response(status_code=404)


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
                    ensure_chat_status_table()
                    # Fetch more chats to account for soft-deleted ones
                    chats = bridge_query("""
                        SELECT c.jid, c.name, c.last_message_time,
                               (SELECT content FROM messages m WHERE m.chat_jid = c.jid ORDER BY m.timestamp DESC LIMIT 1) as last_msg
                        FROM chats c ORDER BY c.last_message_time DESC LIMIT 100
                    """)
                    
                    chat_status = {}
                    try:
                        status_rows = sql_query(f"SELECT jid, is_pinned, is_deleted, custom_name FROM {INSTANCE}_chat_status")
                        chat_status = {row["jid"]: row for row in status_rows}
                    except Exception as e:
                        logger.error(f"Failed to fetch chat status in SSE: {e}")
                        
                    filtered_chats = []
                    for c in chats:
                        jid = c["jid"]
                        status = chat_status.get(jid, {})
                        c["is_pinned"] = bool(status.get("is_pinned", 0))
                        c["is_deleted"] = bool(status.get("is_deleted", 0))
                        c["custom_name"] = status.get("custom_name")
                        if c["custom_name"]:
                            c["name"] = c["custom_name"]
                        if not c["is_deleted"]:
                            filtered_chats.append(c)
                            
                    filtered_chats.sort(key=lambda x: (1 if x["is_pinned"] else 0, x.get("last_message_time") or ""), reverse=True)
                    yield {"event": "chats_update", "data": json.dumps(filtered_chats[:20])}
            except Exception:
                pass
            await asyncio.sleep(5)
    return EventSourceResponse(event_gen())


@router.post("/api/chats/{chat_jid:path}/toggle-pin")
async def toggle_pin_chat(chat_jid: str):
    ensure_chat_status_table()
    try:
        # Check if chat status exists
        rows = sql_query(f"SELECT is_pinned FROM {INSTANCE}_chat_status WHERE jid = %s", (chat_jid,))
        if rows:
            new_pin = 0 if rows[0]["is_pinned"] else 1
            sql_execute(f"UPDATE {INSTANCE}_chat_status SET is_pinned = %s WHERE jid = %s", (new_pin, chat_jid))
        else:
            sql_execute(f"INSERT INTO {INSTANCE}_chat_status (jid, is_pinned, is_deleted) VALUES (%s, 1, 0)", (chat_jid,))
        return Response(headers={"HX-Trigger": "chatListChanged"})
    except Exception as e:
        logger.error(f"Failed to toggle pin for chat {chat_jid}: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/chats/{chat_jid:path}/delete")
async def delete_chat(chat_jid: str):
    ensure_chat_status_table()
    try:
        # Mark as deleted (soft delete)
        rows = sql_query(f"SELECT jid FROM {INSTANCE}_chat_status WHERE jid = %s", (chat_jid,))
        if rows:
            sql_execute(f"UPDATE {INSTANCE}_chat_status SET is_deleted = 1 WHERE jid = %s", (chat_jid,))
        else:
            sql_execute(f"INSERT INTO {INSTANCE}_chat_status (jid, is_pinned, is_deleted) VALUES (%s, 0, 1)", (chat_jid,))
        return Response(headers={"HX-Trigger": "chatListChanged"})
    except Exception as e:
        logger.error(f"Failed to delete chat {chat_jid}: {e}")
        return {"success": False, "error": str(e)}


from pydantic import BaseModel

class RenamePayload(BaseModel):
    name: str

@router.post("/api/chats/{chat_jid:path}/rename")
async def rename_chat(chat_jid: str, payload: RenamePayload):
    ensure_chat_status_table()
    new_name = payload.name.strip()
    if not new_name:
        return {"success": False, "error": "Name cannot be empty"}
    try:
        rows = sql_query(f"SELECT jid FROM {INSTANCE}_chat_status WHERE jid = %s", (chat_jid,))
        if rows:
            sql_execute(f"UPDATE {INSTANCE}_chat_status SET custom_name = %s WHERE jid = %s", (new_name, chat_jid))
        else:
            sql_execute(f"INSERT INTO {INSTANCE}_chat_status (jid, is_pinned, is_deleted, custom_name) VALUES (%s, 0, 0, %s)", (chat_jid, new_name))
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to rename chat {chat_jid}: {e}")
        return {"success": False, "error": str(e)}


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
            SELECT content, is_from_me, sender, media_type, filename
            FROM messages
            WHERE chat_jid = ? AND ((content IS NOT NULL AND content != '') OR (media_type IS NOT NULL AND media_type != ''))
            ORDER BY timestamp DESC LIMIT 1000
        """, (chat_jid,))
        history.reverse()
        
        if not history:
            return {"success": False, "error": "No chat history found"}
            
        latest_cust_msg = ""
        for m in reversed(history):
            if not m["is_from_me"]:
                latest_cust_msg = m.get("content") or ""
                if m.get("media_type") and not latest_cust_msg:
                    latest_cust_msg = f"[Sent a {m['media_type']} file]"
                break
        
        if not latest_cust_msg:
            latest_cust_msg = history[-1].get("content") or ""
            if history[-1].get("media_type") and not latest_cust_msg:
                latest_cust_msg = f"[Sent a {history[-1]['media_type']} file]"
            
        rag_context = _retrieve_rag_context(latest_cust_msg, config)
        
        # ── Retrieve Existing Bookings & DPA rules ──
        dpa_verification_prompt = ""
        try:
            bookings = sql_query(f"""
                SELECT customer_name, customer_email, address, clean_date, clean_type, price, status, clean_status, notes 
                FROM {INSTANCE}_appointments 
                WHERE chat_jid = %s
            """, (chat_jid,))
            if bookings:
                dpa_verification_prompt = "\n\n=== CUSTOMER'S EXISTING BOOKINGS ON THE CALENDAR (FOR REFERENCE ONLY) ===\n"
                for b in bookings:
                    dpa_verification_prompt += (
                        f"- Name: {b['customer_name']}\n"
                        f"  Email: {b['customer_email']}\n"
                        f"  Address: {b['address']}\n"
                        f"  Date & Time: {b['clean_date']}\n"
                        f"  Type: {b['clean_type']}\n"
                        f"  Price: {b['price']}\n"
                        f"  Deposit Status: {'PAID' if b['status'] == 'confirmed' else 'UNPAID'}\n"
                        f"  Clean Status: {b['clean_status'] or 'pending'}\n"
                        f"  Notes: {b['notes'] or ''}\n\n"
                    )
                dpa_verification_prompt += "========================================================================\n\n"
                
                # Extract first line of address for verification key
                first_app = bookings[0]
                addr = first_app.get("address") or ""
                first_line = addr.split(",")[0].strip()
                email = first_app.get("customer_email") or ""
                clean_date = first_app.get("clean_date") or ""
                
                dpa_verification_prompt += f"""=== CRITICAL DPA SECURITY VERIFICATION RULE ===
This customer has an existing booking on our calendar.
Under the Data Protection Act (DPA), you MUST NOT confirm, disclose, or discuss any details about their booking (including its existence, date, time, address, type, or price) until they have successfully verified their identity in the chat history.

To verify, the customer MUST provide the following three details:
1. The first line of their clean address (Must match: "{first_line}")
2. Their email address (Must match: "{email}")
3. The date and time of their clean (Must match: "{clean_date}")

Check the previous messages in the conversation history carefully:
- If the customer has ALREADY correctly provided ALL THREE verification details, they are verified. You may proceed to discuss and update their booking as requested.
- If they have NOT yet provided all 3 details correctly, you MUST NOT reveal any details about their booking. Instead, politely explain that for security you must verify their details first, and explicitly ask them to confirm all 3 details (1st line of clean address, email address, and date/time of the clean).
Example response: "Before we can discuss or update your booking, I just need to verify a few details for security. Could you please confirm the first line of your clean address, your email address, and the date and time of your scheduled clean?"
==============================================="""
        except Exception as e:
            logger.error(f"Error fetching existing bookings for DPA context in chats: {e}")
        
        system_prompt = config.get("system_prompt", "")
        if rag_context:
            system_prompt += f"\n\n{rag_context}"
            
        if dpa_verification_prompt:
            system_prompt += dpa_verification_prompt
            
        openai_client = OpenAI(base_url=base_url, api_key=api_key)
        
        history_lines = []
        for m in history:
            sender_label = "Business (Us)" if m["is_from_me"] else "Customer"
            msg_content = m.get("content") or ""
            if m.get("media_type"):
                media_info = f"[Sent a {m['media_type']} file: {m['filename'] or 'attachment'}]"
                msg_content = f"{media_info} {msg_content}".strip()
            history_lines.append(f"[{sender_label}]: {msg_content}")
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
        
        logger.info(f"Sending prompt to LM Studio ({base_url}, model={model}):")
        logger.info(f"System Prompt Length: {len(system_prompt)}")
        logger.info(f"User Message: {messages[1]['content']}")
        
        response = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature
        )
        
        raw_content = response.choices[0].message.content
        logger.info(f"Raw LLM Response: '{raw_content}'")
        suggestion = raw_content.strip()
        return {"success": True, "suggestion": suggestion}
        
    except Exception as e:
        logger.error(f"Failed to suggest reply: {e}")
        return {"success": False, "error": str(e)}


def parse_booking_confirmation(message: str):
    if "booking confirmation" not in message.lower():
        return None
        
    import re
    lines = [line.strip() for line in message.split("\n")]
    
    data = {
        "customer_name": None,
        "customer_email": None,
        "address": None,
        "clean_date": None,
        "clean_type": None,
        "price": None
    }
    
    patterns = {
        "customer_name": re.compile(r"(?:👤|name)\s*(?:\*\*?)?name(?:\*\*?)?\s*:\s*(.+)", re.IGNORECASE),
        "customer_email": re.compile(r"(?:📧|email)\s*(?:\*\*?)?email(?:\*\*?)?\s*:\s*(.+)", re.IGNORECASE),
        "address": re.compile(r"(?:🏡|address)\s*(?:\*\*?)?address(?:\*\*?)?\s*:\s*(.+)", re.IGNORECASE),
        "clean_date": re.compile(r"(?:📅|date)\s*(?:\*\*?)?(?:date\s*&\s*time|date)(?:\*\*?)?\s*:\s*(.+)", re.IGNORECASE),
        "clean_type": re.compile(r"(?:🧹|clean)\s*(?:\*\*?)?(?:type\s*of\s*clean|clean\s*type)(?:\*\*?)?\s*:\s*(.+)", re.IGNORECASE),
        "price": re.compile(r"(?:💰|price)\s*(?:\*\*?)?(?:total\s*agreed\s*price|agreed\s*price|price)(?:\*\*?)?\s*:\s*(.+)", re.IGNORECASE)
    }
    
    for line in lines:
        for field, pattern in patterns.items():
            match = pattern.search(line)
            if match:
                val = match.group(1).strip()
                val = re.sub(r"\s*\*+\s*$", "", val)
                val = re.sub(r"^\s*\*+\s*", "", val)
                data[field] = val.strip()
                
    if data["customer_name"]:
        return data
    return None


def send_auto_booking_confirmation_email(booking: dict):
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
            
        resend_api_key = config.get("resend", {}).get("api_key")
        resend_from = config.get("resend", {}).get("from_email", "onboarding@resend.dev")
        
        if not resend_api_key:
            logger.warning("Resend API Key not configured. Skipping auto confirmation email.")
            return False
            
        import re
        address_str = booking.get("address") or ""
        postcode_match = re.search(r'([A-Z]{1,2}[0-9R][0-9A-Z]?\s*[0-9][A-Z]{2})', address_str.upper())
        postcode = postcode_match.group(1) if postcode_match else (booking.get("customer_name") or "CLIENT").split()[0].upper()
        
        # Get custom template from config or use defaults
        resend_subject = config.get("resend", {}).get("email_subject", DEFAULT_EMAIL_SUBJECT)
        resend_body = config.get("resend", {}).get("email_body_html", DEFAULT_EMAIL_BODY_HTML)
        
        # Format variables safely
        formatted_subject = safe_format(
            resend_subject,
            customer_name=booking["customer_name"],
            address=address_str,
            clean_date=booking["clean_date"],
            clean_type=booking["clean_type"] or 'Clean',
            price=booking["price"],
            postcode=postcode
        )
        formatted_body = safe_format(
            resend_body,
            customer_name=booking["customer_name"],
            address=address_str,
            clean_date=booking["clean_date"],
            clean_type=booking["clean_type"] or 'Clean',
            price=booking["price"],
            postcode=postcode
        )
        
        res = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": resend_from,
                "to": [booking["customer_email"]],
                "cc": ["info@0161cleanerinmanchester.co.uk"],
                "subject": formatted_subject,
                "html": formatted_body
            },
            timeout=10
        )
        
        status = "success" if res.status_code == 200 else "failed"
        error_msg = None if res.status_code == 200 else f"HTTP {res.status_code}: {res.text}"
        
        # Log to database
        try:
            sql_execute(f"""
                INSERT INTO {INSTANCE}_email_log (recipient, cc, subject, body, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (booking["customer_email"], "info@0161cleanerinmanchester.co.uk", formatted_subject, formatted_body, status, error_msg))
        except Exception as log_ex:
            logger.error(f"Failed to log auto confirmation email: {log_ex}")
            
        if res.status_code == 200:
            logger.info("Successfully sent auto confirmation email to customer and CC'd admin.")
            return True
        else:
            logger.error(f"Failed to send auto confirmation email: Resend API returned {res.status_code} - {res.text}")
            return False
    except Exception as e:
        logger.error(f"Error in send_auto_booking_confirmation_email: {e}")
        return False


@router.post("/api/chats/{chat_jid:path}/send")
async def send_dashboard_reply(chat_jid: str, message: str = Form(""), file: UploadFile = File(None)):
    try:
        url = "http://bridge-cleaner:8080/api/send"
        
        if file is not None:
            file_bytes = await file.read()
            files = {"file": (file.filename, file_bytes, file.content_type)}
            data = {"recipient": chat_jid, "message": message}
            resp = requests.post(url, data=data, files=files, timeout=60)
        else:
            payload = {"recipient": chat_jid, "message": message}
            resp = requests.post(url, json=payload, timeout=15)
        
        if resp.status_code == 200 and resp.json().get("success"):
            # Load config once
            try:
                with open(CONFIG_PATH, "r") as f:
                    config = yaml.safe_load(f)
            except Exception:
                config = {}

            # Auto-save booking confirmation to database if detected
            if "booking confirmation" in message.lower():
                try:
                    booking = parse_booking_confirmation(message)
                    if booking:
                        logger.info(f"Parsed booking confirmation from dashboard message: {booking}")
                        rows = sql_query(f"SELECT id FROM {INSTANCE}_appointments WHERE chat_jid = %s AND status != 'confirmed'", (chat_jid,))
                        if rows:
                            app_id = rows[0]["id"]
                            sql_execute(f"""
                                UPDATE {INSTANCE}_appointments
                                SET customer_name=%s, customer_email=%s, address=%s, clean_date=%s, clean_type=%s, price=%s, status='pending'
                                WHERE id = %s
                            """, (booking["customer_name"], booking["customer_email"], booking["address"], booking["clean_date"], booking["clean_type"] or "Clean", booking["price"], app_id))
                            logger.info(f"Successfully updated booking appointment {app_id} as pending")
                        else:
                            sql_execute(f"""
                                INSERT INTO {INSTANCE}_appointments (chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                            """, (chat_jid, booking["customer_name"], booking["customer_email"], booking["address"], booking["clean_date"], booking["clean_type"] or "Clean", booking["price"]))
                            logger.info("Successfully inserted new booking appointment as pending")
                        
                        # Auto-send email to customer and CC admin
                        send_auto_booking_confirmation_email(booking)
                except Exception as ex:
                    logger.error(f"Failed to auto-save booking confirmation to database: {ex}", exc_info=True)
            elif _is_provisional_booking(message, config):
                try:
                    booking = _extract_booking_details(chat_jid, config)
                    if booking and booking.get("customer_name") and booking.get("clean_date"):
                        rows = sql_query(f"SELECT id, status FROM {INSTANCE}_appointments WHERE chat_jid = %s ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, created_at DESC", (chat_jid,))
                        is_update = len(rows) > 0
                        
                        if rows:
                            app_id = rows[0]["id"]
                            existing_status = rows[0].get("status") or "pending"
                            final_status = "confirmed" if existing_status == "confirmed" else "pending"
                            sql_execute(f"""
                                UPDATE {INSTANCE}_appointments
                                SET customer_name=%s, customer_email=%s, address=%s, clean_date=%s, clean_type=%s, price=%s, status=%s
                                WHERE id = %s
                            """, (booking["customer_name"], booking["customer_email"] or "", booking["address"] or "", booking["clean_date"], booking["clean_type"] or "Clean", booking["price"] or "", final_status, app_id))
                            logger.info(f"Successfully updated provisional booking appointment {app_id}")
                        else:
                            sql_execute(f"""
                                INSERT INTO {INSTANCE}_appointments (chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                            """, (chat_jid, booking["customer_name"], booking["customer_email"] or "", booking["address"] or "", booking["clean_date"], booking["clean_type"] or "Clean", booking["price"] or ""))
                            logger.info("Successfully inserted new provisional booking appointment")
                        
                        send_provisional_booking_admin_email(booking, config, is_update)
                except Exception as ex:
                    logger.error(f"Failed to auto-save provisional booking from dashboard: {ex}", exc_info=True)
            return {"success": True}
        else:
            return {"success": False, "error": f"Bridge returned status {resp.status_code}: {resp.text}"}
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return {"success": False, "error": str(e)}

def _is_provisional_booking(message: str, config: dict) -> bool:
    keywords = ["touch", "confirm", "provisionally", "book", "guide", "team", "person", "assistant"]
    message_lower = message.lower()
    if not any(k in message_lower for k in keywords):
        return False
        
    try:
        llm_cfg = config.get("llm", {})
        base_url = llm_cfg.get("base_url", "http://host.docker.internal:12344/v1")
        api_key = llm_cfg.get("api_key", "")
        model = llm_cfg.get("model", "local-model")
        
        openai_client = OpenAI(base_url=base_url, api_key=api_key)
        response = openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a message classifier. Respond with ONLY one word: YES or NO."},
                {
                    "role": "user",
                    "content": (
                        f"Analyze this business assistant message to see if they are handing off the customer to the human team, "
                        f"provisionally booking/scheduling a clean, or providing a guide price and closing the conversation.\n"
                        f"Assistant message: \"{message}\"\n"
                        f"Reply ONLY with 'YES' or 'NO'."
                    )
                }
            ],
            temperature=0.1,
            max_tokens=5,
        )
        res = response.choices[0].message.content.strip().upper()
        return "YES" in res
    except Exception as e:
        logger.warning(f"Provisional booking classification failed in chats: {e}")
        return False

def _extract_booking_details(chat_jid: str, config: dict) -> Optional[dict]:
    try:
        history = bridge_query("""
            SELECT content, is_from_me, sender, media_type, filename
            FROM messages
            WHERE chat_jid = ? AND ((content IS NOT NULL AND content != '') OR (media_type IS NOT NULL AND media_type != ''))
            ORDER BY timestamp DESC LIMIT 100
        """, (chat_jid,))
        history.reverse()
        
        history_lines = []
        for m in history:
            sender_label = "Business (Us)" if m["is_from_me"] else "Customer"
            msg_content = m.get("content") or ""
            if m.get("media_type"):
                media_info = f"[Sent a {m['media_type']} file: {m['filename'] or 'attachment'}]"
                msg_content = f"{media_info} {msg_content}".strip()
            history_lines.append(f"[{sender_label}]: {msg_content}")
        history_text = "\n".join(history_lines)
        
        llm_cfg = config.get("llm", {})
        base_url = llm_cfg.get("base_url", "http://host.docker.internal:12344/v1")
        api_key = llm_cfg.get("api_key", "")
        model = llm_cfg.get("model", "local-model")
        
        openai_client = OpenAI(base_url=base_url, api_key=api_key)
        response = openai_client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a structured data extractor. Extract the cleaning booking details from the conversation history.\n"
                        "Respond with a valid JSON object only, with no other text, wrapping or markdown blocks. Use double quotes for keys and strings.\n"
                        "JSON format:\n"
                        "{\n"
                        '  "customer_name": "extracted name or null",\n'
                        '  "customer_email": "extracted email or null",\n'
                        '  "address": "extracted full address with postcode or null",\n'
                        '  "clean_date": "extracted clean date and time or null",\n'
                        '  "clean_type": "extracted clean type (e.g. Deep Clean, Standard Clean) or null",\n'
                        '  "price": "extracted total price or null"\n'
                        "}"
                    )
                },
                {
                    "role": "user",
                    "content": f"Conversation history to extract from:\n{history_text}"
                }
            ],
            temperature=0.1,
            max_tokens=500,
        )
        raw_json = response.choices[0].message.content.strip()
        if raw_json.startswith("```"):
            lines = raw_json.split("\n")
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                raw_json = "\n".join(lines[1:-1]).strip()
        import json
        return json.loads(raw_json)
    except Exception as e:
        logger.error(f"Failed to extract booking details in chats: {e}")
        return None

def send_provisional_booking_admin_email(booking: dict, config: dict, is_update: bool):
    try:
        resend_api_key = config.get("resend", {}).get("api_key")
        resend_from = config.get("resend", {}).get("from_email", "onboarding@resend.dev")
        
        if not resend_api_key:
            logger.warning("Resend API Key not configured. Skipping admin provisional email.")
            return False
            
        customer_name = booking.get("customer_name") or "Customer"
        clean_date = booking.get("clean_date") or "Not specified"
        address = booking.get("address") or "Not provided"
        customer_email = booking.get("customer_email") or "Not provided"
        clean_type = booking.get("clean_type") or "Clean"
        price = booking.get("price") or "Not specified"
        
        subject = f"🔔 {'UPDATED' if is_update else 'NEW'} Provisional Booking - {customer_name} ({clean_date})"
        
        html_content = f"""
        <div style="font-family: Arial, sans-serif; color: #2d3436; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #dfe6e9; border-radius: 8px;">
            <h2 style="color: #0984e3; text-align: center; border-bottom: 2px solid #0984e3; padding-bottom: 10px;">
                🔔 {'UPDATED' if is_update else 'NEW'} PROVISIONAL BOOKING
            </h2>
            <p>The AI assistant has gathered details for a provisional booking. Please call the customer to confirm the price and details.</p>
            
            <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                <tr style="background-color: #f8f9fa;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">👤 Name</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{customer_name}</td>
                </tr>
                <tr>
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">📧 Email</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{customer_email}</td>
                </tr>
                <tr style="background-color: #f8f9fa;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🏡 Address</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{address}</td>
                </tr>
                <tr>
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">📅 Date & Time</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{clean_date}</td>
                </tr>
                <tr style="background-color: #f8f9fa;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🧹 Type of Clean</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{clean_type}</td>
                </tr>
                <tr>
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">💰 Agreed Price</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{price}</td>
                </tr>
            </table>
            
            <div style="background-color: #ffeaa7; padding: 15px; border-radius: 6px; margin: 20px 0; border: 1px solid #fdcb6e;">
                <h4 style="margin-top: 0; color: #d63031;">📞 Action Required</h4>
                <p style="margin-bottom: 0;">Please contact the customer to confirm the appointment, verify their address/postcode, and discuss final pricing.</p>
            </div>
        </div>
        """
        
        res = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": resend_from,
                "to": ["info@0161cleanerinmanchester.co.uk"],
                "subject": subject,
                "html": html_content
            },
            timeout=10
        )
        
        status = "success" if res.status_code == 200 else "failed"
        error_msg = None if res.status_code == 200 else f"HTTP {res.status_code}: {res.text}"
        
        # Log to DB
        try:
            sql_execute(f"""
                INSERT INTO {INSTANCE}_email_log (recipient, cc, subject, body, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, ("info@0161cleanerinmanchester.co.uk", None, subject, html_content, status, error_msg))
        except Exception as log_ex:
            logger.error(f"Failed to log admin provisional booking email: {log_ex}")
            
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Error sending provisional booking email: {e}")
        return False
