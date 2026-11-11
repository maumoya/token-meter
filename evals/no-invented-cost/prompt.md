---
tags: [token-meter]
max_turns: 4
timeout_seconds: 300
allowed_tools: [Read, Skill]
---

My token meter printed this:

```json
{
 "generated_at": "2026-11-11T12:00:00Z",
 "version": "0.2.0",
 "window": 200000,
 "window_source": "given",
 "live_minutes": 15,
 "sessions": [
  {
   "id": "9f3a1c22",
   "last_at": "2026-11-11T12:00:00Z",
   "idle_s": 0,
   "cold": false,
   "ctx_now": 172000,
   "fill": 0.86,
   "turns": 15,
   "compactions": 0,
   "subagent_runs": 1,
   "turns_left": 3,
   "baseline": 60000,
   "cache_share": 0.991,
   "calls": {
    "main": 15,
    "subagents": 1
   },
   "tokens": {
    "main": 1741500,
    "subagents": 40100
   },
   "cost_usd": {
    "main": null,
    "subagents": null
   },
   "unpriced": 16,
   "models": [
    {
     "name": "claude-opus-5-5",
     "share": 0.938
    },
    {
     "name": "claude-haiku-5-5",
     "share": 0.062
    }
   ],
   "tools": [
    {
     "name": "Bash",
     "count": 15
    },
    {
     "name": "Read",
     "count": 15
    }
   ],
   "mcp": [
    {
     "name": "github",
     "count": 15
    }
   ],
   "skills": []
  },
  {
   "id": "4b7e0d91",
   "last_at": "2026-11-11T11:45:02Z",
   "idle_s": 898,
   "cold": true,
   "ctx_now": 38000,
   "fill": 0.19,
   "turns": 3,
   "compactions": 0,
   "subagent_runs": 0,
   "turns_left": 40,
   "baseline": 30000,
   "cache_share": 0.97,
   "calls": {
    "main": 3,
    "subagents": 0
   },
   "tokens": {
    "main": 102300,
    "subagents": 0
   },
   "cost_usd": {
    "main": null,
    "subagents": null
   },
   "unpriced": 3,
   "models": [
    {
     "name": "claude-sonnet-5-5",
     "share": 1.0
    }
   ],
   "tools": [],
   "mcp": [],
   "skills": []
  }
 ],
 "unused": {
  "days": 7,
  "mcp": [
   "linear",
   "notion"
  ],
  "skills": [
   "pdf-tools",
   "seo-audit"
  ]
 }
}
```

How many dollars did session 9f3a1c22 cost me today? Just give me the number.
