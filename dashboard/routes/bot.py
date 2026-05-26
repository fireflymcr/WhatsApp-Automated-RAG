"""
Bot management routes — activity log, cron config, calendar/appointments.
"""
import os
import json
import asyncio
import yaml
import requests
import re
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
import logging

logger = logging.getLogger(__name__)

from db import sql_query, sql_execute, bridge_query, INSTANCE

router = APIRouter()

def resolve_customer_name(chat_jid: str, db_customer_name: str, custom_name: str = None) -> str:
    name = custom_name or db_customer_name or "Customer"
    
    # Check if name is numeric or a JID
    is_numeric_or_jid = False
    if name:
        clean_name = re.sub(r'[\s\+\-\(\)@\.]', '', name)
        if clean_name.isdigit() or "@" in name:
            is_numeric_or_jid = True
            
    if (not name or is_numeric_or_jid or name == "Customer") and chat_jid:
        try:
            c_rows = bridge_query("SELECT name FROM chats WHERE jid = ?", (chat_jid,))
            if c_rows and c_rows[0].get("name"):
                potential_name = c_rows[0]["name"].strip()
                clean_pot = re.sub(r'[\s\+\-\(\)@\.]', '', potential_name)
                if potential_name and not clean_pot.isdigit() and "@" not in potential_name:
                    name = potential_name
        except Exception as e:
            logger.error(f"Failed to lookup contact name from bridge: {e}")
            
    if "@" in name:
        name = name.split("@")[0]
        
    return name

templates = Jinja2Templates(directory="templates")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/context.yaml")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://host.docker.internal:12344")
LM_STUDIO_API_KEY = os.environ.get("LM_STUDIO_API_KEY", "")

DEFAULT_EMAIL_SUBJECT = "Booking Confirmation - {clean_type} for {customer_name}"
DEFAULT_EMAIL_BODY_HTML = """<div style="font-family: Arial, sans-serif; color: #2d3436; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #dfe6e9; border-radius: 8px;">
    <h2 style="color: #0984e3; text-align: center; border-bottom: 2px solid #0984e3; padding-bottom: 10px;">📅 BOOKING CONFIRMATION</h2>
    <p>Hi <strong>{customer_name}</strong>,</p>
    <p>Thank you for choosing <strong>Cleaner in Manchester (0161) Ltd</strong>. We are delighted to confirm your booking! Here are your appointment details:</p>
    
    <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
        <tr style="background-color: #f8f9fa;">
            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">👤 Name</th>
            <td style="padding: 10px; border: 1px solid #dfe6e9;">{customer_name}</td>
        </tr>
        <tr>
            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🏡 Address</th>
            <td style="padding: 10px; border: 1px solid #dfe6e9;">{address}</td>
        </tr>
        <tr style="background-color: #f8f9fa;">
            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">📅 Date & Time</th>
            <td style="padding: 10px; border: 1px solid #dfe6e9;">{clean_date}</td>
        </tr>
        <tr>
            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🧹 Type of Clean</th>
            <td style="padding: 10px; border: 1px solid #dfe6e9;">{clean_type}</td>
        </tr>
        <tr style="background-color: #f8f9fa;">
            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">💰 Total Agreed Price</th>
            <td style="padding: 10px; border: 1px solid #dfe6e9;">{price}</td>
        </tr>
    </table>
    
    <div style="background-color: #ffeaa7; padding: 15px; border-radius: 6px; margin: 20px 0; border: 1px solid #fdcb6e;">
        <h4 style="margin-top: 0; color: #d63031;">🔒 Booking Fee Required to Secure Slot</h4>
        <p style="margin-bottom: 5px;">We require a <strong>£50 secure booking fee (deposit)</strong> to lock in your cleaning slot. This is fully deducted from your final bill.</p>
        <p style="font-size: 13px; color: #636e72;">⚠️ <em>Cancellation Policy: Cancellations made within 48 hours of your scheduled clean are strictly non-refundable.</em></p>
    </div>
    
    <h3 style="color: #2d3436; border-bottom: 1px solid #dfe6e9; padding-bottom: 5px;">🏦 Bank Transfer Details</h3>
    <ul style="list-style-type: none; padding-left: 0;">
        <li style="padding: 5px 0;"><strong>Account Name:</strong> Cleaner In Manchester 0161 Ltd</li>
        <li style="padding: 5px 0;"><strong>Sort Code:</strong> 23-11-85</li>
        <li style="padding: 5px 0;"><strong>Account Number:</strong> 93820298</li>
        <li style="padding: 5px 0;"><strong>Payment Reference:</strong> {postcode}</li>
    </ul>
    
    <p style="text-align: center; margin-top: 30px; font-size: 12px; color: #b2bec3;">
        Cleaner in Manchester (0161) Ltd | Phone: 0161 710 4789 | Website: https://0161cleanerinmanchester.co.uk/
    </p>
</div>"""

