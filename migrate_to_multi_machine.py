#!/usr/bin/env python3
"""Migration script to add multi-machine support to existing database."""

import sqlite3
import socket
import platform
from datetime import datetime
from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from src.utils.config import Config

def get_machine_id():
    """Generate a unique machine ID based on hostname."""
    hostname = socket.gethostname()
    return f"{hostname}_{platform.system()}".lower().replace(" ", "_")

def migrate_database():
    """Add machine_id columns to existing database and populate with default machine."""
    db_path = Config.DB_PATH
    print(f"Migrating database at: {db_path}")

    # Connect to database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get or create default machine (this server)
    machine_id = get_machine_id()
    machine_name = socket.gethostname()
    os_type = platform.system().lower()
    os_version = platform.release()
    now = datetime.utcnow().isoformat()

    print(f"\nDefault machine:")
    print(f"  ID: {machine_id}")
    print(f"  Name: {machine_name}")
    print(f"  OS: {os_type} {os_version}")

    # Preserve metadata if this machine has already been migrated/registered.
    cursor.execute("""
        INSERT INTO machines
        (machine_id, machine_name, os_type, os_version, first_seen, last_seen, is_active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(machine_id) DO NOTHING
    """, (machine_id, machine_name, os_type, os_version, now, now))

    print(f"\n✓ Created/updated default machine: {machine_id}")

    # Add machine_id column to tables if they don't exist and populate
    tables_to_migrate = [
        'sessions', 'launches', 'process_metrics', 'network_metrics',
        'filesystem_events', 'token_usage', 'transcripts', 'messages'
    ]

    for table in tables_to_migrate:
        # Check if table exists
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
        if not cursor.fetchone():
            print(f"  Skipping {table} (table doesn't exist)")
            continue

        # Check if machine_id column already exists
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [col[1] for col in cursor.fetchall()]

        if 'machine_id' not in columns:
            # Add machine_id column
            print(f"  Adding machine_id column to {table}...")
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN machine_id TEXT")

            # Update all existing records to use default machine
            cursor.execute(f"UPDATE {table} SET machine_id = ? WHERE machine_id IS NULL", (machine_id,))
            count = cursor.rowcount
            print(f"    ✓ Updated {count} records in {table}")
        else:
            # Update NULL machine_ids if any exist
            cursor.execute(f"UPDATE {table} SET machine_id = ? WHERE machine_id IS NULL", (machine_id,))
            count = cursor.rowcount
            if count > 0:
                print(f"  ✓ Updated {count} NULL machine_ids in {table}")
            else:
                print(f"  ✓ {table} already has machine_id")

    # Commit changes
    conn.commit()
    conn.close()

    print("\n✓ Migration completed successfully!")
    print(f"  All existing data now belongs to machine: {machine_id}")

if __name__ == "__main__":
    try:
        Config.ensure_directories()
        migrate_database()
    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
