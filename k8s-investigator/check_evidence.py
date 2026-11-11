import json, sys
run = json.load(open(sys.argv[1]))
msgs = run["messages"] if isinstance(run, dict) else run
# tool_use_id -> tool name; tool_use_id -> result text
names, results = {}, {}
for m in msgs:
    if not isinstance(m.get("content"), list): continue
    for b in m["content"]:
        if b.get("type") == "tool_use": names[b["id"]] = b["name"]
        if b.get("type") == "tool_result":
            c = b["content"]
            results[b["tool_use_id"]] = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c)
by_tool = {}
for i, t in results.items(): by_tool.setdefault(names[i], []).append(t)
final = [b["text"] for m in msgs if m["role"] == "assistant" and isinstance(m["content"], list)
         for b in m["content"] if b.get("type") == "text"][-1]
for item in json.loads(final):
    for ev in item["evidence"]:
        ok = any(ev["quote"] in t for t in by_tool.get(ev["tool"], []))
        print("PASS" if ok else "FAIL", item["pod"][:12], ev["tool"], repr(ev["quote"][:60]))