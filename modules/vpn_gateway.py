from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx


GATEWAY_PORT_MIN = 21000
GATEWAY_PORT_MAX = 21999
GATEWAY_RECHECK_SECONDS = 300
logger = logging.getLogger("vpn-gateway")


def xray_executable() -> str:
    """Resolve an explicitly configured Xray executable or one on PATH."""
    configured = os.getenv("XRAY_EXECUTABLE", "").strip()
    if configured:
        return configured
    discovered = shutil.which("xray") or shutil.which("xray-core")
    if discovered:
        return discovered
    raise RuntimeError(
        "Xray was not found. Set XRAY_EXECUTABLE or install xray on this host."
    )


def _subscription_url(env_name: str) -> str:
    configured = os.getenv(env_name, "").strip()
    if configured:
        return configured
    raise RuntimeError(f"Set {env_name} to your own subscription URL")


def _provider_enabled(name: str) -> bool:
    """Allow a dead subscription provider to be disabled without code edits."""
    return os.getenv(name, "1").strip().lower() not in {"0", "false", "no", "off"}


def _gateway_outbound_mark() -> int | None:
    """Optional Linux SO_MARK for gateway upstreams in full-system VPN mode."""
    raw = os.getenv("VPN_GATEWAY_OUTBOUND_MARK", "").strip()
    if not raw:
        return None
    try:
        mark = int(raw, 0)
    except ValueError as exc:
        raise ValueError("VPN_GATEWAY_OUTBOUND_MARK must be an integer") from exc
    if mark <= 0:
        raise ValueError("VPN_GATEWAY_OUTBOUND_MARK must be positive")
    return mark


def _http_get(url: str, timeout: float = 30) -> httpx.Response:
    """Fetch a user-configured subscription, respecting standard proxy env vars."""
    return httpx.get(url, timeout=timeout, follow_redirects=True)


def fetch_vless_nodes() -> list[str]:
    local_file = os.getenv("VPN_VLESS_FILE", "").strip()
    if local_file:
        content = Path(local_file).read_text(encoding="utf-8")
    else:
        response = _http_get(_subscription_url("VPN_VLESS_SUBSCRIPTION_URL"))
        response.raise_for_status()
        content = response.text
    # Both plain URI lists and base64-encoded subscription lists are supported.
    decoded = content.strip()
    for _ in range(2):
        if "vless://" in decoded:
            break
        try:
            decoded = base64.b64decode(decoded).decode("utf-8")
        except (ValueError, UnicodeError):
            break
    return [line.strip() for line in decoded.splitlines() if line.strip().startswith("vless://")]


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _outbound(uri: str, tag: str) -> dict:
    parsed = urlparse(uri)
    query = parse_qs(parsed.query)
    user = {
        "id": unquote(parsed.username or ""),
        "encryption": _first(query, "encryption", "none"),
    }
    flow = _first(query, "flow")
    import os
    if os.getenv("DISABLE_XTLS_VISION") == "1":
        flow = ""
    if flow:
        user["flow"] = flow
    network = _first(query, "type", "tcp")
    security = _first(query, "security", "none")
    stream: dict = {"network": network, "security": security}
    if security == "reality":
        stream["realitySettings"] = {
            "serverName": _first(query, "sni"),
            "fingerprint": _first(query, "fp", "chrome"),
            "publicKey": _first(query, "pbk"),
            "shortId": _first(query, "sid"),
            "spiderX": _first(query, "spx", "/"),
        }
    elif security == "tls":
        stream["tlsSettings"] = {
            "serverName": _first(query, "sni"),
            "fingerprint": _first(query, "fp", "chrome"),
            "allowInsecure": _first(query, "allowInsecure") in {"1", "true"},
        }
    if network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": _first(query, "serviceName"),
            "multiMode": _first(query, "mode") == "multi",
        }
    elif network == "ws":
        host = _first(query, "host")
        stream["wsSettings"] = {
            "path": _first(query, "path", "/"),
            "headers": {"Host": host} if host else {},
        }
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": parsed.hostname,
                "port": parsed.port or 443,
                "users": [user],
            }]
        },
        "streamSettings": stream,
    }


def build_config(nodes: list[str], start_port: int = 21000) -> tuple[dict, list[int]]:
    inbounds, outbounds, rules, ports = [], [], [], []
    for index, uri in enumerate(nodes):
        inbound_tag = f"local-{index}"
        outbound_tag = f"trust-{index}"
        port = start_port + index
        ports.append(port)
        inbounds.append({
            "tag": inbound_tag,
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        })
        outbounds.append(_outbound(uri, outbound_tag))
        rules.append({"type": "field", "inboundTag": [inbound_tag], "outboundTag": outbound_tag})
    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }, ports


