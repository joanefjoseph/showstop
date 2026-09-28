import sqlite3

con = sqlite3.connect('all_people.db')
cur = con.execute(
    "DELETE FROM employees WHERE company_name = ?",
    ('WeVerse',))
print(cur.rowcount, 'row(s) deleted')
con.commit()
con.close()

# rows = con.execute('SELECT linkedin_profile_url FROM employees').fetchall()
# [print(r[0]) for r in rows]
# con.close()
