import base64
import json
import tempfile
import zlib
from pathlib import Path
from types import SimpleNamespace

from cloudflare_adapter import (
    decode_state_cookie_parts,
    decode_temp_json_arg,
    run_upstream_main,
    should_load_editor_state,
)


def test_decode_double_encoded_json():
    payload = {"subscribes": [], "save_config_path": "./config.json"}
    raw = json.dumps(json.dumps(payload))
    assert decode_temp_json_arg(raw) == payload


def test_stateless_config_route_does_not_load_editor_state():
    assert should_load_editor_state("/") is True
    assert should_load_editor_state("/edit_temp_json") is True
    assert should_load_editor_state("/generate_config") is True
    assert should_load_editor_state("/config/https://example.com/sub") is False


def test_decode_state_cookie_parts_accepts_valid_json():
    value = json.dumps({"subscribes": [{"url": "https://example.com/sub"}]})
    encoded = base64.urlsafe_b64encode(zlib.compress(value.encode())).decode()
    parts = [encoded[i : i + 50] for i in range(0, len(encoded), 50)]

    assert decode_state_cookie_parts(
        parts,
        chunk_size=50,
        max_chunks=8,
        max_output_size=4096,
    ) == value


def test_decode_state_cookie_parts_rejects_oversized_chunk():
    assert decode_state_cookie_parts(
        ["A" * 51],
        chunk_size=50,
        max_chunks=8,
        max_output_size=4096,
    ) is None


def test_decode_state_cookie_parts_bounds_decompression():
    value = json.dumps({"data": "A" * 100_000})
    encoded = base64.urlsafe_b64encode(zlib.compress(value.encode(), 9)).decode()

    assert decode_state_cookie_parts(
        [encoded],
        chunk_size=3000,
        max_chunks=8,
        max_output_size=4096,
    ) is None


def test_run_upstream_main_only_nodes_preserves_output_subdirectory(
    monkeypatch, tmp_path
):
    saved = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"outbounds": []}

    fake = SimpleNamespace()
    fake.requests = SimpleNamespace(get=lambda *a, **k: Response())
    fake.load_json = lambda path: {"outbounds": [], "route": {"rule_set": []}}
    fake.get_template = lambda: ["template"]
    fake.process_subscribes = lambda subscriptions: {
        "g1": [{"type": "vless", "tag": "n1"}],
        "g2": [{"type": "trojan", "tag": "n2"}],
    }
    fake.combin_to_config = lambda config, nodes: {"combined": True}
    fake.set_gh_proxy = lambda urls, index: urls

    def save_config(path, data):
        saved["path"] = path
        saved["data"] = data

    fake.save_config = save_config
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    providers = {
        "subscribes": [{"url": "x", "tag": "g1"}],
        "save_config_path": "./nested/custom.json",
        "Only-nodes": True,
    }
    argv = [
        "python",
        "main.py",
        "--template_index",
        "0",
        "--temp_json_data",
        json.dumps(json.dumps(providers)),
        "--gh_proxy_index",
        "",
    ]

    assert run_upstream_main(fake, argv) == 0
    assert Path(saved["path"]) == tmp_path / "nested" / "custom.json"
    assert (tmp_path / "nested").is_dir()
    assert saved["data"] == [
        {"type": "vless", "tag": "n1"},
        {"type": "trojan", "tag": "n2"},
    ]
