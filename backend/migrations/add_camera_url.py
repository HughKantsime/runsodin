"""Add camera_url to printers table"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'printfarm.db')

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(printers)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if 'camera_url' not in columns:
        cursor.execute("ALTER TABLE printers ADD COLUMN camera_url TEXT")
        print("Added camera_url to printers table")
    else:
        print("camera_url already exists")
    
    conn.commit()
    conn.close()

if __name__ == '__main__':
    migrate()
