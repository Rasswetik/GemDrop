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

main_line = "    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)"
main_pos = text.find(main_line)
if main_pos == -1:
    raise SystemExit("app.py main marker not found; refusing to truncate")

main_end = text.find("\n", main_pos)
if main_end == -1:
    main_end = len(text)
else:
    main_end += 1

trailing = text[main_end:]
if trailing.strip():
    text = text[:main_end]
    changed = True

if fixed not in text:
    raise SystemExit("Relayr regex syntax marker not fixed")

path.write_text(text, encoding="utf-8")
print("app.py syntax repair applied")