def write_config(output: Path, start_port: int = 21000) -> list[int]:
    nodes = sync_filter_vless_nodes(fetch_vless_nodes())
    if not nodes:
        raise RuntimeError("Subscription returned no VLESS nodes")
    config, ports = build_config(nodes, start_port)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return ports


def fetch_ultima_configs() -> list[dict]:
    response = _http_get(
        _subscription_url("VPN_XRAY_SUBSCRIPTION_URL"),
        timeout=60,
    )
    response.raise_for_status()
    configs = response.json()
    if not isinstance(configs, list):
        raise RuntimeError("Xray subscription must contain a JSON array of configurations")
    return [item for item in configs if isinstance(item, dict)]


def build_ultima_config(configs: list[dict], start_port: int = 21100) -> tuple[dict, list[int]]:
    inbounds: list[dict] = []
    outbounds: list[dict] = []
    rules: list[dict] = []
    ports: list[int] = []
    for index, source in enumerate(configs):
        candidates = [
            outbound for outbound in source.get("outbounds", [])
            if outbound.get("protocol") not in {"freedom", "blackhole", "dns"}
        ]
        if not candidates:
            continue
        inbound_tag = f"ultima-local-{index}"
        outbound_tag = f"ultima-{index}"
        port = start_port + len(ports)
        outbound = copy.deepcopy(candidates[0])
        outbound["tag"] = outbound_tag
        ports.append(port)
        inbounds.append({
            "tag": inbound_tag,
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        })
        outbounds.append(outbound)
        rules.append({
            "type": "field", "inboundTag": [inbound_tag],
            "outboundTag": outbound_tag,
        })
    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }, ports


def write_ultima_config(output: Path, start_port: int = 21100) -> list[int]:
    config, ports = build_ultima_config(fetch_ultima_configs(), start_port)
    if not ports:
        raise RuntimeError("Xray subscription returned no usable outbounds")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return ports


def write_gateway_config(output: Path) -> list[int]:
    """Build one Xray config from all enabled subscriptions.

    At least one provider must yield an outbound.  There is deliberately no
    ``freedom`` fallback: if every VPN exit disappears, Telegram I/O stops.
    """
    inbounds: list[dict] = []
    outbounds: list[dict] = []
    rules: list[dict] = []
    ports: list[int] = []
    errors: list[str] = []
    providers = []
    if _provider_enabled("VPN_ENABLE_VLESS") and (
        os.getenv("VPN_VLESS_SUBSCRIPTION_URL") or os.getenv("VPN_VLESS_FILE")
    ):
        providers.append(("vless", lambda: build_config(sync_filter_vless_nodes(fetch_vless_nodes()), GATEWAY_PORT_MIN)))
    if _provider_enabled("VPN_ENABLE_XRAY") and os.getenv("VPN_XRAY_SUBSCRIPTION_URL"):
        providers.append(("xray-json", lambda: build_ultima_config(fetch_ultima_configs(), 21100)))
    if not providers:
        raise RuntimeError("Configure VPN_VLESS_SUBSCRIPTION_URL, VPN_VLESS_FILE or VPN_XRAY_SUBSCRIPTION_URL")
    for provider, build in providers:
        try:
            config, provider_ports = build()
            if not provider_ports:
                raise RuntimeError("subscription has no usable outbounds")
            inbounds.extend(config["inbounds"])
            outbounds.extend(config["outbounds"])
            rules.extend(config["routing"]["rules"])
            ports.extend(provider_ports)
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
    if not ports:
        raise RuntimeError("No VPN gateway exits available (" + "; ".join(errors) + ")")
    if mark := _gateway_outbound_mark():
        for outbound in outbounds:
            stream = outbound.setdefault("streamSettings", {})
            stream.setdefault("sockopt", {})["mark"] = mark
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return ports


