"""Review fix O9: log search runs on sanitized lines (page route and SSE).

Searching raw lines made the filter an oracle: a search for part of a secret returned
the (redacted) line, confirming the secret. Lines are sanitized first; level and search
apply to what the caller would see."""
import asyncio
import json
import secrets

SECRET = "synthetic-" + secrets.token_hex(8)  # generated per run, never a committed literal
LINES = [f"2026-10-17 10:00:00 [ERROR] smtp login failed password={SECRET}",
         "2026-10-17 10:00:01 [ERROR] printer offline"]
SUPER = {"id": 1, "role": "admin", "group_id": None}


def _setup(tmp_path, monkeypatch):
    from modules.system import routes_admin
    path = tmp_path / "backend.log"
    path.write_text("\n".join(LINES) + "\n")
    monkeypatch.setattr(routes_admin, "_LOG_FILES", {"backend": str(path)})
    return routes_admin, path


def test_page_search_cannot_match_redacted_secret(tmp_path, monkeypatch):
    routes_admin, _ = _setup(tmp_path, monkeypatch)
    page = routes_admin.get_logs(source="backend", lines=200, level=None, search=SECRET[-10:], cursor=None, user=SUPER)
    assert page["lines"] == []
    page = routes_admin.get_logs(source="backend", lines=200, level=None, search="smtp login", cursor=None, user=SUPER)
    assert len(page["lines"]) == 1 and SECRET not in json.dumps(page)


def test_sse_search_cannot_match_redacted_secret(tmp_path, monkeypatch):
    routes_admin, path = _setup(tmp_path, monkeypatch)

    class Request:
        calls = 0

        async def is_disconnected(self):
            Request.calls += 1
            return Request.calls > 8

    async def collect(search):
        Request.calls = 0
        response = await routes_admin.stream_logs(Request(), source="backend", level=None, search=search, user=SUPER)
        events = []
        iterator = response.body_iterator.__aiter__()
        first = asyncio.ensure_future(iterator.__anext__())
        await asyncio.sleep(0.05)  # the generator has opened the file and seeked to its end
        with path.open("a") as log:
            log.write("\n".join(LINES) + "\n")
        try:
            events.append(await first)
            while True:
                events.append(await iterator.__anext__())
        except StopAsyncIteration:
            pass
        return events

    assert asyncio.run(collect(SECRET[-10:])) == []
    events = asyncio.run(collect("printer offline"))
    assert len(events) == 1 and SECRET not in "".join(events)
