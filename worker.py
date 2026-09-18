"""Cloudflare Python Worker entrypoint for sing-box-subscribe.

The upstream application is intentionally left unchanged.  We patch only the
operations that do not map directly to the Workers runtime, then expose the
original Flask app through Cloudflare's WSGI bridge.
"""

from __future__ import annotations

import base64
import os
import subprocess
import traceback
import zlib
from pathlib import Path

from cloudflare_adapter import (
    decode_state_cookie_parts,
    install_runtime_patches,
    should_load_editor_state,
    run_upstream_main,
)

# Patch before importing api.app so its module-level ``subprocess`` reference
# points to this same module object.
_original_check_call = subprocess.check_call
_main_module = None


def _worker_check_call(command, *args, **kwargs):
    command_list = [str(part) for part in command]
    if any(Path(part).name == "main.py" for part in command_list):
        try:
            return run_upstream_main(_main_module, command_list)
        except Exception as exc:
            traceback.print_exc()
            raise subprocess.CalledProcessError(1, command_list) from exc
    raise RuntimeError(
        "Cloudflare Workers cannot spawn child processes; only the upstream "
        "main.py invocation is supported by the compatibility adapter."
    )


subprocess.check_call = _worker_check_call

# Import upstream Flask app first. main.py imports TEMP_DIR back from this
# module, so this ordering avoids changing the upstream circular dependency.
import api.app as _app_module  # noqa: E402
from api.app import app  # noqa: E402
import main as _main_module  # noqa: E402

# Static imports guarantee that pywrangler includes every protocol parser even
# though upstream discovers them dynamically with importlib/os.walk.
import parsers.anytls as parser_anytls  # noqa: E402
import parsers.http as parser_http  # noqa: E402
import parsers.https as parser_https  # noqa: E402
import parsers.hysteria as parser_hysteria  # noqa: E402
import parsers.hysteria2 as parser_hysteria2  # noqa: E402
import parsers.socks as parser_socks  # noqa: E402
import parsers.ss as parser_ss  # noqa: E402
import parsers.ssr as parser_ssr  # noqa: E402
import parsers.trojan as parser_trojan  # noqa: E402
import parsers.tuic as parser_tuic  # noqa: E402
import parsers.vless as parser_vless  # noqa: E402
import parsers.vmess as parser_vmess  # noqa: E402
import parsers.wg as parser_wg  # noqa: E402

install_runtime_patches(
    _main_module,
    {
        "anytls": parser_anytls,
        "http": parser_http,
        "https": parser_https,
        "hysteria": parser_hysteria,
        "hysteria2": parser_hysteria2,
        "socks": parser_socks,
        "ss": parser_ss,
        "ssr": parser_ssr,
        "trojan": parser_trojan,
        "tuic": parser_tuic,
        "vless": parser_vless,
        "vmess": parser_vmess,
        "wg": parser_wg,
    },
    app_module=_app_module,
)

@app.get("/healthz")
def _healthz():
    return {"status": "ok", "runtime": "cloudflare-python-workers"}

# The upstream Web UI stores TEMP_JSON_DATA in process memory. Workers may send
# sequential requests to different isolates, so persist that editor state in
# compressed, HttpOnly cookies. This keeps the original UI flow without KV.
from flask import request as flask_request  # noqa: E402

_COOKIE_COUNT = "sbs_temp_n"
_COOKIE_PREFIX = "sbs_temp_"
_COOKIE_CHUNK = 3000
_COOKIE_MAX_CHUNKS = 8
_COOKIE_MAX_DECOMPRESSED = 256 * 1024
_DEFAULT_TEMP_JSON_DATA = os.environ.get("TEMP_JSON_DATA", "{}")


def _load_temp_json_cookie():
    # Never inherit editor state from a previous request in the same isolate.
    os.environ["TEMP_JSON_DATA"] = _DEFAULT_TEMP_JSON_DATA

    # /config/<url> is intentionally stateless and must not inherit Web UI
    # editor settings such as a custom save_config_path.
    if not should_load_editor_state(flask_request.path):
        return

    try:
        count = int(flask_request.cookies.get(_COOKIE_COUNT, "0"))
    except ValueError:
        return
    if count < 1 or count > _COOKIE_MAX_CHUNKS:
        return

    parts = [
        flask_request.cookies.get(f"{_COOKIE_PREFIX}{i}", "")
        for i in range(count)
    ]
    value = decode_state_cookie_parts(
        parts,
        chunk_size=_COOKIE_CHUNK,
        max_chunks=_COOKIE_MAX_CHUNKS,
        max_output_size=_COOKIE_MAX_DECOMPRESSED,
    )
    if value is not None:
        os.environ["TEMP_JSON_DATA"] = value


def _save_temp_json_cookie(response):
    if flask_request.path not in ("/edit_temp_json", "/clear_temp_json_data"):
        return response

    for i in range(_COOKIE_MAX_CHUNKS):
        response.delete_cookie(f"{_COOKIE_PREFIX}{i}", path="/")
    response.delete_cookie(_COOKIE_COUNT, path="/")

    value = os.environ.get("TEMP_JSON_DATA", "{}")
    encoded = base64.urlsafe_b64encode(zlib.compress(value.encode("utf-8"), 9)).decode("ascii")
    chunks = [encoded[i : i + _COOKIE_CHUNK] for i in range(0, len(encoded), _COOKIE_CHUNK)]
    if not chunks or len(chunks) > _COOKIE_MAX_CHUNKS:
        return response

    cookie_args = {
        "path": "/",
        "httponly": True,
        "samesite": "Lax",
        "secure": flask_request.is_secure,
        "max_age": 7 * 24 * 60 * 60,
    }
    response.set_cookie(_COOKIE_COUNT, str(len(chunks)), **cookie_args)
    for i, chunk in enumerate(chunks):
        response.set_cookie(f"{_COOKIE_PREFIX}{i}", chunk, **cookie_args)
    return response


app.before_request(_load_temp_json_cookie)
app.after_request(_save_temp_json_cookie)

from workers import WorkerEntrypoint, wsgi  # noqa: E402


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        for key in ("RUA", "STR"):
            value = getattr(self.env, key, None)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        return await wsgi.fetch(app, request, self.env)
