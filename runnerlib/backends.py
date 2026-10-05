"""Several Cadre backends behind one fleet page.

A backend is one machine's Cadre: its daemon, its review-surface page server
and the agent harness logged in there (Claude Code today). Each one answers
the same small JSON API on its fleet page's port:

    GET /api/fleet/version   {"version", "instance", "machine"} — cheap, polled
    GET /api/fleet           everything the fleet renders for that machine

The fleet page is a client of that API and nothing else. It fetches THIS
machine's backend over HTTP exactly as it fetches a remote one, renders each
under the name of the machine it lives on, and points that section's links at
the backend's own origin, so opening a page, annotating it and the turn the
annotation starts all happen on the machine that owns the page.

Plain request/response, no held connections between the page and a backend:
the version is polled (VERSION_TTL), and the full payload is re-fetched only
when the version moves or the copy is old enough for its relative times to
read wrong (FLEET_TTL). A backend that does not answer is a loud card, never a
missing section.

For a remote browser to reach a backend at all, its fleet page has to listen
on an address the tailnet can reach (`status_bind`, which may list several),
and its page server has to accept the names the browser uses for it, or every
page answers "forbidden host". `reachable_names` works the second out from
the bind and `tailscale status`, for `pipeline.py up` to hand review-surface
as REVIEW_SURFACE_ALLOWED_HOSTS.
"""

import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from html import escape

VERSION_PATH = "/api/fleet/version"
FLEET_PATH = "/api/fleet"
VERSION_TTL = 2.0
FLEET_TTL = 30.0
TIMEOUT = 3.0
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

_lock = threading.Lock()
_versions: dict[str, tuple[float, dict | None, str | None]] = {}
_fleets: dict[str, tuple[float, str, dict]] = {}


def _get_json(url: str, timeout: float = TIMEOUT) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def self_url(server_address) -> str:
    """How this process reaches its own backend, from the socket a request
    arrived on: loopback when that socket listens there or everywhere."""
    host, port = server_address[0], server_address[1]
    if host in ("0.0.0.0", "127.0.0.1", "localhost", ""):
        return f"http://127.0.0.1:{port}"
    if host in ("::", "::1"):
        return f"http://[::1]:{port}"
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def with_self(backends: list[dict], server_address) -> list[dict]:
    """The configured list with this machine's entry (url None) resolved."""
    url = self_url(server_address)
    return [{**b, "url": url} if b["url"] is None else b for b in backends]


def configured(raw: dict) -> list[dict]:
    """This machine's backend first (its URL is None until with_self resolves
    it from the listening socket), then every usable `[[backends]]` block (a
    name and an http(s) URL). A typo drops the entry rather than taking the
    page down; it shows up as a missing section."""
    out = [{"name": "", "url": None}]
    for b in raw.get("backends") or []:
        name, url = str(b.get("name") or "").strip(), str(b.get("url") or "").strip()
        if urllib.parse.urlsplit(url).scheme in ("http", "https"):
            out.append({"name": name, "url": url.rstrip("/")})
    return out


def version(b: dict, now: float | None = None) -> tuple[dict | None, str | None]:
    """({version, instance, machine}, error) for one backend, cached for
    VERSION_TTL — failures included, so a backend that is down costs one
    timeout per TTL, not one per stream tick."""
    now = time.time() if now is None else now
    with _lock:
        hit = _versions.get(b["url"])
        if hit and now - hit[0] < VERSION_TTL:
            return hit[1], hit[2]
    v, err = None, None
    try:
        v = _get_json(b["url"] + VERSION_PATH)
        if not isinstance(v, dict) or "version" not in v:
            v, err = None, "it answered, but not with a Cadre backend's version"
    except Exception as e:                      # offline, refused, timed out, 404
        err = str(getattr(e, "reason", None) or e)
    with _lock:
        _versions[b["url"]] = (now, v, err)
    return v, err