def gateway_ports(config_path: Path) -> list[int]:
    """Read the local SOCKS inbounds from a generated Xray configuration."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        int(inbound["port"])
        for inbound in config.get("inbounds", [])
        if inbound.get("listen") in {"127.0.0.1", "::1"}
        and inbound.get("protocol") == "socks"
        and GATEWAY_PORT_MIN <= int(inbound.get("port", 0)) <= GATEWAY_PORT_MAX
    ]


def test_config(config_path: Path) -> None:
    result = subprocess.run(
        [xray_executable(), "run", "-test", "-config", str(config_path)],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Xray rejected generated gateway configuration")


async def sync_gateway_pool(pool, ports: list[int], validate=None) -> dict[str, int]:
    """Validate local exits and atomically retire all legacy public proxies."""
    if validate is None:
        from modules.proxy_manager import validate_proxy
        validate = validate_proxy

    await pool.archive_non_gateway_entries()
    async def check(port: int):
        ok, latency = await validate("127.0.0.1", port, "socks5", timeout=12)
        return ("127.0.0.1", port, "socks5", ok, latency)

    results = list(await asyncio.gather(*(check(port) for port in ports)))
    await pool.store_gateway_results(results)
    valid = sum(1 for _ip, _port, _proto, ok, _latency in results if ok)
    return {"configured": len(ports), "valid": valid, "dead": len(ports) - valid}


async def run_gateway_service(pool, data_dir: Path, refresh_minutes: int) -> None:
    """Keep Xray and the local VPN-exit pool alive for a systemd service."""
    refresh_seconds = max(refresh_minutes, 5) * 60
    config_path = data_dir / "vpn_gateway" / "xray.json"
    while True:
        try:
            try:
                ports = write_gateway_config(config_path)
            except Exception as exc:
                ports = gateway_ports(config_path)
                if ports:
                    logger.warning(
                        "Failed to refresh gateway config: %s; using existing config with %d ports",
                        exc,
                        len(ports),
                    )
                else:
                    raise
            test_config(config_path)
            log_path = config_path.with_name("xray.log")
            with log_path.open("a", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [xray_executable(), "run", "-config", str(config_path)],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    await asyncio.sleep(2)
                    if process.poll() is not None:
                        raise RuntimeError(f"Xray exited during startup; see {log_path}")
                    stats = await sync_gateway_pool(pool, ports)
                    logger.info(
                        "VPN gateway pool synced: configured=%s valid=%s dead=%s",
                        stats["configured"], stats["valid"], stats["dead"],
                    )
                    deadline = time.monotonic() + refresh_seconds
                    next_recheck = time.monotonic() + min(GATEWAY_RECHECK_SECONDS, refresh_seconds)
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError(f"Xray exited; see {log_path}")
                        if time.monotonic() >= next_recheck:
                            stats = await sync_gateway_pool(pool, ports)
                            logger.info(
                                "VPN gateway pool rechecked: configured=%s valid=%s dead=%s",
                                stats["configured"], stats["valid"], stats["dead"],
                            )
                            next_recheck = time.monotonic() + min(GATEWAY_RECHECK_SECONDS, refresh_seconds)
                        await asyncio.sleep(5)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("VPN gateway service error: %s (retrying in 15s)", exc)
            await asyncio.sleep(15)

async def filter_vless_nodes_via_xray(nodes: list[str], timeout: float = 6.0) -> list[str]:
    """Test VLESS nodes via a temporary Xray instance and return only alive ones."""
    if not nodes:
        return []
    import logging
    from modules.proxy_manager import validate_proxy
    logger = logging.getLogger("vpn-gateway")
    
    config, ports = build_config(nodes, 22000)
    temp_config = Path("temp_xray_test.json")
    temp_config.write_text(json.dumps(config), encoding="utf-8")
    
    logger.info("Starting temporary Xray to test %d nodes...", len(nodes))
    proc = subprocess.Popen([xray_executable(), "run", "-c", str(temp_config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await asyncio.sleep(2.5)
    
    alive_nodes = []
    
    sem = asyncio.Semaphore(15)
    async def check(port, node):
        async with sem:
            ok, lat = await validate_proxy("127.0.0.1", port, "socks5", timeout=timeout)
            if ok:
                alive_nodes.append((lat, node))
            
    tasks = [check(port, node) for port, node in zip(ports, nodes)]
    await asyncio.gather(*tasks)
    
    proc.terminate()
    proc.wait()
    temp_config.unlink(missing_ok=True)
    
    alive_nodes.sort(key=lambda x: x[0])
    logger.info("Found %d alive nodes out of %d.", len(alive_nodes), len(nodes))
    return [node for lat, node in alive_nodes]

def sync_filter_vless_nodes(nodes: list[str]) -> list[str]:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        return loop.run_until_complete(filter_vless_nodes_via_xray(nodes))
    except RuntimeError:
        return asyncio.run(filter_vless_nodes_via_xray(nodes))
