import json
from pathlib import Path
from types import SimpleNamespace

from cloudflare_adapter import decode_temp_json_arg, run_upstream_main


def test_decode_double_encoded_json():
    payload = {"subscribes": [], "save_config_path": "./config.json"}
    raw = json.dumps(json.dumps(payload))
    assert decode_temp_json_arg(raw) == payload


def test_run_upstream_main_only_nodes(monkeypatch, tmp_path):
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

    providers = {
        "subscribes": [{"url": "x", "tag": "g1"}],
        "save_config_path": "./custom.json",
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
    assert Path(saved["path"]).name == "custom.json"
    assert saved["data"] == [
        {"type": "vless", "tag": "n1"},
        {"type": "trojan", "tag": "n2"},
    ]
