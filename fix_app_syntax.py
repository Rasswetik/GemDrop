from pathlib import Path

path = Path(__file__).with_name("app.py")
text = path.read_text(encoding="utf-8")

bad_variants = [
    "        base = re.sub(r'\\s*\\((?:Onyx Black|Onyx|Black)\\)\\s*\n        if re.sub(r'[^a-z0-9]+', '', base.casefold()) != norm:\n",
    "        base = re.sub(r'\\s*\\((?:Onyx Black|Onyx|Black)\\)\\s*        if re.sub(r'[^a-z0-9]+', '', base.casefold()) != norm:\n",
]
fixed = (
    "        base = re.sub(r'\\s*\\((?:Onyx Black|Onyx|Black)\\)\\s*$', '', base, flags=re.I).strip()\n"
    "        if re.sub(r'[^a-z0-9]+', '', base.casefold()) != norm:\n"
)

changed = False
for bad in bad_variants:
    if bad in text:
        text = text.replace(bad, fixed, 1)
        changed = True
        break

if not changed and fixed not in text:
    raise SystemExit("Relayr regex syntax marker not found; refusing to mutate app.py")

path.write_text(text, encoding="utf-8")
print("app.py Relayr regex syntax is valid")
