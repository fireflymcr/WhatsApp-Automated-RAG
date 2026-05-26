"""
Database helpers for the WhatsApp Bot.
- SQL Server (pymssql): persistent tracking data
- SQLite (read-only): bridge message database
"""

import sqlite3
import logging
from datetime import datetime, date
from typing import List, Dict, Any, Optional

import pymssql

from config import BotConfig

logger = logging.getLogger("whatsapp-bot.db")


class BridgeDB:
    """Read-only access to the Go bridge's SQLite messages database."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def get_recent_messages(self, since_minutes: int) -> List[Dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT m.id AS message_id, m.chat_jid, m.sender, m.content,
                       m.timestamp, m.is_from_me, m.media_type, c.name AS chat_name
                FROM messages m JOIN chats c ON m.chat_jid = c.jid
                WHERE m.is_from_me = 0 AND m.content IS NOT NULL AND m.content != ''
                  AND m.timestamp >= datetime('now', ? || ' minutes')
                ORDER BY m.timestamp ASC
            """, (f"-{since_minutes}",))
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Bridge DB error: {e}")
            return []
        finally:
            conn.close()

    def get_contact_name(self, chat_jid: str) -> Optional[str]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM chats WHERE jid = ?", (chat_jid,))
            row = cur.fetchone()
            if row and row["name"]:
                return row["name"].strip()
            return None
        except sqlite3.Error as e:
            logger.error(f"Bridge DB get_contact_name error: {e}")
            return None
        finally:
            conn.close()

    def get_conversation_context(self, chat_jid: str, limit: Optional[int] = None) -> List[Dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            if limit is not None:
                cur.execute("""
                    SELECT m.sender, m.content, m.timestamp, m.is_from_me, m.media_type, m.filename, c.name AS chat_name
                    FROM messages m JOIN chats c ON m.chat_jid = c.jid
                    WHERE m.chat_jid = ? AND ((m.content IS NOT NULL AND m.content != '') OR (m.media_type IS NOT NULL AND m.media_type != ''))
                    ORDER BY m.timestamp DESC LIMIT ?
                """, (chat_jid, limit))
            else:
                cur.execute("""
                    SELECT m.sender, m.content, m.timestamp, m.is_from_me, m.media_type, m.filename, c.name AS chat_name
                    FROM messages m JOIN chats c ON m.chat_jid = c.jid
                    WHERE m.chat_jid = ? AND ((m.content IS NOT NULL AND m.content != '') OR (m.media_type IS NOT NULL AND m.media_type != ''))
                    ORDER BY m.timestamp DESC
                """, (chat_jid,))
            rows = [dict(r) for r in cur.fetchall()]
            rows.reverse()
            return rows
        except sqlite3.Error as e:
            logger.error(f"Bridge DB context error: {e}")
            return []
        finally:
            conn.close()

    def get_group_chats(self) -> List[Dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT jid, name, last_message_time FROM chats
                WHERE jid LIKE '%@g.us' ORDER BY last_message_time DESC
            """)
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Bridge DB groups error: {e}")
            return []
        finally:
            conn.close()


class TrackingDB:
    """SQL Server connection for persistent bot tracking data."""

    def __init__(self, config: BotConfig):
        self.prefix = config.instance_name
        self._server = config.database.server
        self._database = config.database.database
        self._user = config.database.username
        self._password = config.database.password

    def _connect(self):
        parts = self._server.split(",")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 1433
        return pymssql.connect(
            server=host, port=port, user=self._user,
            password=self._password, database=self._database, as_dict=True,
        )

    def run_migrations(self):
        p = self.prefix
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{p}_replied_messages' AND xtype='U')
                CREATE TABLE {p}_replied_messages (
                    message_id NVARCHAR(255) NOT NULL, chat_jid NVARCHAR(255) NOT NULL,
                    replied_at DATETIME2 DEFAULT GETDATE(), reply_text NVARCHAR(MAX),
                    classification NVARCHAR(50), PRIMARY KEY (message_id, chat_jid))
            """)
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{p}_reply_cooldowns' AND xtype='U')
                CREATE TABLE {p}_reply_cooldowns (
                    chat_jid NVARCHAR(255) PRIMARY KEY, last_reply_at DATETIME2,
                    reply_count_today INT DEFAULT 0, last_count_reset DATE)
            """)
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{p}_message_log' AND xtype='U')
                CREATE TABLE {p}_message_log (
                    id INT IDENTITY(1,1) PRIMARY KEY, message_id NVARCHAR(255),
                    chat_jid NVARCHAR(255), sender NVARCHAR(255), content NVARCHAR(MAX),
                    classification NVARCHAR(50), action_taken NVARCHAR(50),
                    reply_text NVARCHAR(MAX), processed_at DATETIME2 DEFAULT GETDATE())
            """)
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{p}_marketing_messages' AND xtype='U')
                CREATE TABLE {p}_marketing_messages (
                    id INT IDENTITY(1,1) PRIMARY KEY, content NVARCHAR(MAX) NOT NULL,
                    target_groups NVARCHAR(MAX), scheduled_at DATETIME2,
                    status NVARCHAR(20) DEFAULT 'pending', created_at DATETIME2 DEFAULT GETDATE())
            """)
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{p}_marketing_log' AND xtype='U')
                CREATE TABLE {p}_marketing_log (
                    id INT IDENTITY(1,1) PRIMARY KEY, marketing_id INT NOT NULL,
                    group_jid NVARCHAR(255) NOT NULL, sent_at DATETIME2 DEFAULT GETDATE(),
                    success BIT, error_message NVARCHAR(MAX))
            """)
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{p}_appointments' AND xtype='U')
                CREATE TABLE {p}_appointments (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    chat_jid NVARCHAR(255),
                    customer_name NVARCHAR(255),
                    customer_email NVARCHAR(255),
                    address NVARCHAR(MAX),
                    clean_date NVARCHAR(255),
                    clean_type NVARCHAR(255),
                    price NVARCHAR(50),
                    status NVARCHAR(50) DEFAULT 'pending',
                    created_at DATETIME2 DEFAULT GETDATE())
            """)
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{p}_email_log' AND xtype='U')
                CREATE TABLE {p}_email_log (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    recipient NVARCHAR(255) NOT NULL,
                    cc NVARCHAR(MAX),
                    subject NVARCHAR(500),
                    body NVARCHAR(MAX),
                    status NVARCHAR(50) DEFAULT 'success',
                    error_message NVARCHAR(MAX),
                    sent_at DATETIME2 DEFAULT GETDATE())
            """)
            conn.commit()
            logger.info(f"Migrations complete for '{p}'")
        finally:
            conn.close()

    def is_already_replied(self, message_id: str, chat_jid: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT message_id FROM {self.prefix}_replied_messages WHERE message_id=%s AND chat_jid=%s",
                (message_id, chat_jid))
            return cur.fetchone() is not None
        finally:
            conn.close()

    def record_reply(self, message_id: str, chat_jid: str, reply_text: str, classification: str):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO {self.prefix}_replied_messages (message_id,chat_jid,reply_text,classification) VALUES (%s,%s,%s,%s)",
                (message_id, chat_jid, reply_text, classification))
            conn.commit()
        finally:
            conn.close()

    def record_skip(self, message_id: str, chat_jid: str, sender: str, content: str, classification: str, action: str):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO {self.prefix}_message_log (message_id,chat_jid,sender,content,classification,action_taken) VALUES (%s,%s,%s,%s,%s,%s)",
                (message_id, chat_jid, sender, content, classification, action))
            cur.execute(
                f"INSERT INTO {self.prefix}_replied_messages (message_id,chat_jid,classification) VALUES (%s,%s,%s)",
                (message_id, chat_jid, classification))
            conn.commit()
        finally:
            conn.close()

    def log_reply(self, message_id: str, chat_jid: str, sender: str, content: str, classification: str, reply_text: str):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO {self.prefix}_message_log (message_id,chat_jid,sender,content,classification,action_taken,reply_text) VALUES (%s,%s,%s,%s,%s,'replied',%s)",
                (message_id, chat_jid, sender, content, classification, reply_text))
            conn.commit()
        finally:
            conn.close()

    def check_cooldown(self, chat_jid: str, cooldown_minutes: int) -> bool:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT last_reply_at FROM {self.prefix}_reply_cooldowns WHERE chat_jid=%s", (chat_jid,))
            row = cur.fetchone()
            if not row or not row["last_reply_at"]:
                return False
            elapsed = (datetime.now() - row["last_reply_at"]).total_seconds() / 60
            return elapsed < cooldown_minutes
        finally:
            conn.close()

    def check_daily_limit(self, chat_jid: str, max_per_day: int) -> bool:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT reply_count_today, last_count_reset FROM {self.prefix}_reply_cooldowns WHERE chat_jid=%s", (chat_jid,))
            row = cur.fetchone()
            if not row:
                return False
            today = date.today()
            if row["last_count_reset"] != today:
                cur.execute(f"UPDATE {self.prefix}_reply_cooldowns SET reply_count_today=0, last_count_reset=%s WHERE chat_jid=%s", (today, chat_jid))
                conn.commit()
                return False
            return row["reply_count_today"] >= max_per_day
        finally:
            conn.close()

    def update_cooldown(self, chat_jid: str):
        today = date.today()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                IF EXISTS (SELECT chat_jid FROM {self.prefix}_reply_cooldowns WHERE chat_jid=%s)
                    UPDATE {self.prefix}_reply_cooldowns SET last_reply_at=GETDATE(),
                        reply_count_today=CASE WHEN last_count_reset=%s THEN reply_count_today+1 ELSE 1 END,
                        last_count_reset=%s WHERE chat_jid=%s
                ELSE
                    INSERT INTO {self.prefix}_reply_cooldowns (chat_jid,last_reply_at,reply_count_today,last_count_reset) VALUES (%s,GETDATE(),1,%s)
            """, (chat_jid, today, today, chat_jid, chat_jid, today))
            conn.commit()
        finally:
            conn.close()

    def get_pending_marketing(self) -> List[Dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT id,content,target_groups FROM {self.prefix}_marketing_messages WHERE status='pending' AND (scheduled_at IS NULL OR scheduled_at<=GETDATE()) ORDER BY created_at ASC")
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def mark_marketing_sent(self, marketing_id: int):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"UPDATE {self.prefix}_marketing_messages SET status='sent' WHERE id=%s", (marketing_id,))
            conn.commit()
        finally:
            conn.close()

    def log_marketing_delivery(self, marketing_id: int, group_jid: str, success: bool, error: str = None):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"INSERT INTO {self.prefix}_marketing_log (marketing_id,group_jid,success,error_message) VALUES (%s,%s,%s,%s)",
                (marketing_id, group_jid, success, error))
            conn.commit()
        finally:
            conn.close()

    def get_training_chunks_with_embeddings(self) -> List[Dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT question_text, answer_text, full_context, embedding FROM {self.prefix}_training_chunks WHERE chunk_type = 'qa_pair' AND embedding IS NOT NULL")
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []
        finally:
            conn.close()

    def get_keyword_chunks(self, conditions: str, limit: int = 3) -> List[Dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            # Simple sanitization / parameterization is not strictly needed for internal bot queries but good safety
            cur.execute(f"SELECT TOP {limit} question_text, answer_text, full_context FROM {self.prefix}_training_chunks WHERE chunk_type = 'qa_pair' AND ({conditions}) ORDER BY id DESC")
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []
        finally:
            conn.close()

    def create_or_update_appointment(self, chat_jid: str, customer_name: str, customer_email: str, address: str, clean_date: str, clean_type: str, price: str, status: str) -> int:
        conn = self._connect()
        try:
            cur = conn.cursor()
            # Check for existing appointment for this chat_jid (prefer pending first, then confirmed)
            cur.execute(f"SELECT id, status FROM {self.prefix}_appointments WHERE chat_jid=%s ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, created_at DESC", (chat_jid,))
            row = cur.fetchone()
            if row:
                app_id = row['id']
                existing_status = row.get('status', 'pending')
                # Preserve confirmed status if it was already confirmed
                final_status = 'confirmed' if existing_status == 'confirmed' else status
                cur.execute(f"""
                    UPDATE {self.prefix}_appointments
                    SET customer_name=%s, customer_email=%s, address=%s, clean_date=%s, clean_type=%s, price=%s, status=%s
                    WHERE id=%s
                """, (customer_name, customer_email, address, clean_date, clean_type, price, final_status, app_id))
                conn.commit()
                return app_id
            else:
                cur.execute(f"""
                    INSERT INTO {self.prefix}_appointments (chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (chat_jid, customer_name, customer_email, address, clean_date, clean_type, price, status))
                cur.execute("SELECT @@IDENTITY AS id")
                row = cur.fetchone()
                conn.commit()
                return row['id'] if row else 0
        except Exception as e:
            # use class logger
            logger.error(f"Failed to create/update appointment: {e}")
            return 0
        finally:
            conn.close()

    def get_appointments_by_jid(self, chat_jid: str) -> List[Dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT id, customer_name, customer_email, address, clean_date, clean_type, price, status, clean_status, notes 
                FROM {self.prefix}_appointments 
                WHERE chat_jid=%s
            """, (chat_jid,))
            return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to fetch appointments by JID: {e}")
            return []
        finally:
            conn.close()

    def log_email(self, recipient: str, cc: str, subject: str, body: str, status: str, error_message: str = None) -> bool:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                INSERT INTO {self.prefix}_email_log (recipient, cc, subject, body, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (recipient, cc, subject, body, status, error_message))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to log email to database: {e}")
            return False
        finally:
            conn.close()

