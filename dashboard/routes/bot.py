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

from db import sql_query, sql_execute, INSTANCE

router = APIRouter()
templates = Jinja2Templates(directory="templates")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/context.yaml")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://host.docker.internal:12344")
LM_STUDIO_API_KEY = os.environ.get("LM_STUDIO_API_KEY", "")


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
    # Fetch appointments
    filt = ""
    params = ()
    if search:
        filt = "WHERE customer_name LIKE %s OR customer_email LIKE %s OR address LIKE %s OR clean_type LIKE %s"
        search_param = f"%{search}%"
        params = (search_param, search_param, search_param, search_param)
        
    try:
        appointments = sql_query(f"""
            SELECT id, chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status, created_at
            FROM {INSTANCE}_appointments
            {filt}
            ORDER BY created_at DESC
        """, params)
    except Exception:
        appointments = []

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
):
    try:
        sql_execute(f"""
            INSERT INTO {INSTANCE}_appointments (chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status))
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


@router.post("/api/appointments/{app_id}/send-email")
async def send_confirmation_email(app_id: int):
    try:
        rows = sql_query(f"SELECT * FROM {INSTANCE}_appointments WHERE id = %s", (app_id,))
        if not rows:
            return RedirectResponse("/calendar?email_error=Appointment not found", status_code=303)
        app = rows[0]
        
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
            
        resend_api_key = config.get("resend", {}).get("api_key")
        resend_from = config.get("resend", {}).get("from_email", "onboarding@resend.dev")
        
        if not resend_api_key:
            return RedirectResponse("/calendar?email_error=Resend API Key not configured in Settings", status_code=303)
            
        # Extract postcode for payment reference
        postcode_match = re.search(r'([A-Z]{1,2}[0-9R][0-9A-Z]?\s*[0-9][A-Z]{2})', app["address"].upper())
        postcode = postcode_match.group(1) if postcode_match else app["customer_name"].split()[0].upper()
        
        # Build premium HTML email
        html_content = f"""
        <div style="font-family: Arial, sans-serif; color: #2d3436; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #dfe6e9; border-radius: 8px;">
            <h2 style="color: #0984e3; text-align: center; border-bottom: 2px solid #0984e3; padding-bottom: 10px;">📅 BOOKING CONFIRMATION</h2>
            <p>Hi <strong>{app['customer_name']}</strong>,</p>
            <p>Thank you for choosing <strong>Cleaner in Manchester (0161) Ltd</strong>. We are delighted to confirm your booking! Here are your appointment details:</p>
            
            <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                <tr style="background-color: #f8f9fa;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">👤 Name</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['customer_name']}</td>
                </tr>
                <tr>
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🏡 Address</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['address']}</td>
                </tr>
                <tr style="background-color: #f8f9fa;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">📅 Date & Time</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['clean_date']}</td>
                </tr>
                <tr>
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🧹 Type of Clean</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['clean_type']}</td>
                </tr>
                <tr style="background-color: #f8f9fa;">
                    <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">💰 Total Agreed Price</th>
                    <td style="padding: 10px; border: 1px solid #dfe6e9;">{app['price']}</td>
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
                "to": [app["customer_email"]],
                "subject": f"Booking Confirmation - {app['clean_type']} for {app['customer_name']}",
                "html": html_content
            },
            timeout=10
        )
        
        if res.status_code == 200:
            sql_execute(f"UPDATE {INSTANCE}_appointments SET status = 'confirmed' WHERE id = %s", (app_id,))
            return RedirectResponse(f"/calendar?email_sent={app['customer_name']}", status_code=303)
        else:
            return RedirectResponse(f"/calendar?email_error=Resend API Error: {res.text}", status_code=303)
            
    except Exception as e:
        logger.error(f"Resend email error: {e}")
        return RedirectResponse(f"/calendar?email_error=Server Error: {str(e)}", status_code=303)


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
