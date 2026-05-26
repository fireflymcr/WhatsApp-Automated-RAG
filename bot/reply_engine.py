"""
Auto-reply engine for WhatsApp Bot.
Checks for new messages, classifies them via AI, and replies to customers.
"""

import logging
import requests
import math
import struct
from typing import Optional

from openai import OpenAI

from config import BotConfig, load_config
from db import BridgeDB, TrackingDB

logger = logging.getLogger("whatsapp-bot.reply")

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

import string

class SafeFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, "{" + key + "}")
        return string.Formatter.get_value(self, key, args, kwargs)

def safe_format(template: str, **kwargs) -> str:
    return SafeFormatter().format(template, **kwargs)


def parse_booking_confirmation(message: str) -> Optional[dict]:
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


class ReplyEngine:
    """Core auto-reply logic: classify, generate, send."""

    def __init__(self, config: BotConfig, bridge_db: BridgeDB, tracking_db: TrackingDB):
        self.config = config
        self.bridge_db = bridge_db
        self.tracking_db = tracking_db
        self.llm = OpenAI(base_url=config.llm.base_url, api_key=config.llm.api_key)

    def run_check(self):
        """Main loop: called every N minutes by the scheduler."""
        logger.info("=== Reply check started ===")

        # Reload configuration dynamically from disk to apply dashboard changes instantly
        try:
            self.config = load_config()
            # Update OpenAI client parameters if they changed
            self.llm.base_url = self.config.llm.base_url
            self.llm.api_key = self.config.llm.api_key
            logger.info(f"Configuration successfully hot-reloaded. Model: {self.config.llm.model}")
        except Exception as e:
            logger.warning(f"Failed to hot-reload configuration: {e}")

        messages = self.bridge_db.get_recent_messages(self.config.lookback_minutes)
        logger.info(f"Found {len(messages)} incoming messages in lookback window")

        # First, filter out any incoming messages that have already been replied to or skipped
        unprocessed_messages = []
        for msg in messages:
            mid = msg["message_id"]
            jid = msg["chat_jid"]
            if not self.tracking_db.is_already_replied(mid, jid):
                unprocessed_messages.append(msg)

        logger.info(f"Found {len(unprocessed_messages)} unprocessed messages to handle")

        # Group unprocessed messages by chat_jid and select only the latest one to prevent duplicate processing
        latest_msgs = {}
        for msg in unprocessed_messages:
            jid = msg["chat_jid"]
            if jid not in latest_msgs:
                latest_msgs[jid] = msg
            else:
                if msg["timestamp"] > latest_msgs[jid]["timestamp"]:
                    old_msg = latest_msgs[jid]
                    self.tracking_db.record_skip(
                        old_msg["message_id"], jid, old_msg["sender"], 
                        old_msg["content"], "superseded", "skipped_superseded"
                    )
                    latest_msgs[jid] = msg
                else:
                    self.tracking_db.record_skip(
                        msg["message_id"], jid, msg["sender"], 
                        msg["content"], "superseded", "skipped_superseded"
                    )

        active_messages = list(latest_msgs.values())
        logger.info(f"Deduplicated to {len(active_messages)} unique active chats to process")

        replied_count = 0
        skipped_count = 0

        for msg in active_messages:
            mid = msg["message_id"]
            jid = msg["chat_jid"]
            sender = msg["sender"]
            content = msg["content"]
            chat_name = msg.get("chat_name", "Unknown")

            # ── Skip group chats if disabled ──
            if not self.config.reply_to_groups and jid.endswith("@g.us"):
                continue

            # ── DEDUP: Never reply to the same message twice ──
            if self.tracking_db.is_already_replied(mid, jid):
                continue

            # ── Cooldown check ──
            # Use a short 15-second safety cooldown (0.25 min) to prevent duplicate API triggers,
            # fully allowing active ongoing back-and-forth conversation.
            if self.tracking_db.check_cooldown(jid, 0.25):
                self.tracking_db.record_skip(mid, jid, sender, content, "unknown", "skipped_cooldown")
                skipped_count += 1
                logger.debug(f"Safety cooldown active for {chat_name}, skipping")
                continue

            # ── Daily limit check ──
            if self.tracking_db.check_daily_limit(jid, self.config.max_replies_per_chat_per_day):
                self.tracking_db.record_skip(mid, jid, sender, content, "unknown", "skipped_daily_limit")
                skipped_count += 1
                logger.debug(f"Daily limit reached for {chat_name}, skipping")
                continue

            # ── AI Classification ──
            classification = self._classify_message(sender, chat_name, content)
            logger.info(f"[{chat_name}] '{content[:80]}...' → {classification}")

            if classification in ("SPAM", "IRRELEVANT"):
                self.tracking_db.record_skip(mid, jid, sender, content, classification, f"skipped_{classification.lower()}")
                skipped_count += 1
                continue

            # ── Build conversation context ──
            # Look through the whole chat history (limit=None) to be context-aware of everything said.
            context = self.bridge_db.get_conversation_context(jid, limit=None)

            # ── Check if already answered by us ──
            if context and context[-1].get("is_from_me"):
                logger.info(f"[{chat_name}] Latest message is from us. Skipping.")
                self.tracking_db.record_skip(mid, jid, sender, content, classification, "skipped_already_answered")
                skipped_count += 1
                continue

            # ── Check if they say they sent a payment ──
            if self._is_payment_assertion(content):
                logger.info(f"[{chat_name}] Payment assertion detected! Attempting to auto-confirm booking...")
                confirmed_ok, pay_reply = self._handle_booking_payment(jid, sender, chat_name, content, context)
                if confirmed_ok and pay_reply:
                    send_ok = self._send_message(jid, pay_reply)
                    if send_ok:
                        self.tracking_db.record_reply(mid, jid, pay_reply, classification)
                        self.tracking_db.log_reply(mid, jid, sender, content, classification, pay_reply)
                        self.tracking_db.update_cooldown(jid)
                        replied_count += 1
                        logger.info(f"✓ Payment Replied to {chat_name}: {pay_reply[:80]}...")
                    else:
                        self.tracking_db.record_skip(mid, jid, sender, content, classification, "skipped_send_failed")
                        skipped_count += 1
                    continue

            # ── Generate AI reply ──
            reply_text = self._generate_reply(context, content, chat_name, sender, jid)
            if not reply_text:
                self.tracking_db.record_skip(mid, jid, sender, content, classification, "skipped_generation_failed")
                skipped_count += 1
                continue

            # ── Send via bridge API ──
            success = self._send_message(jid, reply_text)
            if success:
                self.tracking_db.record_reply(mid, jid, reply_text, classification)
                self.tracking_db.log_reply(mid, jid, sender, content, classification, reply_text)
                self.tracking_db.update_cooldown(jid)
                replied_count += 1
                logger.info(f"✓ Replied to {chat_name}: {reply_text[:80]}...")
            else:
                self.tracking_db.record_skip(mid, jid, sender, content, classification, "skipped_send_failed")
                skipped_count += 1
                logger.error(f"✗ Failed to send reply to {chat_name}")

        logger.info(f"=== Reply check complete: {replied_count} replied, {skipped_count} skipped ===")

    def _classify_message(self, sender: str, chat_name: str, content: str) -> str:
        """Use AI to classify a message as CUSTOMER, SPAM, or IRRELEVANT."""
        prompt = self.config.classification_prompt.format(
            sender_name=sender,
            chat_name=chat_name,
            message_content=content,
        )

        try:
            response = self.llm.chat.completions.create(
                model=self.config.llm.model,
                messages=[
                    {"role": "system", "content": "You are a message classifier. Reply with ONLY one word: CUSTOMER, SPAM, or IRRELEVANT."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=10,
            )
            result = response.choices[0].message.content.strip().upper()
            # Validate result
            if result in ("CUSTOMER", "SPAM", "IRRELEVANT"):
                return result
            # Fuzzy match
            if "SPAM" in result:
                return "SPAM"
            if "IRRELEVANT" in result:
                return "IRRELEVANT"
            return "CUSTOMER"  # Default to customer if unsure
        except Exception as e:
            logger.error(f"Classification error: {e}")
            return "CUSTOMER"  # Safe default: treat as customer

    def _generate_reply(self, context: list, new_message: str, chat_name: str, sender: str, chat_jid: str) -> Optional[str]:
        """Generate a reply using the system prompt and conversation context."""
        system_prompt = self.config.system_prompt
        
        # ── Retrieve Existing Bookings & DPA rules ──
        dpa_verification_prompt = ""
        try:
            bookings = self.tracking_db.get_appointments_by_jid(chat_jid)
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
            logger.error(f"Error fetching existing bookings for DPA context: {e}")

        # ── RAG Retrieval ──
        rag_context = self._retrieve_rag_context(new_message)
        if rag_context:
            system_prompt += f"\n\n{rag_context}"
            logger.info("RAG context successfully injected into system prompt")
            
        if dpa_verification_prompt:
            system_prompt += dpa_verification_prompt
            logger.info("DPA verification rules successfully injected into system prompt")

        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history
        history_lines = []
        if context:
            for m in context:
                sender_label = "Business (Us)" if m.get("is_from_me") else "Customer"
                msg_content = m.get("content") or ""
                if m.get("media_type"):
                    media_info = f"[Sent a {m['media_type']} file: {m['filename'] or 'attachment'}]"
                    msg_content = f"{media_info} {msg_content}".strip()
                history_lines.append(f"[{sender_label}]: {msg_content}")
        else:
            history_lines.append(f"[Customer]: {new_message}")
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

        try:
            response = self.llm.chat.completions.create(
                model=self.config.llm.model,
                messages=messages,
                temperature=self.config.llm.temperature,
            )
            reply = response.choices[0].message.content.strip()
            # Safety: don't send empty or obviously broken replies
            if not reply or len(reply) < 5:
                logger.warning(f"Generated reply too short: '{reply}'")
                return None
            return reply
        except Exception as e:
            logger.error(f"Reply generation error: {e}")
            return None

    def _retrieve_rag_context(self, message_content: str) -> str:
        """Query training chunks using embeddings + cosine similarity, fallback to keywords."""
        if not message_content or len(message_content.strip()) < 2:
            return ""

        logger.info(f"Retrieving RAG context for query: '{message_content[:50]}'")
        
        # 1. Try embedding similarity search via LM Studio
        try:
            url = f"{self.config.llm.base_url}/embeddings"
            payload = {"input": [message_content[:1000]], "model": "text-embedding-nomic-embed-text-v1.5"}
            headers = {}
            if self.config.llm.api_key:
                headers["Authorization"] = f"Bearer {self.config.llm.api_key}"
            res = requests.post(url, json=payload, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                query_emb = data["data"][0]["embedding"]
                
                # Fetch chunks with embeddings from TrackingDB
                chunks = self.tracking_db.get_training_chunks_with_embeddings()
                
                # Cosine similarity helper
                scored_chunks = []
                for chunk in chunks:
                    db_emb_bytes = chunk.get("embedding")
                    if not db_emb_bytes:
                        continue
                    
                    # Unpack embedding from varbinary
                    count = len(db_emb_bytes) // 4
                    chunk_emb = list(struct.unpack(f"{count}f", db_emb_bytes))
                    
                    if len(chunk_emb) != len(query_emb):
                        continue
                    
                    # Calculate similarity
                    dot = sum(a * b for a, b in zip(query_emb, chunk_emb))
                    norm_q = math.sqrt(sum(a * a for a in query_emb))
                    norm_c = math.sqrt(sum(c * c for c in chunk_emb))
                    sim = dot / (norm_q * norm_c + 1e-8)
                    
                    scored_chunks.append((sim, chunk))
                
                # Sort by score descending
                scored_chunks.sort(key=lambda x: x[0], reverse=True)
                
                # Select chunks above threshold (e.g. 0.4)
                top_chunks = [item[1] for item in scored_chunks[:3] if item[0] > 0.4]
                if top_chunks:
                    logger.info(f"Retrieved {len(top_chunks)} similar chunks via vector similarity")
                    return self._format_rag_prompt(top_chunks)
        except Exception as e:
            logger.warning(f"Vector search failed, falling back to keyword search: {e}")

        # 2. Fallback to keyword search
        try:
            # Clean keywords
            words = [w.strip("?,.!'\"()[]{}") for w in message_content.lower().split() if len(w) > 3]
            if words:
                conditions = " OR ".join(f"full_context LIKE '%{w}%'" for w in words[:5])
                chunks = self.tracking_db.get_keyword_chunks(conditions, limit=3)
                if chunks:
                    logger.info(f"Retrieved {len(chunks)} similar chunks via keyword fallback")
                    return self._format_rag_prompt(chunks)
        except Exception as e:
            logger.error(f"Keyword fallback failed: {e}")

        return ""

    def _format_rag_prompt(self, chunks: list) -> str:
        """Format chunks nicely into a prompt constraint context."""
        parts = [
            "=== HISTORICAL CONVERSATION EXAMPLES (For Wording, Tone & Context Reference Only) ===",
            "CRITICAL INSTRUCTION: The following examples show how real staff members word replies and handle unusual requests.",
            "However, you MUST prioritize the main system prompt rules and prices above everything else.",
            "If any price, fee, or rule in these examples differs from the main system prompt, IGNORE the example's price/rule and use the main system prompt's flat rates."
        ]
        for i, c in enumerate(chunks, 1):
            q = c.get("question_text", "").strip()
            a = c.get("answer_text", "").strip()
            if q and a:
                parts.append(f"Historical Example {i}:\nCustomer query: \"{q}\"\nStaff Response: \"{a}\"")
            elif c.get("full_context"):
                parts.append(f"Historical Context {i}:\n{c['full_context'].strip()}")
        parts.append("======================================================================================")
        return "\n\n".join(parts)


    def _send_message(self, recipient: str, message: str) -> bool:
        """Send a message via the Go bridge REST API."""
        try:
            url = f"{self.config.bridge_api_url}/send"
            payload = {"recipient": recipient, "message": message}
            response = requests.post(url, json=payload, timeout=15)

            if response.status_code == 200:
                result = response.json()
                success = result.get("success", False)
                if success:
                    # Auto-save booking confirmation to database if detected
                    if "booking confirmation" in message.lower():
                        try:
                            booking = parse_booking_confirmation(message)
                            if booking:
                                logger.info(f"Parsed booking confirmation from bot reply: {booking}")
                                
                                # Resolve friendly name if customer_name is numeric/JID
                                import re
                                cust_name = booking.get("customer_name") or ""
                                clean_name = re.sub(r'[\s\+\-\(\)@\.]', '', cust_name)
                                is_numeric = clean_name.isdigit() or "@" in cust_name
                                if not cust_name or is_numeric or cust_name == "Customer":
                                    contact_name = self.bridge_db.get_contact_name(recipient)
                                    if contact_name:
                                        clean_pot = re.sub(r'[\s\+\-\(\)@\.]', '', contact_name)
                                        if not clean_pot.isdigit() and "@" not in contact_name:
                                            booking["customer_name"] = contact_name

                                self.tracking_db.create_or_update_appointment(
                                    chat_jid=recipient,
                                    customer_name=booking["customer_name"],
                                    customer_email=booking["customer_email"],
                                    address=booking["address"] or "",
                                    clean_date=booking["clean_date"],
                                    clean_type=booking["clean_type"] or "Clean",
                                    price=booking["price"],
                                    status="pending"
                                )
                                logger.info("Successfully recorded bot booking confirmation as pending")
                                self._send_booking_confirmation_email(booking)
                        except Exception as ex:
                            logger.error(f"Failed to auto-save bot booking confirmation: {ex}", exc_info=True)
                    elif self._is_provisional_booking(message):
                        try:
                            # Extract details and save/update
                            context = self.bridge_db.get_conversation_context(recipient, limit=None)
                            booking = self._extract_booking_details(context)
                            if booking and booking.get("customer_name") and booking.get("clean_date"):
                                exists = self.tracking_db.get_appointments_by_jid(recipient)
                                is_update = len(exists) > 0
                                
                                # Resolve friendly name if customer_name is numeric/JID
                                import re
                                cust_name = booking.get("customer_name") or ""
                                clean_name = re.sub(r'[\s\+\-\(\)@\.]', '', cust_name)
                                is_numeric = clean_name.isdigit() or "@" in cust_name
                                if not cust_name or is_numeric or cust_name == "Customer":
                                    contact_name = self.bridge_db.get_contact_name(recipient)
                                    if contact_name:
                                        clean_pot = re.sub(r'[\s\+\-\(\)@\.]', '', contact_name)
                                        if not clean_pot.isdigit() and "@" not in contact_name:
                                            booking["customer_name"] = contact_name

                                app_id = self.tracking_db.create_or_update_appointment(
                                    chat_jid=recipient,
                                    customer_name=booking["customer_name"],
                                    customer_email=booking["customer_email"] or "",
                                    address=booking["address"] or "",
                                    clean_date=booking["clean_date"],
                                    clean_type=booking["clean_type"] or "Clean",
                                    price=booking["price"] or "",
                                    status="pending"
                                )
                                if app_id:
                                    logger.info(f"Successfully recorded bot provisional booking {app_id} (is_update={is_update})")
                                    self._send_provisional_booking_admin_email(booking, is_update)
                        except Exception as ex:
                            logger.error(f"Failed to auto-save bot provisional booking: {ex}", exc_info=True)
                return success
            else:
                logger.error(f"Bridge API error: HTTP {response.status_code} - {response.text}")
                return False
        except requests.RequestException as e:
            logger.error(f"Bridge API request error: {e}")
            return False

    def _send_booking_confirmation_email(self, booking: dict):
        try:
            resend_api_key = self.config.resend.get("api_key")
            resend_from = self.config.resend.get("from_email", "onboarding@resend.dev")
            
            if not resend_api_key:
                logger.warning("Resend API Key not configured. Skipping bot auto confirmation email.")
                return False
                
            import re
            address_str = booking.get("address") or ""
            postcode_match = re.search(r'([A-Z]{1,2}[0-9R][0-9A-Z]?\s*[0-9][A-Z]{2})', address_str.upper())
            postcode = postcode_match.group(1) if postcode_match else (booking.get("customer_name") or "CLIENT").split()[0].upper()
            
            # Get custom template from config or use defaults
            resend_subject = self.config.resend.get("email_subject", DEFAULT_EMAIL_SUBJECT)
            resend_body = self.config.resend.get("email_body_html", DEFAULT_EMAIL_BODY_HTML)
            
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
            
            # Log to DB
            try:
                self.tracking_db.log_email(booking["customer_email"], "info@0161cleanerinmanchester.co.uk", formatted_subject, formatted_body, status, error_msg)
            except Exception as log_ex:
                logger.error(f"Failed to log bot auto confirmation email: {log_ex}")
                
            if res.status_code == 200:
                logger.info("Successfully sent bot auto confirmation email to customer and CC'd admin.")
                return True
            else:
                logger.error(f"Failed to send bot auto confirmation email: Resend API returned {res.status_code} - {res.text}")
                return False
        except Exception as e:
            logger.error(f"Error in _send_booking_confirmation_email: {e}")
            return False

    def _is_payment_assertion(self, content: str) -> bool:
        """Analyze content to see if they say they sent a payment/deposit."""
        # Pre-filter keywords to avoid calling LLM on completely unrelated messages
        keywords = ["sent", "paid", "payment", "transfer", "deposit", "done", "receipt", "transferred", "money", "fee"]
        content_lower = content.lower()
        if not any(k in content_lower for k in keywords):
            return False

        try:
            response = self.llm.chat.completions.create(
                model=self.config.llm.model,
                messages=[
                    {"role": "system", "content": "You are a message classifier. Respond with ONLY one word: YES or NO."},
                    {
                        "role": "user",
                        "content": (
                            f"Analyze this customer message and determine if they are asserting that they have made/sent/transferred/paid a payment or booking deposit.\n"
                            f"Customer message: \"{content}\"\n"
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
            logger.warning(f"Payment assertion classification failed: {e}")
            return False

    def _handle_booking_payment(self, jid: str, sender: str, chat_name: str, content: str, context: list):
        """Auto-extract details, save confirmed booking, send email confirmation and notify admin."""
        # 1. Build conversation history
        history_lines = []
        if context:
            for m in context:
                sender_label = "Business (Us)" if m.get("is_from_me") else "Customer"
                history_lines.append(f"[{sender_label}]: {m['content']}")
        else:
            history_lines.append(f"[Customer]: {content}")
        history_text = "\n".join(history_lines)

        # 2. Ask LLM to extract booking details
        try:
            response = self.llm.chat.completions.create(
                model=self.config.llm.model,
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
            # Clean possible markdown wrapping
            if raw_json.startswith("```"):
                lines = raw_json.split("\n")
                if lines[0].startswith("```json") or lines[0].startswith("```"):
                    raw_json = "\n".join(lines[1:-1]).strip()

            import json
            data = json.loads(raw_json)

            customer_name = data.get("customer_name")
            customer_email = data.get("customer_email")
            address = data.get("address")
            clean_date = data.get("clean_date")
            clean_type = data.get("clean_type")
            price = data.get("price")

            # Resolve friendly name if customer_name is numeric/JID
            import re
            cust_name = customer_name or ""
            clean_name = re.sub(r'[\s\+\-\(\)@\.]', '', cust_name)
            is_numeric = clean_name.isdigit() or "@" in cust_name
            if not cust_name or is_numeric or cust_name == "Customer":
                contact_name = self.bridge_db.get_contact_name(jid)
                if contact_name:
                    clean_pot = re.sub(r'[\s\+\-\(\)@\.]', '', contact_name)
                    if not clean_pot.isdigit() and "@" not in contact_name:
                        customer_name = contact_name

            # Check if we have minimum viable details
            if not customer_name or not customer_email or not clean_date or not price:
                logger.warning(f"Failed to auto-confirm booking for {chat_name}: Missing vital details (Name: {customer_name}, Email: {customer_email}, Date: {clean_date}, Price: {price})")
                return False, None

            # 3. Create or update confirmed appointment in SQL Server
            app_id = self.tracking_db.create_or_update_appointment(
                chat_jid=jid,
                customer_name=customer_name,
                customer_email=customer_email,
                address=address or "",
                clean_date=clean_date,
                clean_type=clean_type or "Clean",
                price=price,
                status="confirmed"
            )

            if not app_id:
                logger.error(f"Failed to save confirmed appointment for {chat_name}")
                return False, None

            logger.info(f"Successfully saved confirmed appointment {app_id} for {customer_name}")

            # 4. Trigger Resend Emails
            resend_api_key = self.config.resend.get("api_key")
            resend_from = self.config.resend.get("from_email", "onboarding@resend.dev")

            if resend_api_key:
                # Build premium HTML email content
                html_content = f"""
                <div style="font-family: Arial, sans-serif; color: #2d3436; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #dfe6e9; border-radius: 8px;">
                    <h2 style="color: #2ecc71; text-align: center; border-bottom: 2px solid #2ecc71; padding-bottom: 10px;">📅 BOOKING CONFIRMED</h2>
                    <p>Hi <strong>{customer_name}</strong>,</p>
                    <p>Thank you for choosing <strong>Cleaner in Manchester (0161) Ltd</strong>. We are delighted to confirm that your deposit payment has been received and your booking is fully locked into our calendar! Here are your confirmed details:</p>
                    
                    <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                        <tr style="background-color: #f8f9fa;">
                            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">👤 Name</th>
                            <td style="padding: 10px; border: 1px solid #dfe6e9;">{customer_name}</td>
                        </tr>
                        <tr>
                            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🏡 Address</th>
                            <td style="padding: 10px; border: 1px solid #dfe6e9;">{address or 'Not provided'}</td>
                        </tr>
                        <tr style="background-color: #f8f9fa;">
                            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">📅 Date & Time</th>
                            <td style="padding: 10px; border: 1px solid #dfe6e9;">{clean_date}</td>
                        </tr>
                        <tr>
                            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🧹 Type of Clean</th>
                            <td style="padding: 10px; border: 1px solid #dfe6e9;">{clean_type or 'Clean'}</td>
                        </tr>
                        <tr style="background-color: #f8f9fa;">
                            <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">💰 Total Price</th>
                            <td style="padding: 10px; border: 1px solid #dfe6e9;">{price}</td>
                        </tr>
                    </table>
                    
                    <div style="background-color: #d4edda; color: #155724; padding: 15px; border-radius: 6px; margin: 20px 0; border: 1px solid #c3e6cb;">
                        <h4 style="margin-top: 0; color: #155724;">🔒 Deposit Received Successfully!</h4>
                        <p style="margin-bottom: 5px;">We have received your <strong>£50 secure booking fee</strong>. This has been fully deducted from your price. The remaining balance is payable on completion.</p>
                        <p style="font-size: 13px; color: #155724; margin-bottom: 0;">⚠️ <em>Cancellation Policy: Non-refundable if cancelled within 24 hours of your clean.</em></p>
                    </div>
                    
                    <p style="text-align: center; margin-top: 30px; font-size: 12px; color: #b2bec3;">
                        Cleaner in Manchester (0161) Ltd | Phone: 0161 710 4789 | Website: https://0161cleanerinmanchester.co.uk/
                    </p>
                </div>
                """

                # Send email to Customer
                status_cust = "failed"
                err_cust = None
                try:
                    logger.info(f"Sending confirmation email to customer: {customer_email}")
                    res = requests.post(
                        "https://api.resend.com/emails",
                        headers={
                            "Authorization": f"Bearer {resend_api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "from": resend_from,
                            "to": [customer_email],
                            "subject": f"Booking Confirmed! - {clean_type or 'Clean'} on {clean_date}",
                            "html": html_content
                        },
                        timeout=10
                    )
                    status_cust = "success" if res.status_code == 200 else "failed"
                    if res.status_code != 200:
                        err_cust = f"HTTP {res.status_code}: {res.text}"
                except Exception as ex:
                    logger.error(f"Failed to email customer: {ex}")
                    err_cust = str(ex)
                
                try:
                    self.tracking_db.log_email(customer_email, None, f"Booking Confirmed! - {clean_type or 'Clean'} on {clean_date}", html_content, status_cust, err_cust)
                except Exception as log_ex:
                    logger.error(f"Failed to log customer payment confirmation email: {log_ex}")

                # Sleep 30 seconds to avoid Resend API rate limits as requested
                logger.info("Sleeping 30 seconds to avoid Resend API rate limits...")
                import time
                time.sleep(30)

                # Send email to Admin (info@0161cleanerinmanchester.co.uk)
                status_admin = "failed"
                err_admin = None
                admin_html = ""
                try:
                    logger.info("Sending confirmation email copy to admin...")
                    admin_html = f"""
                    <div style="font-family: Arial, sans-serif; color: #2d3436; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #dfe6e9; border-radius: 8px;">
                        <h2 style="color: #e67e22; text-align: center; border-bottom: 2px solid #e67e22; padding-bottom: 10px;">🔔 NEW BOOKING PAID & CONFIRMED</h2>
                        <p>A new WhatsApp booking has been confirmed via deposit payment receipt detection!</p>
                        
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
                                <td style="padding: 10px; border: 1px solid #dfe6e9;">{address or 'Not provided'}</td>
                            </tr>
                            <tr>
                                <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">📅 Date & Time</th>
                                <td style="padding: 10px; border: 1px solid #dfe6e9;">{clean_date}</td>
                            </tr>
                            <tr style="background-color: #f8f9fa;">
                                <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">🧹 Type of Clean</th>
                                <td style="padding: 10px; border: 1px solid #dfe6e9;">{clean_type or 'Clean'}</td>
                            </tr>
                            <tr>
                                <th style="text-align: left; padding: 10px; border: 1px solid #dfe6e9;">💰 Total Price</th>
                                <td style="padding: 10px; border: 1px solid #dfe6e9;">{price}</td>
                            </tr>
                        </table>
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
                            "subject": f"🔔 Booking Paid! - {customer_name} ({clean_date})",
                            "html": admin_html
                        },
                        timeout=10
                    )
                    status_admin = "success" if res.status_code == 200 else "failed"
                    if res.status_code != 200:
                        err_admin = f"HTTP {res.status_code}: {res.text}"
                except Exception as ex:
                    logger.error(f"Failed to email admin: {ex}")
                    err_admin = str(ex)
                    
                try:
                    self.tracking_db.log_email("info@0161cleanerinmanchester.co.uk", None, f"🔔 Booking Paid! - {customer_name} ({clean_date})", admin_html, status_admin, err_admin)
                except Exception as log_ex:
                    logger.error(f"Failed to log admin payment confirmation email: {log_ex}")
            else:
                logger.warning("Resend API not configured. Skipping confirmation emails.")

            whatsapp_reply = (
                f"Thank you, {customer_name}! 😊 I have successfully verified your deposit payment. 💰\n\n"
                f"Your booking is now **fully locked in and confirmed** in our calendar! 📅\n\n"
                f"🏡 **Address**: {address}\n"
                f"📅 **Date & Time**: {clean_date}\n"
                f"🧹 **Clean Type**: {clean_type}\n"
                f"💰 **Total Price**: {price}\n\n"
                f"A detailed confirmation email has been sent to **{customer_email}**. 📧 We look forward to seeing you then!\n\n"
                f"If you need anything else, feel free to ask! 🧹"
            )
            return True, whatsapp_reply

        except Exception as e:
            logger.error(f"Error handling booking payment confirmation: {e}", exc_info=True)
            return False, None

    def _is_provisional_booking(self, content: str) -> bool:
        """Analyze content to see if the bot is provisionally booking or handing off."""
        keywords = ["touch", "confirm", "provisionally", "book", "guide", "team", "person", "assistant"]
        content_lower = content.lower()
        if not any(k in content_lower for k in keywords):
            return False

        try:
            response = self.llm.chat.completions.create(
                model=self.config.llm.model,
                messages=[
                    {"role": "system", "content": "You are a message classifier. Respond with ONLY one word: YES or NO."},
                    {
                        "role": "user",
                        "content": (
                            f"Analyze this business assistant message to see if they are handing off the customer to the human team, "
                            f"provisionally booking/scheduling a clean, or providing a guide price and closing the conversation.\n"
                            f"Assistant message: \"{content}\"\n"
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
            logger.warning(f"Provisional booking classification failed: {e}")
            return False

    def _extract_booking_details(self, context: list) -> Optional[dict]:
        history_lines = []
        for m in context:
            sender_label = "Business (Us)" if m.get("is_from_me") else "Customer"
            history_lines.append(f"[{sender_label}]: {m.get('content') or ''}")
        history_text = "\n".join(history_lines)
        
        try:
            response = self.llm.chat.completions.create(
                model=self.config.llm.model,
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
            logger.error(f"Failed to extract booking details: {e}")
            return None

    def _send_provisional_booking_admin_email(self, booking: dict, is_update: bool):
        try:
            resend_api_key = self.config.resend.get("api_key")
            resend_from = self.config.resend.get("from_email", "onboarding@resend.dev")
            
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
                self.tracking_db.log_email("info@0161cleanerinmanchester.co.uk", None, subject, html_content, status, error_msg)
            except Exception as log_ex:
                logger.error(f"Failed to log admin provisional booking email: {log_ex}")
                
            return res.status_code == 200
        except Exception as e:
            logger.error(f"Error sending provisional booking email: {e}")
            return False
