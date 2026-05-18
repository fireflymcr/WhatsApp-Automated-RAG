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
            context = self.bridge_db.get_conversation_context(jid, self.config.context_messages)

            # ── Check if already answered by us ──
            if context and context[-1].get("is_from_me"):
                logger.info(f"[{chat_name}] Latest message is from us. Skipping.")
                self.tracking_db.record_skip(mid, jid, sender, content, classification, "skipped_already_answered")
                skipped_count += 1
                continue

            # ── Generate AI reply ──
            reply_text = self._generate_reply(context, content, chat_name, sender)
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

    def _generate_reply(self, context: list, new_message: str, chat_name: str, sender: str) -> Optional[str]:
        """Generate a reply using the system prompt and conversation context."""
        system_prompt = self.config.system_prompt
        
        # ── RAG Retrieval ──
        rag_context = self._retrieve_rag_context(new_message)
        if rag_context:
            system_prompt += f"\n\n{rag_context}"
            logger.info("RAG context successfully injected into system prompt")

        # Append final hierarchy reinforcement instruction to make Bot Config the absolute main authority
        system_prompt += (
            "\n\n=== MAIN BOT CONFIGURATION SYSTEM PROMPT DIRECTIVE (SUPREME PRIORITY) ===\n"
            "1. The configured SYSTEM PROMPT rules and CONTACT DETAILS above are your ABSOLUTE AND SUPREME AUTHORITIES.\n"
            "2. Do NOT copy, imitate, or rely on any conflicting prices, deposit details, bank accounts, or procedures from any historical examples below. The main rules above MUST override any historical examples.\n"
            "3. The 'HISTORICAL CONVERSATION EXAMPLES' provided below are ONLY to help you understand general vocabulary, tone of voice, and stylistic preferences. They are pure reference material. The actual rules, bank details, and procedures in the main System Prompt are the absolute truth.\n"
            "4. Specifically, for booking confirmations: ALWAYS follow the 'BOOKING RULES & CONFIRMATIONS' section in the main system prompt. If they have not provided all 5 details (Name, Address, Date/Time, Clean Type, Price), do NOT confirm the booking! Ask them to confirm the missing details first.\n"
            "============================================================"
        )

        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history
        history_lines = []
        if context:
            for m in context:
                sender_label = "Business (Us)" if m.get("is_from_me") else "Customer"
                history_lines.append(f"[{sender_label}]: {m['content']}")
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
                return result.get("success", False)
            else:
                logger.error(f"Bridge API error: HTTP {response.status_code} - {response.text}")
                return False
        except requests.RequestException as e:
            logger.error(f"Bridge API request error: {e}")
            return False
