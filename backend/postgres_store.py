"""
Appello Postgres Store
Handles persistent logging of calls, leads, and full transcript history on Neon/Postgres.
Includes automatic table initialization.
"""

import asyncio
import logging
import os
import time
from datetime import date, timedelta
from typing import Optional, List, Dict, Any
import asyncpg

from tenancy import (
    DEFAULT_TENANT_ID,
    admin_scope,
    initialize_tenancy,
    tenant_scope,
)

logger = logging.getLogger("appello")

class PostgresStore:
    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self, tenant_id: Optional[str] = None):
        """Establish connection pool to Postgres (Neon or local) with retries."""
        if not self.database_url:
            logger.warning("⚠️ DATABASE_URL not set. Skipping Postgres integration.")
            return

        max_retries = 5
        backoff = 2
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"🔌 Connecting to PostgreSQL (attempt {attempt}/{max_retries})...")
                self.pool = await asyncpg.create_pool(self.database_url)
                logger.info("🔌 Connected to PostgreSQL pool successfully")
                await self.initialize_tables()
                return
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                logger.error(f"❌ Connection attempt {attempt} failed: {err_msg}")
                self.pool = None
                if attempt < max_retries:
                    logger.info(f"Waiting {backoff}s before retrying...")
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    logger.error("❌ Failed to connect to PostgreSQL after all retry attempts.")

    # ─── Tenant-scoped access ────────────────────────────────────────────

    def acquire(self, tenant_id: Optional[str] = None):
        """Acquire a connection bound to one tenant, for use in `async with`.

        This is the replacement for `self.acquire(tenant_id)`. It opens a transaction
        and sets the transaction-local `app.tenant_id` GUC that the RLS policies
        read, so anything run inside sees exactly one tenant's rows.

        Passing no tenant_id falls back to the default tenant, which is where all
        pre-tenancy data was backfilled. That keeps older call paths working
        while they are migrated one at a time.
        """
        return tenant_scope(self.pool, tenant_id or DEFAULT_TENANT_ID)

    def acquire_admin(self):
        """Acquire a connection that can see across every tenant.

        Platform-level reads only — billing rollups, the tenant list, and the
        lookups that happen before a tenant is known. Never call this from a
        route that serves one tenant.
        """
        return admin_scope(self.pool)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def initialize_tables(self):
        """Auto-create tables for leads, calls, and transcripts if they don't exist.

        Runs in admin scope: this is schema migration, not a tenant operation, and
        it has to be able to see and alter every tenant's rows.
        """
        if not self.pool:
            return

        async with self.acquire_admin() as conn:
            async with conn.transaction():
                # 1. Leads Table (stores customer profile info)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS leads (
                        id SERIAL PRIMARY KEY,
                        phone_number VARCHAR(20) UNIQUE NOT NULL,
                        name VARCHAR(100) NOT NULL,
                        loan_id VARCHAR(50),
                        emi_amount NUMERIC(10, 2),
                        overdue_days INT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 2. Calls Table (logs call sessions)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS calls (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(100) UNIQUE NOT NULL,
                        phone_number VARCHAR(20) NOT NULL,
                        scenario VARCHAR(50) NOT NULL,
                        status VARCHAR(20) DEFAULT 'connected',
                        start_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        end_time TIMESTAMP WITH TIME ZONE,
                        summary TEXT
                    );
                """)

                # 3. Transcripts Table (persists turn-by-turn chat transcripts)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS transcripts (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(100) NOT NULL,
                        role VARCHAR(20) NOT NULL, -- 'user' or 'assistant'
                        text TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # 4. Availability Slots Table (spa/restaurant booking)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS availability_slots (
                        id SERIAL PRIMARY KEY,
                        service_type VARCHAR(50) NOT NULL,
                        date DATE NOT NULL,
                        time_slot VARCHAR(20) NOT NULL,
                        duration_minutes INT DEFAULT 60,
                        is_available BOOLEAN DEFAULT TRUE,
                        price VARCHAR(20),
                        UNIQUE (service_type, date, time_slot)
                    );
                """)

                # 5. Bookings Table (confirmed customer appointments)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS bookings (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(100) NOT NULL,
                        customer_name VARCHAR(100),
                        customer_phone VARCHAR(20),
                        service_type VARCHAR(50) NOT NULL,
                        booking_date DATE NOT NULL,
                        booking_time VARCHAR(20) NOT NULL,
                        status VARCHAR(20) DEFAULT 'confirmed',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 6. Knowledge Files Table (Centralized storage for dynamic KB files)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_files (
                        id SERIAL PRIMARY KEY,
                        user_email VARCHAR(100) NOT NULL,
                        name VARCHAR(255) NOT NULL,
                        category VARCHAR(50) NOT NULL,
                        agent_id VARCHAR(100) DEFAULT 'restaurant_booking',
                        format VARCHAR(20),
                        size INT,
                        chunk_count INT,
                        status VARCHAR(20) DEFAULT 'indexed',
                        uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 9. Restaurant Reservations Table
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS restaurant_reservations (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(100),
                        customer_name VARCHAR(100),
                        phone_number VARCHAR(20),
                        slot_date DATE,
                        slot_time VARCHAR(20),
                        party_size INT,
                        special_requests TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 10. Restaurant Pre-orders Table
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS restaurant_pre_orders (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(100),
                        customer_name VARCHAR(100),
                        phone_number VARCHAR(20),
                        items TEXT,
                        total_amount VARCHAR(50),
                        arrival_time VARCHAR(20),
                        slot_date DATE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 11. Restaurant Booking Logs Table
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS restaurant_booking_logs (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(100) UNIQUE NOT NULL,
                        customer_name VARCHAR(100) DEFAULT '-',
                        call_datetime TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        booking_details VARCHAR(255) DEFAULT '-',
                        special_requests TEXT DEFAULT '-',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 12. Feedback Agent Logs Table (Stores feedback/lead qualification)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_agent_logs (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(100) UNIQUE NOT NULL,
                        customer_name VARCHAR(100) DEFAULT '-',
                        call_datetime TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        product_review TEXT DEFAULT '-',
                        satisfaction_level VARCHAR(50) DEFAULT 'neutral',
                        overall_experience TEXT DEFAULT '-',
                        sentiment VARCHAR(50) DEFAULT 'neutral',
                        escalation_required BOOLEAN DEFAULT FALSE,
                        call_summary TEXT DEFAULT '-',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # 12. Feedback Agent Logs Table (Stores feedback/lead qualification)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_agent_logs (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(100) UNIQUE NOT NULL,
                        customer_name VARCHAR(100) DEFAULT '-',
                        call_datetime TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        product_review TEXT DEFAULT '-',
                        satisfaction_level VARCHAR(50) DEFAULT 'neutral',
                        overall_experience TEXT DEFAULT '-',
                        sentiment VARCHAR(50) DEFAULT 'neutral',
                        escalation_required BOOLEAN DEFAULT FALSE,
                        call_summary TEXT DEFAULT '-',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 13. Reminder Contacts Table
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS reminder_contacts (
                        id SERIAL PRIMARY KEY,
                        user_email VARCHAR(100) NOT NULL,
                        name VARCHAR(100) NOT NULL,
                        phone VARCHAR(20) NOT NULL,
                        location VARCHAR(100),
                        priority VARCHAR(20) DEFAULT 'Normal',
                        tags TEXT[] DEFAULT '{}'::TEXT[],
                        notes TEXT,
                        domain VARCHAR(50) DEFAULT 'restaurant',
                        status VARCHAR(20) DEFAULT 'pending',
                        scheduled_at VARCHAR(100),
                        attributes JSONB DEFAULT '{}'::JSONB,
                        call_history JSONB DEFAULT '[]'::JSONB,
                        attempt_number INT DEFAULT 0,
                        total_attempts INT DEFAULT 3,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Ensure phone_number columns exist in case tables were created in older versions without it
                await conn.execute("ALTER TABLE restaurant_reservations ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20);")
                await conn.execute("ALTER TABLE restaurant_pre_orders ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20);")

                # Tenancy runs last: it alters the tables created above, so they
                # all have to exist first.
                await initialize_tenancy(conn)

                logger.info("📁 Database schema verified (leads, calls, transcripts, availability_slots, bookings, knowledge_files, restaurant_reservations, restaurant_pre_orders, restaurant_booking_logs, feedback_agent_logs, reminder_contacts tables ready)")

    async def save_lead(self, name: str, phone: str, loan_id: str, emi: float, overdue: int, tenant_id: Optional[str] = None):
        """Seed or update a customer profile."""
        if not self.pool:
            return
        try:
            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    INSERT INTO leads (name, phone_number, loan_id, emi_amount, overdue_days)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (tenant_id, phone_number) DO UPDATE
                    SET name = EXCLUDED.name,
                        loan_id = EXCLUDED.loan_id,
                        emi_amount = EXCLUDED.emi_amount,
                        overdue_days = EXCLUDED.overdue_days;
                """, name, phone, loan_id, emi, overdue)
        except Exception as e:
            logger.error(f"[db] Error saving lead: {e}")

    async def log_call_start(self, session_id: str, phone_number: str, scenario: str, tenant_id: Optional[str] = None):
        """Create call entry when user clicks 'Talk' / connects."""
        if not self.pool:
            return
        try:
            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    INSERT INTO calls (session_id, phone_number, scenario, status)
                    VALUES ($1, $2, $3, 'active')
                    ON CONFLICT (session_id) DO NOTHING;
                """, session_id, phone_number, scenario)
        except Exception as e:
            logger.error(f"[db] Error logging call start: {e}")

    async def log_call_end(self, session_id: str, summary: Optional[str] = None, tenant_id: Optional[str] = None):
        """Mark call complete with end time and overall summary."""
        if not self.pool:
            return
        try:
            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    UPDATE calls 
                    SET status = 'completed', 
                        end_time = CURRENT_TIMESTAMP, 
                        summary = $2
                    WHERE session_id = $1;
                """, session_id, summary)
        except Exception as e:
            logger.error(f"[db] Error logging call end: {e}")

    async def save_transcript_turn(self, session_id: str, role: str, text: str, tenant_id: Optional[str] = None):
        """Log a single turn of the conversation."""
        if not self.pool:
            return
        try:
            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    INSERT INTO transcripts (session_id, role, text)
                    VALUES ($1, $2, $3);
                """, session_id, role, text)
        except Exception as e:
            logger.error(f"[db] Error saving transcript: {e}")

    # ─── Booking & Availability Methods ──────────────────────────────────

    async def seed_demo_availability(self, tenant_id: Optional[str] = None):
        """Seed availability_slots with demo spa services for the next 7 days.
        Idempotent — uses ON CONFLICT DO NOTHING so it's safe to call on every startup."""
        if not self.pool:
            return

        services = [
            ("Swedish Massage", 60, "$89"),
            ("Deep Tissue Massage", 90, "$120"),
            ("Hot Stone Therapy", 75, "$110"),
            ("Facial Treatment", 45, "$65"),
            ("Aromatherapy Massage", 60, "$95"),
        ]
        time_slots = [
            "9:00 AM", "10:00 AM", "11:00 AM",
            "1:00 PM", "2:00 PM", "3:00 PM", "4:00 PM", "5:00 PM",
        ]

        today = date.today()
        inserted = 0

        try:
            async with self.acquire(tenant_id) as conn:
                for day_offset in range(1, 8):  # next 7 days
                    slot_date = today + timedelta(days=day_offset)
                    for service_name, duration, price in services:
                        for ts in time_slots:
                            result = await conn.execute("""
                                INSERT INTO availability_slots
                                    (service_type, date, time_slot, duration_minutes, is_available, price)
                                VALUES ($1, $2, $3, $4, TRUE, $5)
                                ON CONFLICT (tenant_id, service_type, date, time_slot) DO NOTHING;
                            """, service_name, slot_date, ts, duration, price)
                            if result == "INSERT 0 1":
                                inserted += 1
            logger.info(f"[db] Seeded {inserted} new availability slots (next 7 days, {len(services)} services)")
        except Exception as e:
            logger.error(f"[db] Error seeding availability: {e}")

    async def get_availability(self, slot_date: str = None, service_type: str = None, tenant_id: Optional[str] = None) -> list:
        """Query available slots, optionally filtered by date and/or service type."""
        if not self.pool:
            return []
        try:
            async with self.acquire(tenant_id) as conn:
                query = ("SELECT service_type, slot_date, slot_time, slot_type "
                         "FROM availability_slots WHERE is_available = TRUE")
                params: list = []
                param_idx = 1

                if slot_date:
                    from datetime import datetime
                    import dateutil.parser
                    clean_date = str(slot_date).replace('"', '').replace("'", "").strip()
                    try:
                        parsed_date = datetime.strptime(clean_date.split("T")[0], "%Y-%m-%d").date()
                    except ValueError:
                        try:
                            parsed_date = dateutil.parser.parse(clean_date).date()
                        except Exception:
                            parsed_date = date.today()
                    query += f" AND slot_date = ${param_idx}"
                    params.append(parsed_date)
                    param_idx += 1

                if service_type:
                    query += f" AND LOWER(service_type) LIKE ${param_idx}"
                    params.append(f"%{service_type.lower()}%")
                    param_idx += 1

                query += " ORDER BY slot_date, slot_time LIMIT 20"
                rows = await conn.fetch(query, *params)
                result = []
                for r in rows:
                    result.append({
                        "service": r["service_type"],
                        "date": r["slot_date"].isoformat() if r["slot_date"] else "",
                        "time": r["slot_time"],
                        "duration_minutes": 60,
                        "price": 0,
                    })
                return result
        except Exception as e:
            logger.error(f"[db] Error fetching availability: {e}")
            return []

    async def create_booking(self, session_id: str, customer_name: str, customer_phone: str,
                             service_type: str, booking_date: str, booking_time: str, tenant_id: Optional[str] = None) -> dict:
        """Book an appointment: mark the slot as taken and create a booking record.
        Returns a dict with success status and booking details."""
        if not self.pool:
            return {"success": False, "error": "Database not connected"}
        try:
            parsed_date = date.fromisoformat(booking_date)
        except ValueError:
            return {"success": False, "error": f"Invalid date format: {booking_date}. Use YYYY-MM-DD."}

        try:
            async with self.acquire(tenant_id) as conn:
                async with conn.transaction():
                    # Check if the slot still exists and is available
                    slot = await conn.fetchrow("""
                        SELECT id, is_available FROM availability_slots
                        WHERE LOWER(service_type) LIKE $1 AND date = $2 AND time_slot = $3
                    """, f"%{service_type.lower()}%", parsed_date, booking_time)

                    if not slot:
                        return {"success": False, "error": f"No slot found for {service_type} on {booking_date} at {booking_time}"}
                    if not slot["is_available"]:
                        return {"success": False, "error": "Sorry, that slot has already been booked by someone else."}

                    # Mark slot as taken
                    await conn.execute(
                        "UPDATE availability_slots SET is_available = FALSE WHERE id = $1",
                        slot["id"]
                    )

                    # Create booking record
                    booking_id = await conn.fetchval("""
                        INSERT INTO bookings
                            (session_id, customer_name, customer_phone, service_type, booking_date, booking_time, status)
                        VALUES ($1, $2, $3, $4, $5, $6, 'confirmed')
                        RETURNING id;
                    """, session_id, customer_name, customer_phone, service_type, parsed_date, booking_time)

                    return {
                        "success": True,
                        "booking_id": booking_id,
                        "message": f"Booking confirmed! {service_type} on {booking_date} at {booking_time} for {customer_name}."
                    }
        except Exception as e:
            logger.error(f"[db] Error creating booking: {e}")
            return {"success": False, "error": f"Booking failed: {str(e)}"}

    async def get_bookings_for_phone(self, phone: str, tenant_id: Optional[str] = None) -> list:
        """Retrieve all active bookings for a given phone number."""
        if not self.pool:
            return []
        try:
            async with self.acquire(tenant_id) as conn:
                rows = await conn.fetch("""
                    SELECT customer_name, service_type, booking_date, booking_time, status
                    FROM bookings
                    WHERE customer_phone = $1 AND status = 'confirmed'
                    ORDER BY booking_date, booking_time;
                """, phone)
                result = []
                for r in rows:
                    result.append({
                        "name": r["customer_name"],
                        "service": r["service_type"],
                        "date": r["booking_date"].isoformat(),
                        "time": r["booking_time"],
                        "status": r["status"],
                    })
                return result
        except Exception as e:
            logger.error(f"[db] Error fetching bookings: {e}")
            return []

    # ─── Dashboard Log Methods ───────────────────────────────────────────

    # ─── Knowledge Base File Persistence ─────────────────────────────────

    async def save_kb_file(self, user_email: str, filename: str, category: str, size: int, chunk_count: int, agent_id: str = "restaurant_booking", tenant_id: Optional[str] = None) -> dict:
        """Persist a new knowledge base file metadata row in the database."""
        if not self.pool:
            return {}
        try:
            ext = filename.split(".")[-1].upper() if "." in filename else "PDF"
            async with self.acquire(tenant_id) as conn:
                row = await conn.fetchrow("""
                    INSERT INTO knowledge_files (user_email, name, category, agent_id, format, size, chunk_count, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, 'indexed')
                    RETURNING id, name, category, agent_id, format, size, chunk_count, status, uploaded_at;
                """, user_email, filename, category, agent_id, ext, size, chunk_count)
                if row:
                    return {
                        "id": str(row["id"]),
                        "name": row["name"],
                        "category": row["category"],
                        "agent_id": row["agent_id"],
                        "format": row["format"],
                        "size": str(row["size"]),
                        "chunkCount": row["chunk_count"],
                        "status": row["status"],
                        "uploadedAt": row["uploaded_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if row["uploaded_at"] else "",
                    }
        except Exception as e:
            logger.error(f"[db] Error saving KB file: {e}")
        return {}

    async def get_kb_files(self, user_email: str, tenant_id: Optional[str] = None) -> list:
        """Fetch all metadata files for a user's uploaded knowledge bases."""
        if not self.pool:
            return []
        try:
            async with self.acquire(tenant_id) as conn:
                rows = await conn.fetch("""
                    SELECT id, name, category, agent_id, format, size, chunk_count, status, uploaded_at
                    FROM knowledge_files
                    WHERE user_email = $1
                    ORDER BY uploaded_at DESC;
                """, user_email)
                return [
                    {
                        "id": str(r["id"]),
                        "name": r["name"],
                        "category": r["category"],
                        "agent_id": r["agent_id"],
                        "format": r["format"],
                        "size": str(r["size"]),
                        "chunkCount": r["chunk_count"],
                        "status": r["status"],
                        "uploadedAt": r["uploaded_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if r["uploaded_at"] else "",
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"[db] Error getting KB files: {e}")
            return []

    async def delete_kb_file(self, file_id: int, tenant_id: Optional[str] = None):
         """Delete a knowledge base file record by its DB primary key."""
         if not self.pool:
             return
         try:
             async with self.acquire(tenant_id) as conn:
                 await conn.execute("""
                     DELETE FROM knowledge_files
                     WHERE id = $1;
                 """, file_id)
         except Exception as e:
             logger.error(f"[db] Error deleting KB file: {e}")

    # ─── Missing Restaurant & Feedback Agent Log Helpers ─────────────────

    async def init_restaurant_booking_log(self, session_id: str, tenant_id: Optional[str] = None):
        """Initialize a restaurant booking log entry when call starts."""
        if not self.pool:
            return
        try:
            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    INSERT INTO restaurant_booking_logs (session_id)
                    VALUES ($1)
                    ON CONFLICT (session_id) DO NOTHING;
                """, session_id)
        except Exception as e:
            logger.error(f"[db] Error initializing restaurant booking log: {e}")

    async def init_feedback_agent_log(self, session_id: str, customer_name: str, tenant_id: Optional[str] = None):
        """Initialize a feedback agent log entry when call starts."""
        if not self.pool:
            return
        try:
            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    INSERT INTO feedback_agent_logs (session_id, customer_name)
                    VALUES ($1, $2)
                    ON CONFLICT (session_id) DO UPDATE
                    SET customer_name = EXCLUDED.customer_name;
                """, session_id, customer_name)
        except Exception as e:
            logger.error(f"[db] Error initializing feedback agent log: {e}")

    async def get_restaurant_availability(self, slot_date: str = None, party_size: int = None, meal_period: str = None) -> list:
        """Fetch available table slots for a date."""
        slots = await self.get_availability(slot_date=slot_date, service_type="Restaurant")
        if not slots:
            # Fallback mock slots for testing
            return ["7:00 PM (Booth)", "8:00 PM (Window)", "9:00 PM (Standard)"]
        return slots

    async def reserve_table(self, customer_name: str, phone_number: str, slot_date: str, slot_time: str, party_size: int, special_requests: str, session_id: str, tenant_id: Optional[str] = None) -> dict:
        """Confirm and log a table reservation in the database."""
        if not self.pool:
            return {"success": True, "message": "Reservation saved (offline mode)"}
        try:
            import re
            import dateutil.parser
            from datetime import datetime, date as dt_date
            
            # Clean and normalize parameters
            customer_name_str = str(customer_name).strip() if customer_name else "Guest"
            slot_time_str = re.sub(r'["\']', '', str(slot_time)).strip() if slot_time else "7:00 PM"
            special_requests_str = str(special_requests).strip() if special_requests else ""
            
            try:
                party_size_int = int(float(str(party_size)))
            except (ValueError, TypeError):
                party_size_int = 2
                
            dt = None
            if slot_date:
                clean_date = str(slot_date).replace('"', '').replace("'", "").strip()
                try:
                    dt = datetime.strptime(clean_date.split("T")[0], "%Y-%m-%d").date()
                except Exception:
                    try:
                        dt = dateutil.parser.parse(clean_date).date()
                    except Exception:
                        pass
            if not dt:
                dt = dt_date.today()

            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    INSERT INTO restaurant_reservations (session_id, customer_name, phone_number, slot_date, slot_time, party_size, special_requests)
                    VALUES ($1, $2, $3, $4, $5, $6, $7);
                """, session_id, customer_name_str, phone_number, dt, slot_time_str, party_size_int, special_requests_str)
                
                # Update booking log
                booking_details = f"Table for {party_size_int} on {dt.strftime('%Y-%m-%d')} at {slot_time_str}"
                await conn.execute("""
                    INSERT INTO restaurant_booking_logs (session_id, customer_name, booking_details, special_requests)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (session_id) DO UPDATE
                    SET customer_name = EXCLUDED.customer_name,
                        booking_details = EXCLUDED.booking_details,
                        special_requests = EXCLUDED.special_requests;
                """, session_id, customer_name_str, booking_details, special_requests_str)
                
            return {
                "success": True,
                "reservation_id": int(time.time()) % 10000,
                "table_type": f"Table for {party_size_int}",
                "message": f"Reservation confirmed! Table for {party_size_int} on {dt.strftime('%Y-%m-%d')} at {slot_time_str} for {customer_name_str}.",
            }
        except Exception as e:
            logger.error(f"[db] Error reserving table: {e}")
            return {"success": False, "error": str(e)}

    async def pre_order_food(self, customer_name: str, phone_number: str, items: str, total_amount: str, arrival_time: str, slot_date: str, session_id: str, tenant_id: Optional[str] = None) -> dict:
        """Log a pre-ordered meal request."""
        if not self.pool:
            return {"success": True, "message": "Pre-order saved (offline mode)"}
        try:
            import re
            import dateutil.parser
            from datetime import datetime, date as dt_date
            
            customer_name_str = str(customer_name).strip() if customer_name else "Guest"
            arrival_time_str = re.sub(r'["\']', '', str(arrival_time)).strip() if arrival_time else "7:00 PM"
            items_str = str(items).strip() if items else ""
            total_amount_str = str(total_amount).strip() if total_amount else ""
            
            dt = None
            if slot_date:
                clean_date = str(slot_date).replace('"', '').replace("'", "").strip()
                try:
                    dt = datetime.strptime(clean_date.split("T")[0], "%Y-%m-%d").date()
                except Exception:
                    try:
                        dt = dateutil.parser.parse(clean_date).date()
                    except Exception:
                        pass
            if not dt:
                dt = dt_date.today()

            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    INSERT INTO restaurant_pre_orders (session_id, customer_name, phone_number, items, total_amount, arrival_time, slot_date)
                    VALUES ($1, $2, $3, $4, $5, $6, $7);
                """, session_id, customer_name_str, phone_number, items_str, total_amount_str, arrival_time_str, dt)
                
                # Update booking log
                booking_details = f"Pre-order: {items_str} (Total: {total_amount_str})"
                await conn.execute("""
                    INSERT INTO restaurant_booking_logs (session_id, customer_name, booking_details)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (session_id) DO UPDATE
                    SET customer_name = EXCLUDED.customer_name,
                        booking_details = EXCLUDED.booking_details;
                """, session_id, customer_name_str, booking_details)
                
            return {
                "success": True,
                "message": f"Pre-order for {customer_name_str} placed. Items: {items_str}.",
            }
        except Exception as e:
            logger.error(f"[db] Error pre-ordering food: {e}")
            return {"success": False, "error": str(e)}

    async def get_customer_bookings(self, phone_number: str, tenant_id: Optional[str] = None) -> list:
        """Fetch all reservation history for a phone number."""
        if not self.pool:
            return []
        try:
            clean_phone = phone_number.lstrip("0").lstrip("+91")
            async with self.acquire(tenant_id) as conn:
                rows = await conn.fetch("""
                    SELECT slot_date, slot_time, party_size
                    FROM restaurant_reservations
                    WHERE phone_number LIKE $1
                    ORDER BY slot_date DESC, slot_time DESC;
                """, f"%{clean_phone}")
                return [
                    {
                        "date": str(r["slot_date"]),
                        "time": r["slot_time"],
                        "party_size": r["party_size"],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"[db] Error getting customer bookings: {e}")
            return []

    async def update_feedback_agent_log(self, session_id: str, product_review: str, satisfaction_level: str, overall_experience: str, call_summary: str, escalation_required: bool, tenant_id: Optional[str] = None):
        """Update a feedback log entry with ratings, objection details or lead qualification stages."""
        if not self.pool:
            return
        try:
            async with self.acquire(tenant_id) as conn:
                sentiment = "positive" if satisfaction_level in ("positive", "satisfied", "high") else "negative" if satisfaction_level in ("negative", "unsatisfied", "low") else "neutral"
                await conn.execute("""
                    INSERT INTO feedback_agent_logs (session_id, product_review, satisfaction_level, overall_experience, sentiment, escalation_required, call_summary)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (session_id) DO UPDATE
                    SET product_review = EXCLUDED.product_review,
                        satisfaction_level = EXCLUDED.satisfaction_level,
                        overall_experience = EXCLUDED.overall_experience,
                        sentiment = EXCLUDED.sentiment,
                        escalation_required = EXCLUDED.escalation_required,
                        call_summary = EXCLUDED.call_summary;
                """, session_id, product_review, satisfaction_level, overall_experience, sentiment, escalation_required, call_summary)
        except Exception as e:
            logger.error(f"[db] Error updating feedback agent log: {e}")

    async def get_feedback_agent_logs(self, limit: int = 50, tenant_id: Optional[str] = None) -> list:
        """Fetch feedback/lead qualification logs for the dashboard."""
        if not self.pool:
            return []
        try:
            async with self.acquire(tenant_id) as conn:
                rows = await conn.fetch("""
                    SELECT session_id, customer_name, call_datetime, product_review, satisfaction_level, sentiment, escalation_required, call_summary
                    FROM feedback_agent_logs
                    ORDER BY call_datetime DESC
                    LIMIT $1;
                """, limit)
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[db] Error getting feedback agent logs: {e}")
            return []

    async def get_restaurant_booking_logs(self, limit: int = 50, tenant_id: Optional[str] = None) -> list:
        """Fetch restaurant booking logs for the dashboard."""
        if not self.pool:
            return []
        try:
            async with self.acquire(tenant_id) as conn:
                rows = await conn.fetch("""
                    SELECT session_id, customer_name, call_datetime, booking_details, special_requests
                    FROM restaurant_booking_logs
                    ORDER BY call_datetime DESC
                    LIMIT $1;
                """, limit)
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[db] Error getting restaurant booking logs: {e}")
            return []

    async def get_reminder_contacts(self, user_email: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch reminder contacts for a user email."""
        if not self.pool:
            return []
        try:
            import json
            async with self.acquire(tenant_id) as conn:
                rows = await conn.fetch("""
                    SELECT id, name, phone, location, priority, tags, notes, domain, status, scheduled_at, attributes, call_history, attempt_number, total_attempts
                    FROM reminder_contacts
                    WHERE user_email = $1
                    ORDER BY id ASC;
                """, user_email)
                contacts = []
                for row in rows:
                    attrs = row['attributes']
                    if isinstance(attrs, str):
                        attrs = json.loads(attrs)
                    history = row['call_history']
                    if isinstance(history, str):
                        history = json.loads(history)
                    contacts.append({
                        "id": str(row['id']),
                        "name": row['name'],
                        "phone": row['phone'],
                        "location": row['location'] or "",
                        "priority": row['priority'] or "Normal",
                        "tags": row['tags'] or [],
                        "notes": row['notes'] or "",
                        "domain": row['domain'] or "restaurant",
                        "status": row['status'] or "pending",
                        "scheduledAt": row['scheduled_at'],
                        "attributes": attrs or {},
                        "callHistory": history or [],
                        "attemptNumber": row['attempt_number'] or 0,
                        "totalAttempts": row['total_attempts'] or 3,
                    })
                return contacts
        except Exception as e:
            logger.error(f"[db] Error getting reminder contacts: {e}")
            return []

    async def add_reminder_contact(self, user_email: str, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Add a single reminder contact to the database."""
        if not self.pool:
            return {}
        try:
            import json
            tags = data.get("tags") or []
            if not isinstance(tags, list):
                tags = [tags]
            attributes_str = json.dumps(data.get("attributes") or {})
            call_history_str = json.dumps(data.get("callHistory") or [])
            async with self.acquire(tenant_id) as conn:
                row = await conn.fetchrow("""
                    INSERT INTO reminder_contacts (
                        user_email, name, phone, location, priority, tags, notes, domain, status, scheduled_at, attributes, call_history, attempt_number, total_attempts
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    RETURNING id, name, phone, location, priority, tags, notes, domain, status, scheduled_at, attributes, call_history, attempt_number, total_attempts;
                """, 
                    user_email, 
                    data.get("name", ""), 
                    data.get("phone", ""), 
                    data.get("location", ""), 
                    data.get("priority", "Normal"), 
                    tags, 
                    data.get("notes", ""), 
                    data.get("domain", "restaurant"), 
                    data.get("status", "pending"), 
                    data.get("scheduledAt"), 
                    attributes_str, 
                    call_history_str, 
                    data.get("attemptNumber", 0), 
                    data.get("totalAttempts", 3)
                )
                if row:
                    attrs = row['attributes']
                    if isinstance(attrs, str):
                        attrs = json.loads(attrs)
                    history = row['call_history']
                    if isinstance(history, str):
                        history = json.loads(history)
                    return {
                        "id": str(row['id']),
                        "name": row['name'],
                        "phone": row['phone'],
                        "location": row['location'] or "",
                        "priority": row['priority'] or "Normal",
                        "tags": row['tags'] or [],
                        "notes": row['notes'] or "",
                        "domain": row['domain'] or "restaurant",
                        "status": row['status'] or "pending",
                        "scheduledAt": row['scheduled_at'],
                        "attributes": attrs or {},
                        "callHistory": history or [],
                        "attemptNumber": row['attempt_number'] or 0,
                        "totalAttempts": row['total_attempts'] or 3,
                    }
            return {}
        except Exception as e:
            logger.error(f"[db] Error adding reminder contact: {e}")
            return {}

    async def update_reminder_contact_status(self, contact_id: int, status: str, tenant_id: Optional[str] = None):
        """Update status of a reminder contact."""
        if not self.pool:
            return
        try:
            async with self.acquire(tenant_id) as conn:
                await conn.execute("""
                    UPDATE reminder_contacts
                    SET status = $1
                    WHERE id = $2;
                """, status, contact_id)
        except Exception as e:
            logger.error(f"[db] Error updating reminder status: {e}")

    async def add_reminder_call_history(self, contact_id: int, call_session_id: str, summary: str, duration: str = "1m", outcome: str = "Completed", tenant_id: Optional[str] = None):
        """Append a call history item to the reminder contact."""
        if not self.pool:
            return
        try:
            import json
            import time
            from datetime import datetime
            async with self.acquire(tenant_id) as conn:
                row = await conn.fetchrow("SELECT call_history FROM reminder_contacts WHERE id = $1", contact_id)
                if not row:
                    return
                history = row['call_history']
                if isinstance(history, str):
                    history = json.loads(history)
                if not history:
                    history = []
                    
                called_at_str = datetime.now().strftime("%B %d, %Y, %I:%M %p")
                new_item = {
                    "id": f"h-{int(time.time())}",
                    "sessionId": call_session_id,
                    "calledAt": called_at_str,
                    "duration": duration,
                    "outcome": outcome,
                    "summary": summary
                }
                history.append(new_item)
                
                await conn.execute("""
                    UPDATE reminder_contacts
                    SET call_history = $1, attempt_number = attempt_number + 1
                    WHERE id = $2;
                """, json.dumps(history), contact_id)
        except Exception as e:
            logger.error(f"[db] Error adding reminder call history: {e}")

    async def bulk_import_reminder_contacts(self, user_email: str, contacts: List[Dict[str, Any]], domain: str) -> List[Dict[str, Any]]:
        """Bulk import reminder contacts."""
        if not self.pool:
            return []
        try:
            imported = []
            for c in contacts:
                c_data = {**c, "domain": domain, "status": "pending"}
                res = await self.add_reminder_contact(user_email, c_data)
                if res:
                    imported.append(res)
            return imported
        except Exception as e:
            logger.error(f"[db] Error bulk importing reminder contacts: {e}")
            return []


