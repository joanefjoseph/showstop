import sqlite3

con = sqlite3.connect('all_people.db')
# cur = con.execute(
#     "DELETE FROM employees WHERE linkedin_profile_url = ?",
#     ('https://www.linkedin.com/in/acoaacrmileba_nxa2he52mpvyp3zakjidmfyiu/',))
# print(cur.rowcount, 'row(s) deleted')
# con.commit()
# con.close()

rows = con.execute('SELECT linkedin_profile_url FROM employees').fetchall()
[print(r[0]) for r in rows]
con.close()
