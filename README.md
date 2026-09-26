# storychat-hermes

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) platform plugin that connects your
own Hermes to your [StoryChat](https://storychat.app) account. In a chat with a character **you
created**, pick **My Hermes Agent** in the model picker and your Hermes replies as that character,
with its own memory and skills. Replies cost 0 Storypoints.

Your gateway dials **out** to StoryChat over one websocket. Nothing on your computer is exposed to
the internet, and your LLM keys and tools never leave your machine. Your approval PIN is stored only
in your `.env`: when you approve a command from StoryChat you type the PIN there, and it passes
through StoryChat's servers to your agent without being stored or logged there. Use a PIN you use
nowhere else.

## Requirements

- Hermes Agent v0.21.5 (tag `v2026.9.24`) or newer, with the gateway running.
- A StoryChat account and at least one character you created.

## Setup

### 1. Get a pairing token

On StoryChat open **Advanced Settings → Connect Hermes Agent** (`storychat.app/chat/hermes`) and
click **Generate pairing token**. The token is shown once. The page also shows a ready-made `.env`
block with your token and userId filled in.

### 2. Install and enable the plugin

Non-bundled plugins are opt-in:

```bash
hermes plugins install albertahn/storychat-hermes
hermes plugins enable storychat-hermes
```

Or clone it by hand with
`git clone https://github.com/albertahn/storychat-hermes ~/.hermes/plugins/storychat-hermes`
and run the `enable` command above.

### 3. Add your settings to `~/.hermes/.env`

The StoryChat page pre-fills your token and userId:

```
# Required. From /chat/hermes (pre-filled there).
STORYCHAT_HERMES_TOKEN=sch_…
# Pre-filled on /chat/hermes.
STORYCHAT_ALLOWED_USERS=<storychat userId>
# Fill in yourself, 6+ digits. Needed to approve commands from StoryChat.
STORYCHAT_APPROVAL_PIN=
# Empty = chat only (default). To opt in, e.g. STORYCHAT_TOOLSETS=web,file,terminal
# MCP servers stay off unless you name them here.
STORYCHAT_TOOLSETS=
# STORYCHAT_URL defaults to wss://prod-rail.storychat.app/api/v1/hermes/connect
# (On non-prod builds /chat/hermes writes STORYCHAT_URL=… here, plus
#  STORYCHAT_DEV_INSECURE_LOCALHOST=1 when the URL is ws://localhost.)
```

### 4. Turn on streaming in `~/.hermes/config.yaml`

Merge these keys into your existing `~/.hermes/config.yaml` instead of pasting a second
`streaming:` or `display:` section, because YAML keeps only the last copy of a key and the
settings in the earlier section would be silently lost. Keep the `display.platforms.storychat`
settings exactly as shown — see "Progress messages stay off" below.

```yaml
streaming:
  enabled: true              # Hermes default is off; without it replies arrive whole at turn end.
                             # This is global: to keep another platform unchanged, add
                             # display.platforms.<that platform>.streaming: false
display:
  platforms:
    storychat:
      tool_progress: off
      long_running_notifications: off
      interim_assistant_messages: false
```

### 5. Restart the gateway

Run `hermes gateway restart` (or stop and start `hermes gateway run` if you run it in a terminal).
The StoryChat page switches to **Online** and lists the toolsets your agent exposes to StoryChat.

### Keeping old chats attached

Each storychat is one persistent Hermes session. Hermes drops a session mapping after 90 idle days
(`session_store_max_age_days`, default 90). Setting it to 0 keeps old storychats attached to their
Hermes history. In Hermes v0.21.5 this key is read from `~/.hermes/gateway.json`, not
`config.yaml`:

```json
{"session_store_max_age_days": 0}
```

## Tools, approvals and the PIN

- **Chat only by default.** With `STORYCHAT_TOOLSETS` empty the plugin gives Hermes the override
  `["no_mcp"]`: no core toolset and no MCP server reaches StoryChat. Hermes then logs this line once,
  which is expected:

  `` platform 'storychat' has no valid toolsets configured (unknown name(s): no_mcp) - tools will be unavailable. Run `hermes tools` to reconfigure. See issue #38798. ``

  Do not run `hermes tools` to "fix" it for storychat — the empty set is intended.
- **Startup self-check.** Before connecting, the plugin resolves the toolsets exactly as Hermes will.
  In chat-only mode only `x_search` (when xAI credentials exist) and `context_engine` (when a
  non-default context engine is set) are tolerated; neither can touch your files or run commands.
  Anything else (for example a plugin toolset that is on by default) makes the plugin refuse to
  connect and log which toolsets leaked. Turn them off for StoryChat by listing them under
  `known_plugin_toolsets.storychat` in `config.yaml` (or add them to `agent.disabled_toolsets`),
  then restart the gateway. The same check also runs before every turn, because Hermes re-reads
  `config.yaml` on each turn: if the toolsets or display settings changed since startup, the turn
  is refused (and StoryChat shows an error) until you fix `config.yaml` and restart the gateway.
- **Opting in.** `STORYCHAT_TOOLSETS=web,file,terminal` gives StoryChat those toolsets, plus any
  plugin toolsets that are on by default. The startup self-check sends the full list to StoryChat,
  and the `/chat/hermes` page shows it.
  `clarify` is always removed (StoryChat cannot answer its typed question) — and if it comes back
  through a bundle such as `hermes-cli`, `coding` or `all`, the plugin refuses to connect, so name
  toolsets one by one instead of a bundle, or add `clarify` to `agent.disabled_toolsets`. An unknown
  toolset name also makes the plugin refuse to connect. MCP servers stay off unless you name them.
  The plugin warns once that imported or cloned character cards can carry instructions.
- **Progress messages stay off.** The `display.platforms.storychat` settings from step 4
  (`tool_progress: off`, `interim_assistant_messages: false`) must stay as shown, and
  `thinking_progress` must stay off too (it defaults to off, so just don't turn it on). Hermes would
  otherwise send those progress messages to StoryChat as the character's reply, so the plugin
  refuses to connect — and refuses turns if these settings change later — until they're off again.
- **Approvals.** A dangerous command shows an approval card in StoryChat: Approve once, This
  session, Always or Deny. Every choice except Deny needs `STORYCHAT_APPROVAL_PIN`. The PIN is stored
  only in your `.env`; you type it into StoryChat to approve, and it passes through StoryChat's
  servers to your agent without being stored or logged there, so use a PIN you use nowhere else.
  After 5 wrong PINs in a row, remote approvals stay locked until you restart the
  gateway. Without a PIN (or with one shorter than 6 digits) remote approvals are refused. If nobody
  answers, Hermes denies the command after `approvals.timeout` (default 300s). Hermes shows no card
  for a command that matches your `command_allowlist` (for example one you approved with Always) or
  that `approvals.mode: smart` judges safe; those run without the PIN.
- **Approvals must stay on.** With `STORYCHAT_TOOLSETS` set, the plugin refuses to connect while
  Hermes approvals are off (`approvals.mode: off` in `config.yaml`, or the gateway runs with `--yolo`
  / `HERMES_YOLO_MODE`), and refuses turns if they are turned off later (including `/yolo` for that
  chat's session), because every dangerous command would then run without your PIN. `manual` (the
  default) and `smart` are fine.
- **External memory providers.** If `memory.provider` in `config.yaml` names an external provider
  (for example `honcho`), Hermes sends every StoryChat turn to it, where your other sessions can
  recall it, whatever the toolsets: chat-only mode cannot stop that, and text a character card can
  steer would end up in the memory your tool-enabled sessions read. So the plugin refuses to
  connect (and refuses turns if you add one later) unless you set `STORYCHAT_ALLOW_MEMORY_PROVIDER=1`
  in `~/.hermes/.env`. With it set, the plugin logs a warning once and connects. The built-in
  `MEMORY.md` / `USER.md` store is always fine.
- **Chat text never controls the gateway.** Messages from StoryChat can't run `/approve`, `/yolo`,
  `/new` or any other command, and can't answer an approval.

## Troubleshooting

| Log message | What happened | What to do |
|---|---|---|
| `another client took over — rotate your token` | Another Hermes connected with the same token. | Generate a new token on `/chat/hermes` and update `.env`, then run `hermes gateway restart`. |
| `token invalid or revoked — generate a new one at storychat.app/chat/hermes` | The token is wrong, rotated or revoked. | Generate a new token and update `.env`, then run `hermes gateway restart`. |
| `STORYCHAT_ALLOWED_USERS must contain your StoryChat userId <id> — copy it from storychat.app/chat/hermes` | The token belongs to StoryChat account `<id>`, and `STORYCHAT_ALLOWED_USERS` does not list it, so the plugin refused to connect and stopped retrying. | Open `/chat/hermes` signed in to your own account and copy your userId into the existing `STORYCHAT_ALLOWED_USERS=` line, then run `hermes gateway restart`. If `<id>` is not your userId, the token is someone else's: generate your own token there instead. |
| `protocol mismatch — update storychat-hermes` | StoryChat speaks a newer protocol. | `hermes plugins update storychat-hermes`, then restart the gateway. |
| `unexpected redirect — check STORYCHAT_URL` | The relay URL answered with a redirect. | Remove or fix `STORYCHAT_URL`. |
| `proxy settings are invalid or need python-socks — check HTTPS_PROXY/WSS_PROXY/SOCKS_PROXY` | The proxy the gateway picked up (from `HTTPS_PROXY`, `WSS_PROXY` or `SOCKS_PROXY`, in either case, or the system proxy settings) is not a valid proxy URL, or it is a SOCKS proxy and `python-socks` is not installed where Hermes runs. The plugin stopped retrying. | Fix or unset that proxy variable for the gateway, or install `python-socks` into the Python environment Hermes runs in, then run `hermes gateway restart`. |
| `chat-only mode, but these toolsets would still reach StoryChat: …` | The startup self-check found tools. | See "Startup self-check" above. |
| `STORYCHAT_TOOLSETS names unknown toolset(s): …; leave STORYCHAT_TOOLSETS empty for chat only` | `STORYCHAT_TOOLSETS` named something Hermes doesn't recognize as a toolset key or an enabled MCP server. | Fix the toolset names in `.env` (or leave `STORYCHAT_TOOLSETS` empty for chat only), then restart the gateway. |
| `StoryChat accepts at most 100 toolsets with names of up to 128 characters, but STORYCHAT_TOOLSETS resolves to …` | `STORYCHAT_TOOLSETS` names so many MCP servers, or an MCP server with such a long name, that StoryChat's relay would refuse the connection. | Name fewer toolsets or MCP servers in `.env`, or give the MCP server a shorter name in `config.yaml`, then restart the gateway. |
| `STORYCHAT_TOOLSETS brings in the clarify toolset (through a bundle such as hermes-cli, coding or all), and StoryChat cannot answer clarify questions, so turns would hang.` | A bundle in `STORYCHAT_TOOLSETS` brought `clarify` back in. | Name toolsets one by one (for example `web,file,terminal`) instead of a bundle, or add `clarify` to `agent.disabled_toolsets` in `config.yaml`, then restart the gateway. |
| `STORYCHAT_TOOLSETS gives StoryChat tools, but Hermes approvals are off …` | `approvals.mode` is `off` in `config.yaml`, or the gateway runs with `--yolo` / `HERMES_YOLO_MODE`, so dangerous commands would run without your PIN. | Set `approvals.mode` to `manual` or `smart` (or stop using `--yolo`), or leave `STORYCHAT_TOOLSETS` empty for chat only, then restart the gateway. |
| `memory.provider is …: Hermes sends every StoryChat turn, including text a character card can steer, to that memory provider, …` | `memory.provider` in `config.yaml` names an external memory provider, and `STORYCHAT_ALLOW_MEMORY_PROVIDER=1` is not set. | Set `memory.provider` back to the built-in store (remove it, or set it to `builtin`), or add `STORYCHAT_ALLOW_MEMORY_PROVIDER=1` to `~/.hermes/.env` if you accept that StoryChat turns go to that provider (see "External memory providers" above), then run `hermes gateway restart`. |
| `these StoryChat display settings are still on: …` | `tool_progress`, `interim_assistant_messages` or `thinking_progress` under `display.platforms.storychat` would leak progress text into StoryChat as the character's reply. | Set them off under `display.platforms.storychat` in `config.yaml` (see "Progress messages stay off" above), then restart the gateway. |
| `refused turn …: the StoryChat toolsets changed since startup; fix config.yaml and restart the gateway` | `config.yaml` changed after the gateway started and now gives StoryChat toolsets it must not get, so the per-turn self-check refused this turn. | Fix `config.yaml` back to a safe configuration and run `hermes gateway restart`. |
| `refused turn …: the StoryChat display settings changed since startup; fix config.yaml and restart the gateway` | `config.yaml` changed after the gateway started and turned a `display.platforms.storychat` progress setting back on. | Set it off again (see "Progress messages stay off" above) and run `hermes gateway restart`. |
| `refused turn …: Hermes approvals are off (approvals.mode: off, --yolo or /yolo); fix config.yaml and restart the gateway` | With tools opted in, approvals were turned off after startup (`approvals.mode: off`, or `/yolo` in that chat's Hermes session). | Turn approvals back on and run `hermes gateway restart`. |
| `refused turn …: an external memory provider is on (memory.provider) without STORYCHAT_ALLOW_MEMORY_PROVIDER=1; fix config.yaml and restart the gateway` | `memory.provider` was set to an external provider after the gateway started. | Remove it again, or set `STORYCHAT_ALLOW_MEMORY_PROVIDER=1` in `~/.hermes/.env`, then run `hermes gateway restart`. |
| `refused turn …: could not verify the StoryChat toolsets/display settings; fix config.yaml and restart the gateway` | The per-turn self-check could not read the settings; the error line just before it names the exception (often a YAML mistake in `config.yaml`). | Fix `config.yaml` and run `hermes gateway restart`. |
| `refused turn …: Hermes is still finishing the previous turn` | A message arrived while Hermes was still wrapping up the previous reply in that chat (or unwinding a stopped one), so StoryChat showed an error for it. | Nothing is wrong: wait a moment and send the message again. |
| `StoryChat pairing token already in use …` | Another Hermes gateway on this computer (another profile, or the same one started twice) is already connected with this token. | Stop the other gateway (the message names it), then run `hermes gateway restart`. |
| `StoryChat is rate limiting this connection; retrying in 60s or more` | StoryChat closed the connection with code 4429, or refused it with HTTP 429, because of too many connection attempts or messages. | Nothing: the plugin backs off from 60 seconds and reconnects on its own. If it keeps happening, make sure only one gateway uses this token. |

A network blip or a StoryChat redeploy is handled for you: the plugin reconnects after 1–75
seconds, and after 10 failed attempts Hermes' own watcher takes over. A reply that was running when
the connection dropped is stopped.

## Privacy

Chats pass through StoryChat's servers, as all StoryChat chats do. Hermes also keeps its own
transcript on your computer. The plugin never logs your token, your PIN, message text or the
character card. If you set `STORYCHAT_ALLOW_MEMORY_PROVIDER=1`, Hermes also sends your StoryChat
turns to the external memory provider named in `memory.provider`, which may be a third-party
service (see "External memory providers" above).

## Development

Keep the virtualenv outside the repo: the repo root is the plugin directory Hermes scans. Hermes
refuses pip wheel builds, so install it editable with `uv` instead:

```bash
python3.11 -m venv ~/.venvs/storychat-hermes
git clone --depth 1 --branch v2026.9.24 https://github.com/NousResearch/hermes-agent ~/.venvs/storychat-hermes-src
uv pip install --python ~/.venvs/storychat-hermes/bin/python -e ~/.venvs/storychat-hermes-src
~/.venvs/storychat-hermes/bin/python -m pip install -r requirements-dev.txt
~/.venvs/storychat-hermes/bin/python -m pytest tests -q
~/.venvs/storychat-hermes/bin/hermes plugins validate .
```

Always pass `tests` to pytest: the repo root is itself the plugin package, and `tests/pytest.ini`
keeps pytest from importing it. CI runs the suite against Hermes v0.21.5 (Python 3.11 and 3.13)
and Hermes `main` (Python 3.14).

## License

MIT
