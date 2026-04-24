import sys

path = r'c:\Users\LeoMonteiro\Documents\GitHub\temperature_esp32\server.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
skip = False
for line in lines:
    if '    <html>' in line and not skip:
        skip = True
        continue
    if '    return html' in line and skip:
        # Check next line for closing triple quotes
        continue
    if '    """' in line and skip:
        skip = False
        continue
    if not skip:
        new_lines.append(line)

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
