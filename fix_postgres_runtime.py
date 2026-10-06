from pathlib import Path

path = Path(__file__).with_name("app.py")
text = path.read_text(encoding="utf-8")
changed = False

old_class = """    def commit(self):
        self.connection.commit()

    def close(self):
"""
new_class = """    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
"""
if old_class in text:
    text = text.replace(old_class, new_class, 1)
    changed = True
elif "    def rollback(self):\n        self.connection.rollback()\n" not in text:
    raise SystemExit("PostgreSQL commit/close marker not found")

old_turnover = """        db.execute(\"\"\"UPDATE users
                      SET turnover_cents=turnover_cents+?,
                          withdrawal_wager_progress=MIN(withdrawal_wager_required,withdrawal_wager_progress+?)
                      WHERE id=?\"\"\", (amount, amount, user_id))
"""
new_turnover = """        db.execute(\"\"\"UPDATE users
                      SET turnover_cents=turnover_cents+?,
                          withdrawal_wager_progress=CASE
                              WHEN withdrawal_wager_progress+? < withdrawal_wager_required
                              THEN withdrawal_wager_progress+?
                              ELSE withdrawal_wager_required
                          END
                      WHERE id=?\"\"\", (amount, amount, amount, user_id))
"""
if old_turnover in text:
    text = text.replace(old_turnover, new_turnover, 1)
    changed = True
elif "withdrawal_wager_progress=CASE" not in text:
    raise SystemExit("increase_turnover marker not found")

path.write_text(text, encoding="utf-8")
print("PostgreSQL runtime compatibility fixes applied" if changed else "PostgreSQL runtime compatibility already fixed")
