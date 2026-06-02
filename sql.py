import sqlite3

conn = sqlite3.connect("storage/store_intelligence.db")

rows = conn.execute("""
SELECT event_type, COUNT(*)
FROM events
GROUP BY event_type
""").fetchall()

print(rows)

conn.close()