def fleet(b: dict, now: float | None = None) -> tuple[dict | None, str | None]:
    """(payload, error): the backend's whole fleet, re-fetched only when its
    version moved or the copy is older than FLEET_TTL."""
    now = time.time() if now is None else now
    v, err = version(b, now)
    if v is None:
        return None, err
    with _lock:
        hit = _fleets.get(b["url"])
    if hit and hit[1] == v["version"] and now - hit[0] < FLEET_TTL:
        return hit[2], None
    try:
        data = _get_json(b["url"] + FLEET_PATH, timeout=TIMEOUT * 3)
    except Exception as e:
        return None, str(getattr(e, "reason", None) or e)
    with _lock:
        _fleets[b["url"]] = (now, v["version"], data)
    return data, None


def forget():
    """Drop every cached version and payload. Called after a write through
    the fleet page, so the redirect that follows shows what was just done
    instead of waiting out VERSION_TTL."""
    with _lock:
        _versions.clear()
        _fleets.clear()


def signature(backends: list[dict], now: float | None = None) -> tuple[str, bool]:
    """(one string over every backend's version, whether every one answered
    and can see its own review pages change) — the fleet stream's change test
    and its live indicator."""
    parts, ok = [], True
    for b in backends:
        v, err = version(b, now)
        ok = ok and v is not None and bool(v.get("review_events", True))
        parts.append(f'{b["url"]}={v["version"] if v else "!" + str(err)}')
    return "|".join(parts), ok


def point_at(html: str, b: dict, same_origin: bool) -> str:
    """A section rendered from a backend's data, with its links aimed at that
    backend. Root-relative links and form actions become absolute on the
    backend's origin and open in a new tab: its review pages only work there,
    because that is the page server their annotations post to, and a row's
    Archive must end the page on the machine that holds it. Fold keys get the backend's URL so
    two machines with a project of the same name fold independently. The
    backend this page is served from keeps its links as they are."""
    key = escape(b["url"], quote=True)
    html = html.replace('data-fold="', f'data-fold="{key}|')
    if same_origin:
        return html
    html = re.sub(r'action="/', f'target="_blank" action="{key}/', html)
    return re.sub(r'href="/', f'target="_blank" rel="noopener" href="{key}/', html)


def _bind_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return str(value or "").replace(",", " ").split()


def binds(value) -> list[str]:
    """`status_bind` as a list: one address (the old shape), a TOML array, or
    a comma/space separated string. Binding loopback AND the tailnet address
    keeps http://127.0.0.1 working without exposing the page to every
    network 0.0.0.0 would."""
    return _bind_list(value) or ["127.0.0.1"]


def _tailscale_self(run=subprocess.run) -> dict:
    exe = shutil.which("tailscale")
    if not exe:
        return {}
    try:
        out = run([exe, "status", "--json", "--self", "--peers=false"],
                  capture_output=True, text=True, timeout=5)
        return json.loads(out.stdout or "{}").get("Self") or {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _tailscale_names(run=subprocess.run) -> list[str]:
    me = _tailscale_self(run)
    # Not HostName: that is the OS hostname, which MagicDNS may not answer to.
    dns = str(me.get("DNSName") or "").rstrip(".")
    names = [dns, dns.split(".", 1)[0]] if dns else []      # FQDN, short name
    return names + [str(ip) for ip in me.get("TailscaleIPs") or []]


def machine_name(runner: dict, run=subprocess.run) -> str:
    """What this backend calls its machine on every fleet that shows it: the
    configured `machine_name`, else its tailnet name, else the hostname."""
    explicit = str(runner.get("machine_name") or "").strip()
    if explicit:
        return explicit
    dns = str(_tailscale_self(run).get("DNSName") or "").rstrip(".")
    return dns.split(".", 1)[0] if dns else socket.gethostname()


def reachable_names(runner: dict, run=subprocess.run) -> list[str]:
    """The hostnames a remote browser uses to reach this fleet page, for
    review-surface's Host allowlist. An explicit `allowed_hosts` wins. Else,
    when the fleet page is bound beyond loopback, the bind addresses plus
    whatever Tailscale calls this machine. Empty while it is loopback-only:
    nothing remote can reach it, so there is nothing to allow."""
    explicit = _bind_list(runner.get("allowed_hosts"))
    if explicit:
        return explicit
    remote = [b for b in binds(runner.get("status_bind")) if b not in LOOPBACK]
    if not remote:
        return []
    names = [b for b in remote if b not in ("0.0.0.0", "::")] + _tailscale_names(run)
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out