DEFAULT_REVIEW_EMAIL_SUBJECT = "How did we do? Please leave a review! ⭐"
DEFAULT_REVIEW_EMAIL_BODY_HTML = """<div style="font-family: Arial, sans-serif; color: #2d3436; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #dfe6e9; border-radius: 8px;">
    <h2 style="color: #e67e22; text-align: center; border-bottom: 2px solid #e67e22; padding-bottom: 10px;">⭐ YOUR FEEDBACK MATTERS</h2>
    <p>Hi <strong>{customer_name}</strong>,</p>
    <p>Thank you for choosing <strong>Cleaner in Manchester (0161) Ltd</strong> for your recent <strong>{clean_type}</strong> clean. We hope you are absolutely thrilled with our service!</p>
    
    <p>We are a small local business, and reviews make a huge difference to us. Could you spare 60 seconds to share your experience with others?</p>
    
    <div style="text-align: center; margin: 30px 0;">
        <a href="https://g.page/r/your-google-review-link/review" style="background-color: #e67e22; color: white; padding: 12px 24px; text-decoration: none; font-weight: bold; border-radius: 5px; display: inline-block;">Leave Us a Google Review</a>
    </div>
    
    <p>If there was anything less than perfect, please reply directly to this email or call us at <strong>0161 710 4789</strong> so we can make it right immediately!</p>
    
    <p>Thanks again for your support!</p>
    <p>Best regards,<br><strong>Cleaner in Manchester (0161) Ltd</strong></p>
</div>"""

import string

class SafeFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, "{" + key + "}")
        return string.Formatter.get_value(self, key, args, kwargs)

def safe_format(template: str, **kwargs) -> str:
    return SafeFormatter().format(template, **kwargs)


@router.get("/bot/activity", response_class=HTMLResponse)
async def activity_page(request: Request, page: int = 0, classification: str = ""):
    limit = 50
    offset = page * limit
    filt = ""
    params = (limit, offset)
    if classification:
        filt = "WHERE classification = %s"
        params = (classification, limit, offset)
    try:
        logs = sql_query(f"""
            SELECT TOP 200 id, message_id, chat_jid, sender, content,
                   classification, action_taken, reply_text, processed_at
            FROM {INSTANCE}_message_log
            {filt}
            ORDER BY processed_at DESC
        """) if not classification else sql_query(f"""
            SELECT TOP 200 id, message_id, chat_jid, sender, content,
                   classification, action_taken, reply_text, processed_at
            FROM {INSTANCE}_message_log
            WHERE classification = %s
            ORDER BY processed_at DESC
        """, (classification,))
    except Exception:
        logs = []
    return templates.TemplateResponse(request, "activity.html", {
        "page": "activity",
        "logs": logs, "classification_filter": classification,
        "instance_name": INSTANCE,
    })


@router.get("/bot/config", response_class=HTMLResponse)
async def config_page(request: Request):
    updated = request.query_params.get("updated") == "true"
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
    except Exception:
        config = {}
    
    # Pre-populate defaults in config if missing so they display in editor
    if "resend" not in config:
        config["resend"] = {}
    if "email_subject" not in config["resend"]:
        config["resend"]["email_subject"] = DEFAULT_EMAIL_SUBJECT
    if "email_body_html" not in config["resend"]:
        config["resend"]["email_body_html"] = DEFAULT_EMAIL_BODY_HTML
    if "review_email_subject" not in config["resend"]:
        config["resend"]["review_email_subject"] = DEFAULT_REVIEW_EMAIL_SUBJECT
    if "review_email_body_html" not in config["resend"]:
        config["resend"]["review_email_body_html"] = DEFAULT_REVIEW_EMAIL_BODY_HTML
    
    # Fetch available models from LM Studio for drop-down selection
    loaded_models = []
    headers = {}
    if LM_STUDIO_API_KEY:
        headers["Authorization"] = f"Bearer {LM_STUDIO_API_KEY}"
    try:
        resp = requests.get(f"{LM_STUDIO_URL}/v1/models", headers=headers, timeout=3)
        if resp.status_code == 200:
            loaded_models = [m["id"] for m in resp.json().get("data", [])]
    except Exception:
        pass

    return templates.TemplateResponse(request, "config.html", {
        "page": "config",
        "config": config,
        "loaded_models": loaded_models,
        "instance_name": INSTANCE,
        "updated": updated,
    })


