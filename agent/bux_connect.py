#!/opt/bux/venv/bin/python
"""bux-connect — gh-login-style wrapper for Composio service connections.

Composio brokers OAuth into ~250 SaaS toolkits (Gmail, Calendar, Drive,
Slack, Linear, …) and hands the agent stable connected-account IDs to use
afterwards. The flow has two browser-OAuth legs:

  1. authenticate the bux box to your Composio account (one-time per box)
  2. authorize each toolkit (one-time per service)

This wrapper makes both legs feel like `gh auth login`: each prints a URL,
forwards it to Telegram via tg-send when running under the bot, then waits
for the connection to flip ACTIVE before reporting back.

Usage:
    bux-connect                          status: api key + stored auth configs
    bux-connect set-key <api_key>        store the Composio API key
    bux-connect set-auth-config <toolkit> <auth_config_id>
                                         remember <toolkit> → <ac_id>
    bux-connect <toolkit>                kick off a connection, send URL,
                                         poll until ACTIVE (alias: connect)
    bux-connect list                     show the user's connected accounts
    bux-connect login                    wrap `composio login --no-browser`
                                         and forward the URL to TG

State lives at /home/bux/.secrets/composio.env (mode 600, .env-style):

    COMPOSIO_API_KEY=uak_…
    COMPOSIO_USER_ID=bux
    COMPOSIO_AUTH_CONFIG_GMAIL=ac_…
    COMPOSIO_AUTH_CONFIG_GOOGLECALENDAR=ac_…

Auth config IDs are minted in the Composio dashboard
(https://platform.composio.dev → Auth Configs → <toolkit> → use Composio's
managed credentials). Once stored here, future `bux-connect <toolkit>` runs
need no arguments.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

SECRETS_DIR = pathlib.Path("/home/bux/.secrets")
ENV_FILE = SECRETS_DIR / "composio.env"
DASHBOARD_URL = "https://platform.composio.dev"


def load_env() -> dict[str, str]:
    """Parse the .env-style secrets file. Empty dict if missing."""
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def save_env(env: dict[str, str]) -> None:
    """Atomically rewrite the secrets file at mode 600."""
    SECRETS_DIR.mkdir(mode=0o700, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in env.items())
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text(body)
    tmp.chmod(0o600)
    tmp.replace(ENV_FILE)


def notify(text: str) -> None:
    """Print to stdout and, when running under the TG bot (TG_THREAD_ID set),
    also forward to the chat via tg-send so the URL lands back in the same
    forum topic the user asked from."""
    print(text)
    sys.stdout.flush()
    if shutil.which("tg-send") and os.environ.get("TG_THREAD_ID"):
        try:
            subprocess.run(["tg-send", text], check=True, timeout=15)
        except Exception as e:
            print(f"(tg-send failed: {e})", file=sys.stderr)


def _composio(api_key: str):
    try:
        from composio import Composio
    except ImportError:
        print(
            "composio sdk not installed. run:\n"
            "  /opt/bux/venv/bin/pip install composio",
            file=sys.stderr,
        )
        sys.exit(1)
    return Composio(api_key=api_key)


def cmd_status(env: dict[str, str]) -> int:
    if not env.get("COMPOSIO_API_KEY"):
        print("not authenticated.")
        print(f"  1. sign up / get a key at {DASHBOARD_URL}/settings/api-keys")
        print("  2. bux-connect set-key <api_key>")
        return 1
    print(f"composio: ok (user_id={env.get('COMPOSIO_USER_ID', 'bux')})")
    auth_configs = {
        k.removeprefix("COMPOSIO_AUTH_CONFIG_").lower(): v
        for k, v in env.items()
        if k.startswith("COMPOSIO_AUTH_CONFIG_")
    }
    if auth_configs:
        print("auth configs:")
        for tk, ac in sorted(auth_configs.items()):
            print(f"  {tk}: {ac}")
    else:
        print("no auth configs yet. create one in the dashboard, then:")
        print("  bux-connect set-auth-config <toolkit> ac_…")
    return 0


def cmd_set_key(env: dict[str, str], key: str) -> int:
    env["COMPOSIO_API_KEY"] = key
    env.setdefault("COMPOSIO_USER_ID", "bux")
    save_env(env)
    print("api key saved to", ENV_FILE)
    return 0


def cmd_set_auth_config(env: dict[str, str], toolkit: str, ac_id: str) -> int:
    env[f"COMPOSIO_AUTH_CONFIG_{toolkit.upper()}"] = ac_id
    save_env(env)
    print(f"stored {toolkit} → {ac_id}")
    return 0


def cmd_connect(env: dict[str, str], toolkit: str) -> int:
    api_key = env.get("COMPOSIO_API_KEY")
    if not api_key:
        print("not authenticated. run: bux-connect set-key <api_key>", file=sys.stderr)
        return 1
    auth_config_id = env.get(f"COMPOSIO_AUTH_CONFIG_{toolkit.upper()}")
    if not auth_config_id:
        print(f'no auth_config_id stored for "{toolkit}".', file=sys.stderr)
        print(
            f"create one at {DASHBOARD_URL} (Auth Configs → {toolkit},\n"
            f"select Composio-managed credentials), then:\n"
            f"  bux-connect set-auth-config {toolkit} ac_…",
            file=sys.stderr,
        )
        return 1
    user_id = env.get("COMPOSIO_USER_ID", "bux")

    composio = _composio(api_key)
    try:
        req = composio.connected_accounts.initiate(
            user_id=user_id,
            auth_config_id=auth_config_id,
        )
    except Exception as e:
        print(f"initiate failed: {e}", file=sys.stderr)
        return 1

    redirect_url = getattr(req, "redirect_url", None) or getattr(req, "redirectUrl", None)
    if not redirect_url:
        print(f"no redirect_url returned: {req!r}", file=sys.stderr)
        return 1

    notify(
        f"connect {toolkit}: tap to authorize\n\n{redirect_url}\n\n"
        "(I'm waiting up to 10 min for it to flip ACTIVE.)"
    )

    try:
        account = req.wait_for_connection(timeout=600)
    except Exception as e:
        notify(f"connect {toolkit}: failed — {e}")
        return 1

    notify(f"connect {toolkit}: connected (account_id={getattr(account, 'id', '?')})")
    return 0


def cmd_list(env: dict[str, str]) -> int:
    api_key = env.get("COMPOSIO_API_KEY")
    if not api_key:
        print("not authenticated", file=sys.stderr)
        return 1
    composio = _composio(api_key)
    user_id = env.get("COMPOSIO_USER_ID", "bux")
    try:
        page = composio.connected_accounts.list(user_ids=[user_id])
    except Exception as e:
        print(f"list failed: {e}", file=sys.stderr)
        return 1
    items = getattr(page, "items", None) or list(page) if not isinstance(page, list) else page
    if not items:
        print("no connected accounts")
        return 0
    for acc in items:
        toolkit = getattr(acc, "toolkit_slug", None) or getattr(acc, "toolkit", "?")
        status = getattr(acc, "status", "?")
        print(f"{toolkit}: {getattr(acc, 'id', '?')} ({status})")
    return 0


def cmd_login(env: dict[str, str]) -> int:
    """Wrap `composio login --no-browser` and forward the printed URL to TG.

    The composio CLI handles the actual OAuth dance and writes the resulting
    API key to ~/.composio/. Caller still needs to run `set-key` afterwards
    if they want the key surfaced into our secrets file (the CLI keeps its
    own copy regardless).
    """
    if not shutil.which("composio"):
        print("composio CLI not installed.", file=sys.stderr)
        print(
            "either install it (npm i -g composio) or skip it: grab a key at\n"
            f"  {DASHBOARD_URL}/settings/api-keys\n"
            "then: bux-connect set-key <api_key>",
            file=sys.stderr,
        )
        return 1
    proc = subprocess.Popen(
        ["composio", "login", "--no-browser"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    sent_url = False
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if not sent_url:
            m = re.search(r"https?://\S+", line)
            if m:
                notify(f"composio login: tap to authorize\n\n{m.group(0)}")
                sent_url = True
    return proc.wait()


def main() -> int:
    args = sys.argv[1:]
    env = load_env()
    if not args or args[0] in ("status",):
        return cmd_status(env)
    if args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if args[0] == "set-key" and len(args) == 2:
        return cmd_set_key(env, args[1])
    if args[0] == "set-auth-config" and len(args) == 3:
        return cmd_set_auth_config(env, args[1], args[2])
    if args[0] == "list":
        return cmd_list(env)
    if args[0] == "login":
        return cmd_login(env)
    if args[0] == "connect" and len(args) == 2:
        return cmd_connect(env, args[1])
    if len(args) == 1:
        return cmd_connect(env, args[0])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
