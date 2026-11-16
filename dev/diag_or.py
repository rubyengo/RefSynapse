"""One-off: check an OpenRouter key + model from the console. Key is NOT saved."""
import getpass
import json
import urllib.error
import urllib.request

key = getpass.getpass("Paste OpenRouter key (input is hidden): ").strip()
if not key:
    print("ERROR: empty key")
    raise SystemExit(1)

request = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=json.dumps({
        "model": "deepseek/deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 1000,
    }).encode("utf-8"),
    headers={"Authorization": "Bearer " + key,
             "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8", "ignore"))
        print("HTTP", response.status)
    print("model:", data.get("model"))
    choice = (data.get("choices") or [{}])[0]
    print("reply:", (choice.get("message") or {}).get("content") or "(empty)")
    print("usage:", data.get("usage"))
except urllib.error.HTTPError as ex:
    print("HTTP", ex.code)
    print(ex.read().decode("utf-8", "ignore")[:500])
except Exception as ex:
    print("ERROR:", ex)
print("DONE")