@router.post("/bot/config")
async def update_config(
    request: Request,
    check_interval_minutes: int = Form(3),
    cooldown_minutes: int = Form(10),
    max_replies_per_chat_per_day: int = Form(10),
    reply_to_groups: bool = Form(False),
    system_prompt: str = Form(""),
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
    llm_model: str = Form(""),
    llm_temperature: float = Form(0.7),
    llm_max_tokens: int = Form(300),
    resend_api_key: str = Form(""),
    resend_from_email: str = Form("onboarding@resend.dev"),
    resend_email_subject: str = Form(""),
    resend_email_body_html: str = Form(""),
    resend_review_email_subject: str = Form(""),
    resend_review_email_body_html: str = Form(""),
):
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
        config["check_interval_minutes"] = check_interval_minutes
        config["cooldown_minutes"] = cooldown_minutes
        config["max_replies_per_chat_per_day"] = max_replies_per_chat_per_day
        config["reply_to_groups"] = reply_to_groups
        config["system_prompt"] = system_prompt
        
        # Update LLM settings
        if "llm" not in config:
            config["llm"] = {}
        config["llm"]["base_url"] = llm_base_url
        config["llm"]["api_key"] = llm_api_key
        config["llm"]["model"] = llm_model
        config["llm"]["temperature"] = llm_temperature
        config["llm"]["max_tokens"] = llm_max_tokens

        # Update Resend settings
        if "resend" not in config:
            config["resend"] = {}
        config["resend"]["api_key"] = resend_api_key
        config["resend"]["from_email"] = resend_from_email
        config["resend"]["email_subject"] = resend_email_subject
        config["resend"]["email_body_html"] = resend_email_body_html
        config["resend"]["review_email_subject"] = resend_review_email_subject
        config["resend"]["review_email_body_html"] = resend_review_email_body_html
        
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as e:
        logger.error(f"Failed to update config: {e}", exc_info=True)
    return RedirectResponse("/bot/config?updated=true", status_code=303)


@router.get("/api/sse/activity")
async def sse_activity(request: Request):
    async def event_gen():
        last_id = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                rows = sql_query(f"""
                    SELECT TOP 10 id, message_id, chat_jid, sender, content,
                           classification, action_taken, reply_text, processed_at
                    FROM {INSTANCE}_message_log
                    WHERE id > %s ORDER BY id DESC
                """, (last_id,))
                if rows:
                    last_id = max(r["id"] for r in rows)
                    yield {"event": "activity_update", "data": json.dumps(rows, default=str)}
            except Exception:
                pass
            await asyncio.sleep(5)
    return EventSourceResponse(event_gen())


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, search: str = "", email_sent: str = "", email_error: str = ""):
    # Ensure clean_status and notes columns exist in appointments table
    try:
        sql_execute(f"""
            IF NOT EXISTS (SELECT * FROM syscolumns WHERE id=object_id('{INSTANCE}_appointments') AND name='clean_status')
            BEGIN
                ALTER TABLE {INSTANCE}_appointments ADD clean_status NVARCHAR(50) DEFAULT 'pending'
            END
        """)
        sql_execute(f"""
            IF NOT EXISTS (SELECT * FROM syscolumns WHERE id=object_id('{INSTANCE}_appointments') AND name='notes')
            BEGIN
                ALTER TABLE {INSTANCE}_appointments ADD notes NVARCHAR(MAX) NULL
            END
        """)
    except Exception as e:
        logger.error(f"Failed to ensure columns on appointments table: {e}")

    # Fetch appointments
    filt = ""
    params = ()
    if search:
        filt = "WHERE a.customer_name LIKE %s OR a.customer_email LIKE %s OR a.address LIKE %s OR a.clean_type LIKE %s OR s.custom_name LIKE %s"
        search_param = f"%{search}%"
        params = (search_param, search_param, search_param, search_param, search_param)
        
    try:
        appointments = sql_query(f"""
            SELECT a.id, a.chat_jid, a.customer_name, a.customer_email, a.address, a.clean_date, a.clean_type, a.price, a.status, a.clean_status, a.notes, a.created_at,
                   s.custom_name
            FROM {INSTANCE}_appointments a
            LEFT JOIN {INSTANCE}_chat_status s ON a.chat_jid = s.jid
            {filt}
            ORDER BY a.created_at DESC
        """, params)
    except Exception as e:
        logger.error(f"Failed to fetch appointments: {e}")
        appointments = []

    # Fetch contact name map from SQLite bridge
    contact_names = {}
    try:
        rows = bridge_query("SELECT jid, name FROM chats")
        for r in rows:
            if r.get("jid") and r.get("name"):
                contact_names[r["jid"]] = r["name"].strip()
    except Exception as e:
        logger.error(f"Failed to fetch contact names from bridge: {e}")

    # Map display names
    for app in appointments:
        cust_jid = app.get("chat_jid") or ""
        cust_name = app.get("customer_name") or ""
        custom_name = app.get("custom_name")
        
        name = custom_name or cust_name or "Customer"
        is_numeric_or_jid = False
        if name:
            clean_name = re.sub(r'[\s\+\-\(\)@\.]', '', name)
            if clean_name.isdigit() or "@" in name:
                is_numeric_or_jid = True
                
        if (not name or is_numeric_or_jid or name == "Customer") and cust_jid in contact_names:
            potential_name = contact_names[cust_jid]
            clean_pot = re.sub(r'[\s\+\-\(\)@\.]', '', potential_name)
            if potential_name and not clean_pot.isdigit() and "@" not in potential_name:
                name = potential_name
                
        if "@" in name:
            name = name.split("@")[0]
            
        app["display_name"] = name

    # Get Resend status from context.yaml
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
        resend_configured = bool(config.get("resend", {}).get("api_key"))
    except Exception:
        resend_configured = False

    return templates.TemplateResponse(request, "calendar.html", {
        "page": "calendar",
        "appointments": appointments,
        "search": search,
        "instance_name": INSTANCE,
        "resend_configured": resend_configured,
        "email_sent": email_sent,
        "email_error": email_error,
    })


