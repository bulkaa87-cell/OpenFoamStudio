"""Server setup: Tailscale on this computer, the SSH key, the server check.

The logic of the server wizard (fs_serverwiz.py), without Qt, so that a test
can call each step:

- Tailscale on this computer: find the program, read `tailscale status
  --json` (state, the name of the tailnet, the peers). The login happens in
  the browser of the person. This module never asks for, keeps or writes an
  auth key or a password.
- The SSH key: find a key, make an ed25519 key without a passphrase (the
  program logs in with BatchMode, without questions), the command that
  copies the public key to a server (the person types the server password
  one time, in a terminal window).
- The server: test the login, read the OpenFOAM installs (native, docker,
  podman), the cores, the memory and the free disk.
- The SSH config: the aliases that are in it, a new `Host` entry (with
  ProxyJump for a server behind another server).
- A setup script (bash) for a NEW, empty Linux server: Tailscale, the SSH
  server, OpenFOAM v2512 and the public key of this computer. The person runs
  it on the server; the program does not run it anywhere.

Environment (tests):
    FS_SSH_DIR      the folder of the keys (default ~/.ssh)
    FS_SSH_CONFIG   the SSH config file that the wizard reads and writes
                    (default <ssh dir>/config); ssh then gets `-F <file>`
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from fs_i18n import tr

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

TAILSCALE_DOWNLOAD = "https://tailscale.com/download"
TAILSCALE_INSTALL_SH = "https://tailscale.com/install.sh"
OPENFOAM_APT_REPO = "https://dl.openfoam.com/add-debian-repo.sh"
OPENFOAM_VERSION = "2512"
OPENFOAM_IMAGE = "opencfd/openfoam-run:" + OPENFOAM_VERSION
KEY_NAMES = ("id_ed25519", "id_ecdsa", "id_rsa")
KEY_COMMENT = "openfoam-studio"


def _run(argv: list[str], timeout: float = 20, stdin: str | None = None,
         env: dict | None = None) -> tuple[int, str, str]:
    """(exit code, stdout, stderr). Exit code 124: timeout, 127: no program."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           input=stdin.encode("utf-8") if stdin is not None
                           else None,
                           stdin=None if stdin is not None
                           else subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", str(exc)
    return (p.returncode, p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# Tailscale on this computer
# --------------------------------------------------------------------------

def tailscale_exe() -> str | None:
    """The Tailscale command line program, or None."""
    found = shutil.which("tailscale")
    if found:
        return found
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", "")):
            if base:
                cands.append(os.path.join(base, "Tailscale", "tailscale.exe"))
    elif sys.platform == "darwin":
        cands.append("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    else:
        cands += ["/usr/bin/tailscale", "/usr/local/bin/tailscale"]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


@dataclass
class Peer:
    name: str                 # HostName
    dns: str = ""             # MagicDNS name, without the last dot
    ip: str = ""              # first IPv4 address
    os: str = ""
    online: bool = False
    relay: str = ""           # DERP region (for example "fra")
    direct: str = ""          # address of a direct path, "" = relay only

    @property
    def address(self) -> str:
        """The address that SSH uses: the MagicDNS name, else the IP."""
        return self.dns or self.ip or self.name

    def line(self) -> str:
        state = tr("online") if self.online else tr("offline")
        path = ""
        if self.online:
            path = (tr("direct") if self.direct
                    else tr("relay {relay}", relay=self.relay or "?"))
        parts = [self.name, self.ip, self.os, state] + ([path] if path else [])
        return "  ·  ".join(str(p) for p in parts if p)


@dataclass
class TailscaleState:
    installed: bool = False
    exe: str = ""
    running: bool = False         # BackendState == "Running"
    backend: str = ""             # Running, NeedsLogin, Stopped, ...
    self_name: str = ""
    self_ip: str = ""
    tailnet: str = ""
    auth_url: str = ""
    error: str = ""
    peers: list = field(default_factory=list)

    def text(self) -> str:
        """One line for the person."""
        if not self.installed:
            return tr("Tailscale is not installed on this computer.")
        if self.error:
            return tr("Tailscale is installed, but its service does not "
                      "answer: {error}", error=self.error)
        if self.running:
            return tr("Tailscale is connected: this computer is {name} "
                      "({ip}) in the tailnet {tailnet}.", name=self.self_name,
                      ip=self.self_ip, tailnet=self.tailnet or "?")
        if self.backend == "NeedsLogin":
            return tr("Tailscale is installed, but you are not logged in.")
        if self.backend == "Stopped":
            return tr("Tailscale is installed, but it is off (Stopped).")
        return tr("Tailscale state: {state}", state=self.backend or "?")


def parse_status(text: str) -> TailscaleState:
    """TailscaleState from the output of `tailscale status --json`."""
    st = TailscaleState(installed=True)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        st.error = (text or "").strip().splitlines()[-1][:200] if (
            text or "").strip() else "no output"
        return st
    st.backend = str(data.get("BackendState") or "")
    st.running = st.backend == "Running"
    st.auth_url = str(data.get("AuthURL") or "")
    me = data.get("Self") or {}
    st.self_name = str(me.get("HostName") or "")
    ips = [ip for ip in (me.get("TailscaleIPs") or []) if ":" not in ip]
    st.self_ip = ips[0] if ips else ""
    tn = data.get("CurrentTailnet") or {}
    st.tailnet = str(tn.get("Name") or data.get("MagicDNSSuffix") or "")
    peers = []
    for p in (data.get("Peer") or {}).values():
        ips = [ip for ip in (p.get("TailscaleIPs") or []) if ":" not in ip]
        peers.append(Peer(
            name=str(p.get("HostName") or ""),
            dns=str(p.get("DNSName") or "").rstrip("."),
            ip=ips[0] if ips else "",
            os=str(p.get("OS") or ""),
            online=bool(p.get("Online")),
            relay=str(p.get("Relay") or ""),
            direct=str(p.get("CurAddr") or "")))
    # Linux servers first, then the computers that are online.
    peers.sort(key=lambda p: (p.os != "linux", not p.online, p.name.lower()))
    st.peers = peers
    return st


def tailscale_status(exe: str | None = None) -> TailscaleState:
    """Read the state of Tailscale on this computer. Changes nothing."""
    exe = exe or tailscale_exe()
    if not exe:
        return TailscaleState()
    rc, out, err = _run([exe, "status", "--json"], timeout=15)
    if rc == 127:
        return TailscaleState()
    st = parse_status(out if out.strip().startswith("{") else (err or out))
    st.exe = exe
    return st


AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")


def login_argv(state: TailscaleState) -> list[str]:
    """The command that connects this computer to the tailnet.

    `tailscale login` (not logged in) or `tailscale up` (logged in, but
    off). It prints a login address; the person logs in in the browser.
    No auth key: the program never gives one.
    """
    verb = "up" if state.backend == "Stopped" else "login"
    return [state.exe or tailscale_exe() or "tailscale", verb]


# --------------------------------------------------------------------------
# SSH key and config of this computer
# --------------------------------------------------------------------------

def ssh_dir() -> Path:
    env = os.environ.get("FS_SSH_DIR", "").strip()
    return Path(env) if env else Path(os.path.expanduser("~")) / ".ssh"


def ssh_config_path() -> Path:
    env = os.environ.get("FS_SSH_CONFIG", "").strip()
    return Path(env) if env else ssh_dir() / "config"


def ssh_exe() -> str | None:
    found = shutil.which("ssh")
    if found:
        return found
    if os.name == "nt":
        c = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32", "OpenSSH", "ssh.exe")
        if os.path.isfile(c):
            return c
    return None


def ssh_keygen_exe() -> str | None:
    ssh = ssh_exe()
    if ssh:
        c = os.path.join(os.path.dirname(ssh),
                         "ssh-keygen.exe" if os.name == "nt" else "ssh-keygen")
        if os.path.isfile(c):
            return c
    return shutil.which("ssh-keygen")


def find_key(folder: Path | None = None) -> Path | None:
    """The private key that SSH uses by default (it has a .pub), or None."""
    folder = folder or ssh_dir()
    for name in KEY_NAMES:
        priv = folder / name
        if priv.is_file() and priv.with_name(name + ".pub").is_file():
            return priv
    return None


def create_key(folder: Path | None = None) -> tuple[Path | None, str]:
    """Make ~/.ssh/id_ed25519 (no passphrase). Never overwrites a key.

    Returns (private key, "") or (None, why not).
    """
    folder = folder or ssh_dir()
    priv = folder / "id_ed25519"
    if priv.exists() or priv.with_name("id_ed25519.pub").exists():
        return None, tr("The file {path} is already there. The program does "
                        "not overwrite a key.", path=priv)
    keygen = ssh_keygen_exe()
    if not keygen:
        return None, tr("ssh-keygen was not found. Install the OpenSSH "
                        "client (Windows: Settings → Apps → Optional "
                        "features → OpenSSH Client).")
    folder.mkdir(parents=True, exist_ok=True)
    comment = f"{KEY_COMMENT}@{socket.gethostname()}"
    rc, out, err = _run([keygen, "-q", "-t", "ed25519", "-N", "", "-C",
                         comment, "-f", str(priv)], timeout=30)
    if rc != 0 or not priv.is_file():
        return None, (err or out).strip()[:300] or tr("ssh-keygen failed")
    return priv, ""


def public_key_text(priv: Path) -> str:
    return priv.with_name(priv.name + ".pub").read_text(
        encoding="utf-8", errors="replace").strip()


_WILD = re.compile(r"[*?!]")


def config_hosts(path: Path | None = None) -> dict[str, dict]:
    """{alias: {"hostname":…, "user":…, "proxyjump":…}} of an SSH config.

    Only plain aliases (no patterns). An option in a `Host a b` block goes to
    each alias of the block. `Include` is not followed.
    """
    path = path or ssh_config_path()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    hosts: dict[str, dict] = {}
    current: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s*=\s*|\s+", line, maxsplit=1)
        key = parts[0].lower()
        val = parts[1].strip() if len(parts) > 1 else ""
        if key == "host":
            current = [a for a in val.split() if not _WILD.search(a)]
            for a in current:
                hosts.setdefault(a, {})
        elif key == "match":
            current = []
        elif key in ("hostname", "user", "proxyjump", "port", "identityfile"):
            for a in current:
                hosts[a].setdefault(key, val)
    return hosts


_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def valid_alias(alias: str) -> bool:
    return bool(_ALIAS_RE.match(alias or ""))


def suggest_alias(host: str) -> str:
    """A short alias from a host name: "server1.tail1234.ts.net" -> "server1"."""
    h = (host or "").split("@")[-1].strip()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
        return "server-" + h.replace(".", "-")
    a = re.sub(r"[^A-Za-z0-9._-]", "-", h.split(".")[0]) or "server"
    return a[:63]


def host_block(alias: str, hostname: str, user: str = "", jump: str = "",
               port: str = "", identity: str = "") -> str:
    """The text of a new `Host` entry."""
    out = [f"# Added by OpenFOAM Studio {time.strftime('%Y-%m-%d')}",
           f"Host {alias}", f"    HostName {hostname}"]
    if user:
        out.append(f"    User {user}")
    if port and port != "22":
        out.append(f"    Port {port}")
    if jump:
        out.append(f"    ProxyJump {jump}")
    if identity:
        out.append(f"    IdentityFile {identity}")
    out += ["    ServerAliveInterval 30", "    ServerAliveCountMax 4"]
    return "\n".join(out) + "\n"


def add_host(alias: str, hostname: str, user: str = "", jump: str = "",
             port: str = "", identity: str = "",
             path: Path | None = None) -> tuple[bool, str]:
    """Add a `Host` entry at the end of the SSH config.

    (True, backup path or "") when added. (False, why) when the alias is
    already there (the program does not change an entry of the person) or
    the alias is not valid. Before a change, a copy config.bak-<time>.
    """
    path = path or ssh_config_path()
    if not valid_alias(alias):
        return False, tr("The name «{alias}» is not valid: use letters, "
                         "digits, «.», «-», «_».", alias=alias)
    if alias in config_hosts(path):
        return False, tr("The SSH config already has the name «{alias}».",
                         alias=alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    old = ""
    if path.is_file():
        old = path.read_text(encoding="utf-8", errors="replace")
        backup = str(path.with_name(
            path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S")))
        shutil.copy2(path, backup)
    sep = "" if not old or old.endswith("\n\n") else (
        "\n" if old.endswith("\n") else "\n\n")
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(sep + host_block(alias, hostname, user, jump, port, identity))
    return True, backup


# --------------------------------------------------------------------------
# The server: login and check
# --------------------------------------------------------------------------

@dataclass
class Target:
    """How SSH reaches the server: an alias of the config or [user@]host."""
    host: str
    user: str = ""
    jump: str = ""
    port: str = ""

    def dest(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def options(self, batch: bool = True) -> list[str]:
        out = []
        cfg = os.environ.get("FS_SSH_CONFIG", "").strip()
        if cfg:
            out += ["-F", cfg]
        if batch:
            out += ["-o", "BatchMode=yes"]
        out += ["-o", "ConnectTimeout=15",
                "-o", "StrictHostKeyChecking=accept-new"]
        if self.port and self.port != "22":
            out += ["-p", self.port]
        if self.jump:
            out += ["-J", self.jump]
        return out

    def argv(self, command: str = "", batch: bool = True) -> list[str]:
        return ([ssh_exe() or "ssh", *self.options(batch), self.dest()]
                + ([command] if command else []))


# Error classes of a failed `ssh` login, and the advice for each.
SSH_ERRORS = (
    ("key", ("Permission denied", "Too many authentication failures")),
    ("hostkey", ("REMOTE HOST IDENTIFICATION HAS CHANGED",
                 "Host key verification failed")),
    ("dns", ("Could not resolve hostname", "Name or service not known",
             "No such host is known")),
    ("refused", ("Connection refused",)),
    ("route", ("No route to host", "Network is unreachable")),
    ("timeout", ("timed out", "timeout", "Connection reset",
                 "Connection closed")),
)


def classify_ssh_error(text: str) -> str:
    low = (text or "").lower()
    for code, marks in SSH_ERRORS:
        if any(m.lower() in low for m in marks):
            return code
    return "other"


def ssh_error_advice(code: str) -> str:
    """What to do for an error class (classify_ssh_error)."""
    if code == "key":
        return tr("The server does not accept the key of this computer. "
                  "Copy the key to the server (the button above), then "
                  "test again.")
    if code == "hostkey":
        return tr("The server key is not the same as the key that this "
                  "computer remembers (known_hosts). If the server was "
                  "installed again, remove its old line with: ssh-keygen "
                  "-R <host>. If not, stop and ask the owner of the "
                  "server.")
    if code == "dns":
        return tr("The name of the server is not known. Use the name "
                  "from the Tailscale list or its 100.x.y.z address, and "
                  "make sure that MagicDNS is on in Tailscale.")
    if code == "refused":
        return tr("The server answers, but no SSH server runs on it. On "
                  "the server: sudo systemctl enable --now ssh (Fedora: "
                  "sshd).")
    if code == "route":
        return tr("There is no path to the server. Make sure that "
                  "Tailscale is connected on this computer and on the "
                  "server (tailscale status).")
    if code == "timeout":
        return tr("The connection hangs. If Tailscale shows «Connected», "
                  "the cause can be an old relay (DERP) or a public Wi-Fi "
                  "that blocks the traffic. Try another network (for "
                  "example a phone hotspot), run «tailscale netcheck», or "
                  "restart the Tailscale service as administrator. Note: "
                  "the Tailscale MTU is 1280, so a test «ping -l 1400» "
                  "always fails and tells nothing.")
    return tr("See the message of ssh above.")


def test_login(t: Target, timeout: float = 30) -> tuple[bool, str, str]:
    """Key login without questions. (ok, error class, message)."""
    if not ssh_exe():
        return False, "nossh", tr("ssh was not found. Install the OpenSSH "
                                  "client.")
    rc, out, err = _run(t.argv("echo OFS-LOGIN-OK"), timeout=timeout)
    if rc == 0 and "OFS-LOGIN-OK" in out:
        return True, "", ""
    text = (err or out).strip() or ("timeout" if rc == 124 else f"exit {rc}")
    # The last lines carry the error; the first can be a banner.
    msg = "\n".join(text.splitlines()[-3:])[:400]
    return False, classify_ssh_error(text), msg


def copy_key_remote_command() -> str:
    """The server side of the key copy: append the key from stdin once."""
    return ('k=$(cat); umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; '
            'grep -qxF "$k" ~/.ssh/authorized_keys || '
            'echo "$k" >> ~/.ssh/authorized_keys; echo OFS-KEY-OK')


def copy_key_command(t: Target, priv: Path) -> str:
    """The command that the person runs to copy the public key (one line).

    It asks the server password one time (in the terminal, never in the
    program).
    """
    pub = str(priv) + ".pub"
    opts = " ".join(shlex.quote(o) if os.name != "nt" else
                    (f'"{o}"' if " " in o else o)
                    for o in t.options(batch=False))
    remote = copy_key_remote_command()
    if os.name == "nt":
        return f'type "{pub}" | ssh {opts} {t.dest()} "{remote}"'
    return f"cat {shlex.quote(pub)} | ssh {opts} {t.dest()} {shlex.quote(remote)}"


def open_copy_key_terminal(t: Target, priv: Path, folder: Path) -> tuple[bool, str]:
    """Open a terminal window that runs copy_key_command (Windows).

    On another system: (False, "") and the wizard shows the command.
    """
    if os.name != "nt":
        return False, ""
    folder.mkdir(parents=True, exist_ok=True)
    bat = folder / "ofs-copy-key.cmd"
    bat.write_text(
        "@echo off\r\nchcp 65001 >nul\r\n"
        "echo OpenFOAM Studio: copy the SSH key to " + t.dest() + "\r\n"
        "echo Type the password of the server account (one time).\r\n"
        "echo.\r\n" + copy_key_command(t, priv) + "\r\n"
        "echo.\r\npause\r\n", encoding="utf-8")
    try:
        subprocess.Popen(["cmd.exe", "/c", "start", "OpenFOAM Studio - SSH",
                          "cmd.exe", "/c", str(bat)],
                         creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        return False, str(exc)
    return True, str(bat)


# The probe: one bash script through `ssh … bash -s`. It only reads.
PROBE_SCRIPT = r'''
echo "OFS_PROBE=1"
echo "HOSTNAME=$(hostname 2>/dev/null)"
echo "OS=$( (. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME") )"
echo "KERNEL=$(uname -sm 2>/dev/null)"
echo "NPROC=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null)"
echo "MEM_KB=$(awk '/^MemTotal/{print $2}' /proc/meminfo 2>/dev/null)"
echo "DISK_FREE_KB=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print $4}')"
shopt -s nullglob
for f in /usr/lib/openfoam/openfoam*/etc/bashrc /opt/openfoam*/etc/bashrc \
         /opt/OpenFOAM-*/etc/bashrc "$HOME"/OpenFOAM/OpenFOAM-*/etc/bashrc; do
  # The path in the environment, not in $1: the OpenFOAM bashrc reads its
  # arguments, and with its own path as $1 it sources itself again (hang).
  v=$(OFS_RC="$f" timeout 30 bash -c 'source "$OFS_RC" >/dev/null 2>&1; echo "$WM_PROJECT_VERSION"' 2>/dev/null)
  echo "FOAM_NATIVE=$f|$v"
done
for tool in docker podman; do
  if command -v "$tool" >/dev/null 2>&1; then
    if timeout 20 "$tool" info >/dev/null 2>&1; then acc=ok; else acc=noaccess; fi
    imgs=$(timeout 20 "$tool" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
           | grep -i foam | head -5 | tr '\n' ' ')
    echo "CONTAINER=$tool|$acc|$imgs"
  fi
done
if command -v tailscale >/dev/null 2>&1; then
  echo "TAILSCALE=$(tailscale version 2>/dev/null | head -1)"
fi
if command -v mpirun >/dev/null 2>&1; then echo "MPI=yes"; fi
echo "OFS_PROBE_END=1"
'''


@dataclass
class ServerInfo:
    ok: bool = False
    error: str = ""
    hostname: str = ""
    os: str = ""
    kernel: str = ""
    nproc: int = 0
    mem_gb: float = 0.0
    disk_free_gb: float = 0.0
    foam: list = field(default_factory=list)        # [(bashrc, version)]
    containers: list = field(default_factory=list)  # [(tool, access, [images])]
    tailscale: str = ""

    @property
    def native(self) -> tuple[str, str] | None:
        """The newest native OpenFOAM with a version, or None."""
        ok = [(b, v) for b, v in self.foam if v]
        return sorted(ok, key=lambda bv: bv[1])[-1] if ok else None

    def container_images(self) -> list[str]:
        return [f"{tool}: {img}" for tool, _acc, imgs in self.containers
                for img in imgs]

    def container_choices(self) -> list[str]:
        """"engine|image" of each OpenFOAM image that this account can run."""
        return [f"{tool}|{img}" for tool, acc, imgs in self.containers
                if acc == "ok" for img in imgs]

    def default_container(self) -> str:
        """"" = the native OpenFOAM; else the best image (no native one)."""
        if self.native:
            return ""
        import fs_backend
        return fs_backend.pick_container(self.container_choices())

    def lines(self) -> list:
        """The result for the person, line by line."""
        if not self.ok:
            return [tr("The check failed: {error}", error=self.error)]
        out = [tr("Server: {name} ({os}, {kernel})", name=self.hostname,
                  os=self.os or "?", kernel=self.kernel or "?"),
               tr("Cores: {n}   Memory: {mem:.1f} GB   Free disk in the "
                  "home folder: {disk:.0f} GB", n=self.nproc, mem=self.mem_gb,
                  disk=self.disk_free_gb)]
        nat = self.native
        if nat:
            out.append(tr("OpenFOAM (native): {version}  ({bashrc})",
                          version=nat[1], bashrc=nat[0]))
        else:
            out.append(tr("OpenFOAM (native): not found"))
        for tool, acc, imgs in self.containers:
            if acc != "ok":
                out.append(tr("{tool}: installed, but this account cannot "
                              "use it", tool=tool))
            elif imgs:
                out.append(tr("{tool} images: {images}", tool=tool,
                              images=", ".join(imgs)))
            else:
                out.append(tr("{tool}: no OpenFOAM image", tool=tool))
        if self.tailscale:
            out.append(tr("Tailscale on the server: {version}",
                          version=self.tailscale))
        return out

    def warnings(self) -> list:
        out = []
        if self.ok and not self.native and not self.container_choices():
            out.append(tr("OpenFOAM was not found. Install it with the "
                          "setup script (step «OpenFOAM»), then check "
                          "again."))
        if self.ok and 0 < self.disk_free_gb < 10:
            out.append(tr("Less than 10 GB free in the home folder: a large "
                          "mesh does not fit."))
        return out


def parse_probe(text: str) -> ServerInfo:
    info = ServerInfo()
    if "OFS_PROBE=1" not in (text or ""):
        info.error = (text or "").strip()[-300:] or "no answer"
        return info
    for line in text.splitlines():
        key, sep, val = line.strip().partition("=")
        if not sep:
            continue
        val = val.strip()
        if key == "HOSTNAME":
            info.hostname = val
        elif key == "OS":
            info.os = val
        elif key == "KERNEL":
            info.kernel = val
        elif key == "NPROC":
            info.nproc = int(val) if val.isdigit() else 0
        elif key == "MEM_KB":
            info.mem_gb = int(val) / 1024 ** 2 if val.isdigit() else 0.0
        elif key == "DISK_FREE_KB":
            info.disk_free_gb = int(val) / 1024 ** 2 if val.isdigit() else 0.0
        elif key == "FOAM_NATIVE":
            bashrc, _, ver = val.partition("|")
            info.foam.append((bashrc, ver.strip()))
        elif key == "CONTAINER":
            tool, _, rest = val.partition("|")
            acc, _, imgs = rest.partition("|")
            info.containers.append((tool, acc, imgs.split()))
        elif key == "TAILSCALE":
            info.tailscale = val
    info.ok = "OFS_PROBE_END=1" in text
    if not info.ok:
        info.error = "the check did not end"
    return info


def probe_server(t: Target, timeout: float = 90) -> ServerInfo:
    """Read what the server has (only reads; changes nothing there)."""
    # The script goes in the command, not on stdin: with stdin, a child
    # program (docker, the OpenFOAM bashrc) can read the rest of the script.
    rc, out, err = _run(t.argv("exec bash -c " + shlex.quote(PROBE_SCRIPT)
                               + " </dev/null"), timeout=timeout)
    info = parse_probe(out)
    if not info.ok and not info.error.strip():
        info.error = (err or "").strip()[-300:] or f"exit {rc}"
    elif not info.ok and "OFS_PROBE=1" not in out:
        info.error = (err or out).strip()[-300:] or f"exit {rc}"
    return info


# --------------------------------------------------------------------------
# The setup script for a NEW server
# --------------------------------------------------------------------------

def setup_script(tailscale: bool = True, ssh_server: bool = True,
                 openfoam: str = "apt", public_key: str = "") -> str:
    """A bash script that prepares an empty Linux server.

    openfoam: "apt" (the openfoam.com package openfoam2512-default, Debian
    and Ubuntu), "docker" (the image opencfd/openfoam-run:2512), "none".
    public_key: the public key of this computer; the script adds it to
    ~/.ssh/authorized_keys of the account that runs the script. Never a
    private key, an auth key or a password.
    """
    if openfoam not in ("apt", "docker", "none"):
        raise ValueError(openfoam)
    s = [
        "#!/usr/bin/env bash",
        "# OpenFOAM Studio: setup of a new Linux server.",
        "# Run it on the SERVER as the account that will run the cases:",
        "#     bash ofs-server-setup.sh",
        "# It uses sudo for the packages. It is safe to run it again.",
        "set -euo pipefail",
        "",
        'say() { printf "\\n=== %s ===\\n" "$*"; }',
        "if command -v apt-get >/dev/null 2>&1; then PM=apt",
        "elif command -v dnf >/dev/null 2>&1; then PM=dnf",
        "else PM=other; fi",
        'echo "Package manager: $PM"',
    ]
    if ssh_server:
        s += [
            "",
            'say "SSH server"',
            'if [ "$PM" = apt ]; then',
            "  sudo apt-get update",
            "  sudo apt-get install -y openssh-server",
            "  sudo systemctl enable --now ssh",
            'elif [ "$PM" = dnf ]; then',
            "  sudo dnf install -y openssh-server",
            "  sudo systemctl enable --now sshd",
            "else",
            '  echo "Install and start the OpenSSH server with the package '
            'manager of this system."',
            "fi",
        ]
    if public_key:
        key = public_key.strip().splitlines()[0]
        if not re.match(r"^(ssh-ed25519|ecdsa-sha2-\S+|ssh-rsa) [A-Za-z0-9+/=]+",
                        key):
            raise ValueError("not a public key")
        s += [
            "",
            'say "Key of the OpenFOAM Studio computer"',
            "KEY=" + shlex.quote(key),
            "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys",
            'grep -qxF "$KEY" ~/.ssh/authorized_keys || '
            'echo "$KEY" >> ~/.ssh/authorized_keys',
            'echo "The key is in ~/.ssh/authorized_keys"',
        ]
    if tailscale:
        s += [
            "",
            'say "Tailscale"',
            "if ! command -v tailscale >/dev/null 2>&1; then",
            "  curl -fsSL " + TAILSCALE_INSTALL_SH + " | sh",
            "fi",
            "# Prints a login address: open it in a browser and log in with",
            "# the same account as on your computer.",
            "sudo tailscale up",
        ]
    if openfoam == "apt":
        s += [
            "",
            f'say "OpenFOAM v{OPENFOAM_VERSION} (openfoam.com package)"',
            'if [ "$PM" = apt ]; then',
            "  curl -fsSL " + OPENFOAM_APT_REPO + " | sudo bash",
            "  sudo apt-get update",
            f"  sudo apt-get install -y openfoam{OPENFOAM_VERSION}-default",
            "else",
            '  echo "The openfoam.com packages are for Debian and Ubuntu. Use '
            'the docker image (see the guide) on this system."',
            "fi",
        ]
    elif openfoam == "docker":
        s += [
            "",
            f'say "OpenFOAM v{OPENFOAM_VERSION} (docker image)"',
            "if ! command -v docker >/dev/null 2>&1 && "
            "! command -v podman >/dev/null 2>&1; then",
            "  curl -fsSL https://get.docker.com | sudo sh",
            '  sudo usermod -aG docker "$USER"',
            "fi",
            "if command -v docker >/dev/null 2>&1; then",
            f"  sudo docker pull {OPENFOAM_IMAGE}",
            "else",
            f"  podman pull docker.io/{OPENFOAM_IMAGE}",
            "fi",
        ]
    s += [
        "",
        'say "Done"',
        'echo "Account: $(whoami)"',
        "if command -v tailscale >/dev/null 2>&1; then",
        '  echo "Tailscale name: $(hostname)   address: '
        '$(tailscale ip -4 2>/dev/null | head -1)"',
        "fi",
        'echo "Free disk in $HOME: $(df -Ph "$HOME" | awk \'NR==2{print $4}\')"',
        'echo "Type this account and this name in the wizard of OpenFOAM '
        'Studio."',
        "",
    ]
    return "\n".join(s)
