"""Cloudflare Workers adapter for Toperlock/sing-box-subscribe.

This module keeps the upstream converter untouched.  The original Flask app
launches ``main.py`` with ``subprocess.check_call``; Workers cannot spawn child
processes, so the Worker entrypoint redirects that single subprocess call here
and runs the exact same orchestration in-process.
"""

from __future__ import annotations

import base64
import json
import tempfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

BASE_DIR = Path(__file__).resolve().parent


def _arg(argv: Sequence[str], name: str, default: str = "") -> str:
    try:
        index = argv.index(name)
    except ValueError:
        return default
    if index + 1 >= len(argv):
        return default
    return str(argv[index + 1])


def decode_temp_json_arg(raw: str) -> dict[str, Any]:
    """Decode the double-encoded JSON used by upstream api/app.py."""
    if not raw:
        return {}
    value: Any = json.loads(raw)
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("--temp_json_data must decode to a JSON object")
    return value


def decode_state_cookie_parts(
    parts: Sequence[str],
    *,
    chunk_size: int,
    max_chunks: int,
    max_output_size: int,
) -> str | None:
    """Safely decode compressed editor state from bounded cookie chunks."""
    if not parts or len(parts) > max_chunks:
        return None
    if any(not part or len(part) > chunk_size for part in parts):
        return None

    encoded = "".join(parts)
    if len(encoded) > chunk_size * max_chunks:
        return None

    try:
        compressed = base64.b64decode(
            encoded.encode("ascii"), altchars=b"-_", validate=True
        )
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, max_output_size + 1)

        if len(raw) > max_output_size:
            return None
        if decompressor.unconsumed_tail or not decompressor.eof:
            return None
        if decompressor.unused_data:
            return None

        value = raw.decode("utf-8")
        parsed = json.loads(value)
        return value if isinstance(parsed, dict) else None
    except (ValueError, UnicodeDecodeError, zlib.error):
        return None


def install_runtime_patches(main_module: Any, parser_modules: Mapping[str, Any]) -> None:
    """Install Worker-safe path and parser-discovery shims on upstream main.py."""
    main_module.parsers_mod.clear()
    main_module.parsers_mod.update(parser_modules)

    def get_template() -> list[str]:
        template_dir = BASE_DIR / "config_template"
        return sorted(path.stem for path in template_dir.glob("*.json"))

    def load_json(path: str) -> Any:
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = BASE_DIR / file_path
        return json.loads(file_path.read_text(encoding="utf-8"))

    original_read_file = main_module.tool.readFile

    def read_file(path: str) -> bytes:
        file_path = Path(path)
        if not file_path.is_absolute():
            candidate = BASE_DIR / file_path
            if candidate.exists():
                file_path = candidate
        if file_path.exists():
            return file_path.read_bytes()
        return original_read_file(path)

    main_module.get_template = get_template
    main_module.load_json = load_json
    main_module.tool.readFile = read_file


def run_upstream_main(main_module: Any, argv: Sequence[str]) -> int:
    """Execute upstream ``main.py`` CLI behavior in-process."""
    template_index_raw = _arg(argv, "--template_index", "0")
    gh_proxy_index_raw = _arg(argv, "--gh_proxy_index", "")
    providers = decode_temp_json_arg(_arg(argv, "--temp_json_data", "{}"))

    if not providers:
        providers = main_module.load_json("providers.json")

    main_module.providers = providers
    main_module.temp_json_data = json.dumps(providers, ensure_ascii=False)
    main_module.args = SimpleNamespace(
        template_index=int(template_index_raw or 0),
        gh_proxy_index=gh_proxy_index_raw,
    )

    if providers.get("config_template"):
        response = main_module.requests.get(providers["config_template"], timeout=30)
        response.raise_for_status()
        config = response.json()
    else:
        template_list = main_module.get_template()
        if not template_list:
            raise RuntimeError("No config templates found")
        template_index = int(template_index_raw or 0)
        if template_index < 0 or template_index >= len(template_list):
            raise IndexError(
                f"template index {template_index} out of range (0..{len(template_list)-1})"
            )
        config_path = f"config_template/{template_list[template_index]}.json"
        config = main_module.load_json(config_path)

    nodes = main_module.process_subscribes(providers["subscribes"])

    if gh_proxy_index_raw.isdigit():
        gh_proxy_index = int(gh_proxy_index_raw)
        rule_set = config.get("route", {}).get("rule_set", [])
        urls = [item["url"] for item in rule_set if item.get("url")]
        if urls:
            new_urls = main_module.set_gh_proxy(urls, gh_proxy_index)
            new_iter = iter(new_urls)
            for item in rule_set:
                if item.get("url"):
                    item["url"] = next(new_iter)

    if providers.get("Only-nodes"):
        final_config: Any = [
            content
            for contents in nodes.values()
            for content in contents
        ]
    else:
        final_config = main_module.combin_to_config(config, nodes)

    requested_path = str(providers.get("save_config_path", "config.json"))
    if requested_path.startswith("./"):
        requested_path = requested_path[2:]
    requested_path = requested_path or "config.json"

    output_path = Path(tempfile.gettempdir()) / requested_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    main_module.save_config(str(output_path), final_config)
    return 0
