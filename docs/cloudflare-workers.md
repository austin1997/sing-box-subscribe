# Cloudflare Workers deployment

This port adds Cloudflare Python Workers support without removing or replacing the upstream Vercel, Docker, CLI, web UI, subscription parsing, filtering, template, GitHub-proxy, or Only-nodes paths.

## What changes on Workers

The upstream Flask app calls `main.py` through `subprocess.check_call()`. Cloudflare Workers cannot create child processes. `worker.py` intercepts only that invocation and executes the same upstream orchestration in-process. All protocol parsers remain the upstream parser modules.

The Worker also statically imports every parser so pywrangler can bundle modules that upstream normally discovers with `os.walk()` and `importlib`.

Generated config files are written to `/tmp`. Python Workers provide an ephemeral in-memory filesystem, which is sufficient because the upstream Flask route writes the file and reads it back in the same request.

The upstream web editor stores `TEMP_JSON_DATA` in process memory. Since a Worker request can land on another isolate, this port mirrors the editor state into compressed, chunked, HttpOnly cookies. That keeps the existing edit -> generate workflow stateless and avoids requiring KV or Durable Objects. The `/config/<url>` endpoint remains fully stateless.

## Deploy

Prerequisites: Cloudflare account, Node.js, and `uv`.

```bash
uv sync
uv run pywrangler dev
```

Test the UI at the local URL printed by pywrangler. Test the subscription endpoint, for example:

```bash
curl 'http://127.0.0.1:8787/config/https%3A%2F%2Fexample.com%2Fsubscription'
```

Deploy:

```bash
uv run pywrangler deploy
```

The existing `RUA` and `STR` controls are supported as Cloudflare Worker variables/secrets. The Worker entrypoint mirrors those bindings into `os.environ` before dispatching the Flask request, so the unchanged upstream filtering logic continues to work.

## Compatibility notes

- Existing Vercel/Docker/CLI entrypoints are unchanged.
- The web UI and `/config/<url>` API are unchanged.
- VLESS, VMess, Shadowsocks, SSR, Trojan, TUIC, Hysteria/Hysteria2, AnyTLS, WireGuard, HTTP/HTTPS and SOCKS parsers remain upstream implementations.
- Remote config templates continue to use `requests`.
- Local/bundled templates continue to use `config_template/*.json`.
- `ConfigSSH` in `tool.py` remains available for normal Python/Docker execution, but arbitrary SSH/TCP process-style workflows are not a Cloudflare Workers capability and are not used by the subscription-service path.
- Filesystem writes are ephemeral on Workers; generated configs are returned within the same request.
- The cookie-backed editor state supports up to roughly 24 KB after compression/base64 chunking. For unusually large editor payloads, prefer the stateless `/config/<url>` interface.

## Free plan warning

The full converter can parse YAML, decode subscriptions, apply regex filters and generate large JSON responses. CPU quotas on the Free plan are substantially tighter than on the Paid plan, so larger subscriptions may exceed the free per-request CPU allowance.

## Why this is an adapter instead of a rewrite

A JavaScript rewrite would duplicate every protocol parser and invite behavior drift. The adapter reuses upstream code, which makes future upstream merges much easier.
