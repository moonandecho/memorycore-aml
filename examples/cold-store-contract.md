# Cold Store Contract

MemoryCore treats the cold tier as an opaque MCP service. To be usable as the
cold tier, a service must expose these five MCP tools over
`streamable-http` (or stdio proxied by your MCP client):

## Required tools

| Tool | Request params | Response (JSON) | Notes |
|---|---|---|---|
| `remember` | `content: str`, `importance: float`, `scope: str` | `{"status": "stored", "memory_id": "..."}` | Create a memory. `status` must be `"stored"` on success. |
| `recall` | `query: str`, `top_k: int` | `{"results": [{"id": "...", "content": "...", "score": 0.0-1.0}]}` | Semantic recall. `results` may be empty. |
| `update` | `memory_id: str`, `content: str` | `{"status": "updated"}` | Merge-update an existing memory. |
| `forget` | `memory_id: str` | `{"status": "forgotten"}` | Delete a memory. |
| `stats` | *(none)* | `{"total": N, "embeddings": N}` | Used by maintenance to verify embedding integrity. |

## Contract rules

1. **Idempotency** — repeated `remember` with identical content should return a
   new or existing `memory_id`; MemoryCore dedups against `recall` before writing.
2. **Honest failures** — if the service cannot persist, it must return an error
   result (or raise an MCP error), not a fake `stored`. MemoryCore's safety
   ordering depends on this: it only deletes local entries after a confirmed
   remote write.
3. **Session handling** — the server may issue a fixed `mcp-session-id`;
   MemoryCore's client reconnects on 4xx responses.
4. **Vector integrity** — if the service provides embeddings, `stats()` should
   report `total` and `embeddings` counts; maintenance flags mismatches.

## Example: minimal HTTP service skeleton

A tiny reference service that satisfies the contract (stdlib only):

```python
# cold_store.py — minimal in-memory MCP memory service
import json
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("cold-store")
db = {}          # memory_id -> content
id_counter = 0

@mcp.tool()
def remember(content: str, importance: float = 0.5, scope: str = "global") -> str:
    global id_counter
    id_counter += 1
    db[str(id_counter)] = content
    return json.dumps({"status": "stored", "memory_id": str(id_counter)}, ensure_ascii=False)

@mcp.tool()
def recall(query: str, top_k: int = 3) -> str:
    # production: semantic search; here: naive substring match
    hits = [(i, c) for i, c in db.items() if query in c][:top_k]
    return json.dumps({"results": [{"id": i, "content": c, "score": 1.0} for i, c in hits]}, ensure_ascii=False)

@mcp.tool()
def update(memory_id: str, content: str) -> str:
    db[memory_id] = content
    return json.dumps({"status": "updated"})

@mcp.tool()
def forget(memory_id: str) -> str:
    db.pop(memory_id, None)
    return json.dumps({"status": "forgotten"})

@mcp.tool()
def stats() -> str:
    return json.dumps({"total": len(db), "embeddings": len(db)})

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Point MemoryCore at it:

```bash
export MNEMOSYNE_URL="http://localhost:9000/mcp"   # or run the above via stdio
python -m memorycore.server
```

> Note: the naive `recall` above is for illustration only. Production cold
> stores should implement real semantic retrieval (embeddings) so that
> `recall` quality drives the overflow dedup/merge decisions.
