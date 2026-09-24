# `send_emails.py` Setup

1. Edit `SENDER_EMAIL` at the top. If your file really uses | between columns, set CSV_DELIMITER = "|". The first row must be the header with the column names you listed.
2. Preview the emails. Nothing is sent:
```
python send_emails.py
```
3. Send the first 3 rows to yourself and check how they look in Gmail or Outlook:
```
python send_emails.py --test-to you@yourdomain.com
```
4. Send for real. Start with a small batch:
```
python send_emails.py --send --limit 20
python send_emails.py --send
```