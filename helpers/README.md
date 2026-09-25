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

## Additional functionality in `send_emails_guess4.py

```
python send_emails.py --infer-patterns                   # dry run: review the [guess] list carefully
python send_emails.py --test-to you@yourdomain.com       # check formatting
python send_emails.py --infer-patterns --send --limit 20 # small first batch, then watch your inbox for bounces
```

## Additional fucntionality in `send_emails_mailsfinder.py

```
python send_emails.py --find-test John Park weversecompany.com     # look at the raw response + your dashboard credits
python send_emails.py --verify-test your.own.address@yourdomain.com   # should say VALID
python send_emails.py --verify-only                                 # look up blanks, send nothing, review results
python send_emails.py --test-to you@yourdomain.com
python send_emails.py --send --limit 20
```