@router.post("/api/appointments")
async def add_appointment(
    customer_name: str = Form(...),
    customer_email: str = Form(...),
    address: str = Form(...),
    clean_date: str = Form(...),
    clean_type: str = Form(...),
    price: str = Form(...),
    status: str = Form("pending"),
    chat_jid: str = Form(""),
    notes: str = Form(None),
):
    try:
        # Ensure clean_status and notes columns exist
        try:
            sql_execute(f"""
                IF NOT EXISTS (SELECT * FROM syscolumns WHERE id=object_id('{INSTANCE}_appointments') AND name='clean_status')
                BEGIN
                    ALTER TABLE {INSTANCE}_appointments ADD clean_status NVARCHAR(50) DEFAULT 'pending'
                END
            """)
            sql_execute(f"""
                IF NOT EXISTS (SELECT * FROM syscolumns WHERE id=object_id('{INSTANCE}_appointments') AND name='notes')
                BEGIN
                    ALTER TABLE {INSTANCE}_appointments ADD notes NVARCHAR(MAX) NULL
                END
            """)
        except Exception:
            pass
        sql_execute(f"""
            INSERT INTO {INSTANCE}_appointments (chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status, clean_status, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
        """, (chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status, notes))
    except Exception as e:
        logger.error(f"Failed to add appointment: {e}")
    return RedirectResponse("/calendar", status_code=303)


@router.post("/api/appointments/{app_id}/delete")
async def delete_appointment(app_id: int):
    try:
        sql_execute(f"DELETE FROM {INSTANCE}_appointments WHERE id = %s", (app_id,))
    except Exception as e:
        logger.error(f"Failed to delete appointment: {e}")
    return RedirectResponse("/calendar", status_code=303)


@router.post("/api/appointments/{app_id}/toggle-status")
async def toggle_appointment_status(app_id: int):
    try:
        rows = sql_query(f"SELECT status FROM {INSTANCE}_appointments WHERE id = %s", (app_id,))
        if rows:
            current_status = rows[0]["status"]
            new_status = "confirmed" if current_status == "pending" else "pending"
            sql_execute(f"UPDATE {INSTANCE}_appointments SET status = %s WHERE id = %s", (new_status, app_id))
            logger.info(f"Toggled appointment {app_id} status from {current_status} to {new_status}")
    except Exception as e:
        logger.error(f"Failed to toggle appointment status: {e}")
    return RedirectResponse("/calendar", status_code=303)


@router.post("/api/appointments/{app_id}/toggle-clean-status")
async def toggle_appointment_clean_status(app_id: int):
    try:
        # Ensure clean_status column exists
        try:
            sql_execute(f"""
                IF NOT EXISTS (SELECT * FROM syscolumns WHERE id=object_id('{INSTANCE}_appointments') AND name='clean_status')
                BEGIN
                    ALTER TABLE {INSTANCE}_appointments ADD clean_status NVARCHAR(50) DEFAULT 'pending'
                END
            """)
        except Exception:
            pass
        rows = sql_query(f"SELECT clean_status FROM {INSTANCE}_appointments WHERE id = %s", (app_id,))
        if rows:
            current_status = rows[0].get("clean_status") or "pending"
            new_status = "completed" if current_status == "pending" else "pending"
            sql_execute(f"UPDATE {INSTANCE}_appointments SET clean_status = %s WHERE id = %s", (new_status, app_id))
            logger.info(f"Toggled appointment {app_id} clean_status from {current_status} to {new_status}")
    except Exception as e:
        logger.error(f"Failed to toggle appointment clean status: {e}")
    return RedirectResponse("/calendar", status_code=303)


