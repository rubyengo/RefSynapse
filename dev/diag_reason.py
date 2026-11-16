"""One-off: which reasoning settings does our model accept? (public, no key)"""
import json
import urllib.request

MODEL = "deepseek/deepseek-v4.1-flash"
request = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                 headers={"User-Agent": "RefSynapse-check"})
with urllib.request.urlopen(request, timeout=60) as response:
    data = json.loads(response.read().decode("utf-8", "ignore"))

found = next((r for r in data.get("data", []) if r.get("id") == MODEL), None)
if found is None:
    print("model not found:", MODEL)
else:
    print("id:", found.get("id"))
    print("reasoning:", json.dumps(found.get("reasoning"), ensure_ascii=False))
    print("supports tools:", "tools" in (found.get("supported_parameters") or []))
    print("max completion:", (found.get("top_provider") or {}).get("max_completion_tokens"))
print("DONE")