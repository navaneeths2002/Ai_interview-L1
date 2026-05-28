import re

with open('app/static/interview.html', encoding='utf-8') as f:
    content = f.read()

# Extract all IDs declared in HTML elements
html_ids = set(re.findall(r'id=["\']([^"\']+)["\']', content))

# Extract all getElementById calls from JS
js_gets = set(re.findall(r'getElementById\(["\']([^"\']+)["\']\)', content))

missing = js_gets - html_ids
print('IDs used in getElementById but NOT in HTML:')
if missing:
    for m in sorted(missing):
        print(f'  MISSING -> {m}')
else:
    print('  None - all IDs present in HTML')

print(f'\nTotal HTML IDs: {len(html_ids)}  |  Total getElementById calls: {len(js_gets)}')