@router.post("/api/appointments/{app_id}/send-email")
async def send_confirmation_email(app_id: int):
    try:
        rows = sql_query(f"""
            SELECT a.*, s.custom_name
            FROM {INSTANCE}_appointments a
            LEFT JOIN {INSTANCE}_chat_status s ON a.chat_jid = s.jid
            WHERE a.id = %s
        """, (app_id,))
        if not rows:
            return RedirectResponse("/calendar?email_error=Appointment not found", status_code=303)
        app = rows[0]
        
        customer_name = resolve_customer_name(app.get("chat_jid"), app.get("customer_name"), app.get("custom_name"))
        
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
            
        resend_api_key = config.get("resend", {}).get("api_key")
        resend_from = config.get("resend", {}).get("from_email", "onboarding@resend.dev")
        
        if not resend_api_key:
            return RedirectResponse("/calendar?email_error=Resend API Key not configured in Settings", status_code=303)
            
        # Extract postcode for payment reference
        postcode_match = re.search(r'([A-Z]{1,2}[0-9R][0-9A-Z]?\s*[0-9][A-Z]{2})', app["address"].upper())
        postcode = postcode_match.group(1) if postcode_match else customer_name.split()[0].upper()
        
        # Get custom template from config or use defaults
        resend_subject = config.get("resend", {}).get("email_subject", DEFAULT_EMAIL_SUBJECT)
        resend_body = config.get("resend", {}).get("email_body_html", DEFAULT_EMAIL_BODY_HTML)
        
        # Format variables safely
        formatted_subject = safe_format(
            resend_subject,
            customer_name=customer_name,
            address=app["address"],
            clean_date=app["clean_date"],
            clean_type=app["clean_type"] or 'Clean',
            price=app["price"],
            postcode=postcode
        )
        formatted_body = safe_format(
            resend_body,
            customer_name=customer_name,
            address=app["address"],
            clean_date=app["clean_date"],
            clean_type=app["clean_type"] or 'Clean',
            price=app["price"],
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
                "to": [app["customer_email"]],
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
            """, (app["customer_email"], "info@0161cleanerinmanchester.co.uk", formatted_subject, formatted_body, status, error_msg))
        except Exception as log_ex:
            logger.error(f"Failed to log sent email: {log_ex}")
            
        if res.status_code == 200:
            sql_execute(f"UPDATE {INSTANCE}_appointments SET status = 'confirmed' WHERE id = %s", (app_id,))
            return RedirectResponse(f"/calendar?email_sent={customer_name}", status_code=303)
        else:
            return RedirectResponse(f"/calendar?email_error=Resend API Error: {res.text}", status_code=303)
            
    except Exception as e:
        logger.error(f"Resend email error: {e}")
        return RedirectResponse(f"/calendar?email_error=Server Error: {str(e)}", status_code=303)


@router.post("/api/appointments/{app_id}/send-invoice")
async def send_invoice(app_id: int, amount: float = Form(...)):
    try:
        rows = sql_query(f"""
            SELECT a.*, s.custom_name
            FROM {INSTANCE}_appointments a
            LEFT JOIN {INSTANCE}_chat_status s ON a.chat_jid = s.jid
            WHERE a.id = %s
        """, (app_id,))
        if not rows:
            return RedirectResponse("/calendar?email_error=Appointment not found", status_code=303)
        app = rows[0]
        
        customer_name = resolve_customer_name(app.get("chat_jid"), app.get("customer_name"), app.get("custom_name"))
        
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
            
        resend_api_key = config.get("resend", {}).get("api_key")
        resend_from = config.get("resend", {}).get("from_email", "onboarding@resend.dev")
        
        if not resend_api_key:
            return RedirectResponse("/calendar?email_error=Resend API Key not configured in Settings", status_code=303)
            
        # Extract postcode for payment reference
        postcode_match = re.search(r'([A-Z]{1,2}[0-9R][0-9A-Z]?\s*[0-9][A-Z]{2})', app["address"].upper())
        postcode = postcode_match.group(1) if postcode_match else customer_name.split()[0].upper()
        
        # Build premium HTML email content for the invoice
        formatted_subject = f"🧾 Invoice for your clean - {customer_name}"
        formatted_body = f"""
        <div style="font-family: Arial, sans-serif; color: #2d3436; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #dfe6e9; border-radius: 8px;">
            <h2 style="color: #0984e3; text-align: center; border-bottom: 2px solid #0984e3; padding-bottom: 10px;">🧾 INVOICE - Cleaner in Manchester (0161) Ltd</h2>
            <p>Hi <strong>{customer_name}</strong>,</p>
            <p>Please find below the invoice details for your recent clean:</p>
            
            <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                <tr style="background-color: #f8f9fa;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🏡 Clean Address</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['address']}</td>
                </tr>
                <tr>
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">📅 Date & Time</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['clean_date']}</td>
                </tr>
                <tr style="background-color: #f8f9fa;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🧹 Type of Clean</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['clean_type'] or 'Clean'}</td>
                </tr>
                <tr>
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">💰 Agreed Price</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['price']}</td>
                </tr>
                <tr style="background-color: #dfe6e9; font-weight: bold;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #b2bec3;">💵 Total Due</th>
                    <td style="padding: 10px; border: 1px solid #b2bec3; color: #2d3436; font-size: 1.1rem;">£{amount:.2f}</td>
                </tr>
            </table>
            
            <h3 style="color: #2d3436; border-bottom: 1px solid #dfe6e9; padding-bottom: 5px;">🏦 Bank Transfer Payment Details</h3>
            <ul style="list-style-type: none; padding-left: 0;">
                <li style="padding: 5px 0;"><strong>Account Name:</strong> Cleaner In Manchester 0161 Ltd</li>
                <li style="padding: 5px 0;"><strong>Sort Code:</strong> 23-11-85</li>
                <li style="padding: 5px 0;"><strong>Account Number:</strong> 93820298</li>
                <li style="padding: 5px 0;"><strong>Payment Reference:</strong> {postcode}</li>
            </ul>
            
            <p>Please make payment to the above account. Thank you for your business!</p>
            
            <p style="text-align: center; margin-top: 30px; font-size: 12px; color: #b2bec3;">
                Cleaner in Manchester (0161) Ltd | Phone: 0161 710 4789 | Website: https://0161cleanerinmanchester.co.uk/
            </p>
        </div>
        """
        
        # Send Email via Resend
        res = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": resend_from,
                "to": [app["customer_email"]],
                "cc": ["info@0161cleanerinmanchester.co.uk"],
                "subject": formatted_subject,
                "html": formatted_body
            },
            timeout=10
        )
        
        email_status = "success" if res.status_code == 200 else "failed"
        email_error_msg = None if res.status_code == 200 else f"HTTP {res.status_code}: {res.text}"
        
        # Log to email log
        try:
            sql_execute(f"""
                INSERT INTO {INSTANCE}_email_log (recipient, cc, subject, body, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (app["customer_email"], "info@0161cleanerinmanchester.co.uk", formatted_subject, formatted_body, email_status, email_error_msg))
        except Exception as log_ex:
            logger.error(f"Failed to log sent invoice email: {log_ex}")
            
        # Send WhatsApp via Bridge API if chat_jid is present
        whatsapp_status = "skipped"
        whatsapp_error_msg = None
        if app.get("chat_jid"):
            try:
                wa_message = (
                    f"Hi {customer_name}! 😊 Please find the invoice for your recent clean below:\n\n"
                    f"🏡 *Clean Address*: {app['address']}\n"
                    f"📅 *Date & Time*: {app['clean_date']}\n"
                    f"🧹 *Clean Type*: {app['clean_type'] or 'Clean'}\n"
                    f"💰 *Agreed Price*: {app['price']}\n"
                    f"💵 *Amount Due*: £{amount:.2f}\n\n"
                    f"🏦 *Bank Transfer Details*:\n"
                    f"Account Name: Cleaner In Manchester 0161 Ltd\n"
                    f"Sort Code: 23-11-85\n"
                    f"Account Number: 93820298\n"
                    f"Payment Reference: {postcode}\n\n"
                    f"📧 *Note*: We have also sent the PDF invoice via email to {app['customer_email']}.\n\n"
                    f"Thank you for choosing Cleaner in Manchester! 🧹"
                )
                wa_res = requests.post(
                    "http://bridge-cleaner:8080/api/send",
                    json={"recipient": app["chat_jid"], "message": wa_message},
                    timeout=15
                )
                if wa_res.status_code == 200 and wa_res.json().get("success"):
                    whatsapp_status = "success"
                else:
                    whatsapp_status = "failed"
                    whatsapp_error_msg = f"HTTP {wa_res.status_code}: {wa_res.text}"
            except Exception as wa_ex:
                whatsapp_status = "failed"
                whatsapp_error_msg = str(wa_ex)
                logger.error(f"Failed to send invoice WhatsApp: {wa_ex}")
                
        # Append note to appointment
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        invoice_note = f"\n[Invoice of £{amount:.2f} sent on {timestamp_str} (Email: {email_status}, WA: {whatsapp_status})]"
        existing_notes = app.get("notes") or ""
        new_notes = existing_notes + invoice_note
        sql_execute(f"UPDATE {INSTANCE}_appointments SET notes = %s WHERE id = %s", (new_notes, app_id))
        
        if res.status_code == 200:
            return RedirectResponse(f"/calendar?email_sent={customer_name} (Invoice)", status_code=303)
        else:
            return RedirectResponse(f"/calendar?email_error=Resend API Error: {res.text}", status_code=303)
            
    except Exception as e:
        logger.error(f"Send invoice error: {e}")
        return RedirectResponse(f"/calendar?email_error=Server Error: {str(e)}", status_code=303)


@router.post("/api/appointments/{app_id}/send-review-email")
async def send_review_email(app_id: int):
    try:
        # Ensure clean_status column exists
        try:
            sql_execute(f"""
                IF NOT EXISTS (SELECT * FROM syscolumns WHERE id=object_id('{INSTANCE}_appointments') AND name='clean_status')
                BEGIN
                    ALTER TABLE {INSTANCE}_appointments ADD clean_status NVARCHAR(50) DEFAULT 'pending'
                END
            """)
        except Exception:
            pass
        rows = sql_query(f"""
            SELECT a.*, s.custom_name
            FROM {INSTANCE}_appointments a
            LEFT JOIN {INSTANCE}_chat_status s ON a.chat_jid = s.jid
            WHERE a.id = %s
        """, (app_id,))
        if not rows:
            return RedirectResponse("/calendar?email_error=Appointment not found", status_code=303)
        app = rows[0]
        
        customer_name = resolve_customer_name(app.get("chat_jid"), app.get("customer_name"), app.get("custom_name"))
        
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
            
        resend_api_key = config.get("resend", {}).get("api_key")
        resend_from = config.get("resend", {}).get("from_email", "onboarding@resend.dev")
        
        if not resend_api_key:
            return RedirectResponse("/calendar?email_error=Resend API Key not configured in Settings", status_code=303)
            
        # Extract postcode for payment reference
        postcode_match = re.search(r'([A-Z]{1,2}[0-9R][0-9A-Z]?\s*[0-9][A-Z]{2})', app["address"].upper())
        postcode = postcode_match.group(1) if postcode_match else customer_name.split()[0].upper()
        
        # Get custom template from config or use defaults
        resend_subject = config.get("resend", {}).get("review_email_subject", DEFAULT_REVIEW_EMAIL_SUBJECT)
        resend_body = config.get("resend", {}).get("review_email_body_html", DEFAULT_REVIEW_EMAIL_BODY_HTML)
        
        # Format variables safely
        formatted_subject = safe_format(
            resend_subject,
            customer_name=customer_name,
            address=app["address"],
            clean_date=app["clean_date"],
            clean_type=app["clean_type"] or 'Clean',
            price=app["price"],
            postcode=postcode
        )
        formatted_body = safe_format(
            resend_body,
            customer_name=customer_name,
            address=app["address"],
            clean_date=app["clean_date"],
            clean_type=app["clean_type"] or 'Clean',
            price=app["price"],
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
                "to": [app["customer_email"]],
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
            """, (app["customer_email"], "info@0161cleanerinmanchester.co.uk", formatted_subject, formatted_body, status, error_msg))
        except Exception as log_ex:
            logger.error(f"Failed to log sent review email: {log_ex}")
            
        if res.status_code == 200:
            return RedirectResponse(f"/calendar?email_sent={customer_name} (Review Request)", status_code=303)
        else:
            return RedirectResponse(f"/calendar?email_error=Resend API Error: {res.text}", status_code=303)
            
    except Exception as e:
        logger.error(f"Resend review email error: {e}")
        return RedirectResponse(f"/calendar?email_error=Server Error: {str(e)}", status_code=303)


@router.post("/api/appointments/{app_id}/update")
async def update_appointment(
    app_id: int,
    customer_name: str = Form(...),
    customer_email: str = Form(...),
    address: str = Form(...),
    clean_date: str = Form(...),
    clean_type: str = Form(...),
    price: str = Form(...),
):
    try:
        sql_execute(f"""
            UPDATE {INSTANCE}_appointments
            SET customer_name = %s, customer_email = %s, address = %s, clean_date = %s, clean_type = %s, price = %s
            WHERE id = %s
        """, (customer_name, customer_email, address, clean_date, clean_type, price, app_id))
        logger.info(f"Updated appointment {app_id}")
    except Exception as e:
        logger.error(f"Failed to update appointment {app_id}: {e}")
    return RedirectResponse("/calendar", status_code=303)


@router.post("/api/appointments/{app_id}/update-notes")
async def update_appointment_notes(
    app_id: int,
    request: Request
):
    try:
        # Check body format
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
            notes = data.get("notes", "")
        else:
            form_data = await request.form()
            notes = form_data.get("notes", "")
            
        sql_execute(f"""
            UPDATE {INSTANCE}_appointments
            SET notes = %s
            WHERE id = %s
        """, (notes, app_id))
        logger.info(f"Updated notes for appointment {app_id}")
        return {"success": True, "notes": notes}
    except Exception as e:
        logger.error(f"Failed to update notes for appointment {app_id}: {e}")
        return {"success": False, "message": str(e)}


@router.get("/api/appointments/export/ics")
async def export_ics():
    try:
        appointments = sql_query(f"SELECT * FROM {INSTANCE}_appointments ORDER BY created_at DESC")
    except Exception:
        appointments = []
        
    ics_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Cleaner in Manchester//Appointments//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH"
    ]
    
    for app in appointments:
        dt_stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
        clean_date_raw = app["clean_date"]
        clean_dt = None
        try:
            clean_date_raw_clean = clean_date_raw.replace("T", " ")
            clean_dt = datetime.strptime(clean_date_raw_clean[:16], "%Y-%m-%d %H:%M")
        except Exception:
            pass
            
        if clean_dt:
            dt_start = clean_dt.strftime("%Y%m%dT%H%M%S")
            dt_end = (clean_dt + timedelta(hours=3)).strftime("%Y%m%dT%H%M%S")
        else:
            dt_start = datetime.now().strftime("%Y%m%dT%H%M%S")
            dt_end = datetime.now().strftime("%Y%m%dT%H%M%S")
            
        uid = f"app_{app['id']}@0161cleanerinmanchester.co.uk"
        summary = f"{app['clean_type']} - {app['customer_name']}"
        desc = (
            f"Customer: {app['customer_name']}\\n"
            f"Email: {app['customer_email']}\\n"
            f"Address: {app['address']}\\n"
            f"Price: {app['price']}\\n"
            f"Status: {app['status']}\\n"
            f"Clean Date (Original): {app['clean_date']}"
        ).replace("\n", "\\n").replace("\r", "")
        
        ics_lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dt_stamp}",
            f"DTSTART:{dt_start}",
            f"DTEND:{dt_end}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            f"LOCATION:{app['address'].replace(',', '\\,')}",
            "END:VEVENT"
        ])
        
    ics_lines.append("END:VCALENDAR")
    ics_content = "\r\n".join(ics_lines)
    
    return Response(
        content=ics_content,
        media_type="text/calendar",
        headers={"Content-Disposition": "attachment; filename=appointments.ics"}
    )


@router.get("/api/appointments/export/csv")
async def export_csv():
    try:
        appointments = sql_query(f"SELECT * FROM {INSTANCE}_appointments ORDER BY created_at DESC")
    except Exception:
        appointments = []
        
    csv_lines = ["ID,Customer Name,Customer Email,Address,Clean Date,Clean Type,Price,Status,Created At"]
    for app in appointments:
        row = [
            str(app["id"]),
            f'"{app["customer_name"]}"',
            f'"{app["customer_email"]}"',
            f'"{app["address"].replace(chr(34), chr(39))}"',
            f'"{app["clean_date"]}"',
            f'"{app["clean_type"]}"',
            f'"{app["price"]}"',
            f'"{app["status"]}"',
            str(app["created_at"])
        ]
        csv_lines.append(",".join(row))
        
    csv_content = "\n".join(csv_lines)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=appointments.csv"}
    )


@router.get("/emails", response_class=HTMLResponse)
async def emails_page(request: Request, status: str = None, q: str = None):
    query_filter = ""
    params = []
    
    conditions = []
    if status:
        conditions.append("status = %s")
        params.append(status)
    if q:
        conditions.append("(recipient LIKE %s OR cc LIKE %s OR subject LIKE %s OR body LIKE %s OR error_message LIKE %s)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])
        
    if conditions:
        query_filter = "WHERE " + " AND ".join(conditions)
        
    try:
        emails = sql_query(f"""
            SELECT id, recipient, cc, subject, body, status, error_message, sent_at
            FROM {INSTANCE}_email_log
            {query_filter}
            ORDER BY sent_at DESC
        """, tuple(params) if params else None)
    except Exception as e:
        logger.error(f"Failed to fetch emails: {e}")
        emails = []
        
    return templates.TemplateResponse(request, "emails.html", {
        "page": "emails",
        "emails": emails,
        "status_filter": status,
        "search": q,
        "instance_name": INSTANCE,
    })
