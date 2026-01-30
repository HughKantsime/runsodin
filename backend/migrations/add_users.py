"""
Migration: Add users table for authentication
Run: python3 migrations/add_users.py
"""
import sqlite3

DB_PATH = '/opt/printfarm-scheduler/backend/printfarm.db'

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'operator',
            is_active BOOLEAN DEFAULT TRUE,
            last_login DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cur.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
    
    conn.commit()
    conn.close()
    print("Created users table")

def create_admin(username, email, password):
    """Create initial admin user."""
    import sys
    sys.path.insert(0, '/opt/printfarm-scheduler/backend')
    from auth import hash_password
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    password_hash = hash_password(password)
    
    try:
        cur.execute('''
            INSERT INTO users (username, email, password_hash, role)
            VALUES (?, ?, ?, 'admin')
        ''', (username, email, password_hash))
        conn.commit()
        print(f"Created admin user: {username}")
    except sqlite3.IntegrityError:
        print(f"User {username} already exists")
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
    # Uncomment and modify to create initial admin:
    # create_admin("admin", "admin@company.com", "changeme123")
