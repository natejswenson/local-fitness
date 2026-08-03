# 2026-08-03 — the auth gate was reading a string the attacker controlled

0.43.1. A security fix found during a five-agent audit of the MCP surface.
`_is_public_path` — the deny-by-default whitelist that has been the front door
of this app since the 2026-05-04 audit — was correct. It always was. The bug
was one line above it.

## The finding

```python
path = request.url.path                    # <- attacker-influenced
if API_TOKEN is None or _is_public_path(path):
    return await call_next(request)        # <- no token checked
```

Starlette does not hand you the path the router parsed. `request.url` is
*reconstructed*: `f"{scheme}://{host}{path}"`, where `host` is the `Host`
header, and then re-parsed. Put a `/` in the header and the path boundary
moves.

```
Host: fitness.home.local          ->  url.path = '/mcp/'    public=False  ✅
Host: fitness.home.local/health#  ->  url.path = '/health'  public=True   ❌
scope['path']                     ->  '/mcp/'  in both cases              ✅
```

That is GHSA-86qp-5c8j-p5mr, fixed in starlette 1.0.1. This repo was pinned at
1.0.0.

## The part worth remembering: what was containing it

The first thing I did after reproducing it was send the exploit at the real
app. It returned **421, not 200**. The MCP transport ships its own
DNS-rebinding guard, and it compares the `Host` against an allowlist by exact
string match. A poisoned Host contains a `/`. It can never match anything.

It would have been very easy to write that up as "mitigated, low severity" and
move on.

But that containment has nothing to do with authentication. It is a property
of the attack's *shape* — the payload needs a `/`, and the guard happens to
reject strings with a `/` in them. Nothing about it is a decision anyone made.
And the same guard has a second branch:

```python
if allowed.endswith(":*"):
    return host.startswith(base + ":")     # prefix match, not exact
```

`LOCAL_FITNESS_MCP_ALLOWED_HOSTS='fitness.home.local:*'` is a documented,
supported configuration. Under it, `Host: fitness.home.local:8765/health#`
satisfies `startswith`, the guard waves it through, the auth middleware has
already returned, and an unauthenticated `POST /mcp/` completes a full MCP
`initialize`. From there: `tools/list`, then every read tool over the whole
health corpus, then the write tools.

Same code. One env var away. The 421 was luck, and luck is not a control.

## The fix, in two layers

```python
def _request_path(request: Request) -> str:
    return request.scope["path"]
```

`scope["path"]` is set by the ASGI server from the request line. No header
touches it, and it is the exact string the router dispatches on — so the
security check and the routing decision cannot disagree, which is the actual
invariant that was broken.

The dependency got bumped too (starlette 1.0.0 → 1.3.1, which also clears a
HIGH form-limit DoS; `python-multipart` and `pydantic-settings` came along for
their own advisories). **But the bump is the weaker half.** It closes this
CVE. It does nothing about the next library that decides to rebuild a URL from
a header. Reading the router's own path is version-independent, and it is the
part that will still be true in two years.

## A test that tested the CVE instead of the fix

The first draft of the unit test opened with a precondition I was rather
pleased with:

```python
# Precondition: this starlette really is poisonable, so the assertion
# below is testing the fix rather than a library that no longer bites.
assert request.url.path == "/health"
```

Then I bumped starlette and it failed:

```
E       AssertionError: assert '/mcp/' == '/health'
```

Which is the correct outcome and the wrong test. That assertion pinned *the
library's bug*. The moment the library is patched it stops describing
anything, and — worse — it would have passed for years while quietly asserting
nothing about our code. A test whose subject is a third party's defect has a
shelf life.

The replacement asserts only what must hold forever: across five hostile `Host`
forms, `_request_path` returns the scope path and `_is_public_path` says no.
That holds on 1.3.1, on a downgrade, on a transitive re-pin, and on whatever
the next variant of this class looks like.

## The regression test's failure mode is the proof

Reverting the middleware doesn't fail the end-to-end test with a clean
assertion. It fails like this:

```
RuntimeError: Task group is not initialized. Make sure to use run().
    at mcp/server/streamable_http_manager.py:160
```

That error comes from *inside the mounted MCP app*. Getting there at all means
the bearer middleware let the request through. The stack trace is the exploit.

## Also worth knowing

`_is_public_path` has a unit test — `test_is_public_path_deny_by_default` — and
it passed throughout. It was never the thing that was wrong. A pure function
tested in isolation tells you nothing about what its callers feed it, and this
is the second time in this repo's history that a correct component was
undermined by its input (the other: grading a run against the wrong day's
plan). Test the seam, not just the function.

The rate-limit middleware, which reads the same path, was fixed in the same
change. It is a no-op today (`RATE_LIMITED_PREFIXES = ()`) but that is a
one-line change away from mattering, and it should not carry the same bug into
the day someone re-enables it.
