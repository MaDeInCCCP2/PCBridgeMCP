import json, pathlib, httpx
root=pathlib.Path(__file__).resolve().parent.parent
cfg=json.loads((root/"config.json").read_text(encoding="utf-8-sig"))
token=(root/"TOKEN.txt").read_text(encoding="utf-8-sig").strip()
print("token_matches_active_config:", token==cfg.get("secret"))
init={"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"pcbridge-diagnostics","version":"1.0"}},"id":1}
h={"accept":"application/json, text/event-stream","content-type":"application/json","mcp-protocol-version":"2025-06-18"}
for label,url,headers in [
 ("direct",f"http://127.0.0.1:{cfg.get('port',8765)}/mcp",{**h,"authorization":"Bearer "+token}),
 ("proxy","http://127.0.0.1:8770/"+token+"/mcp",h),
]:
 try:
  with httpx.Client(timeout=12.0,trust_env=False) as client:
   r=client.post(url,headers=headers,json=init)
   print(label, "status:", r.status_code, "content-type:",r.headers.get("content-type",""), "body_class:", "initialize" if '"result"' in r.text else "error" if '"error"' in r.text else "other")
 except Exception as exc:
  print(label, "exception_type:",type(exc).__name__,"exception_kind:", str(exc)[:150].replace(token,"[redacted]"))
