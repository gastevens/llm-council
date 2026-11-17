# LLM Council

![llmcouncil](header.jpg)

The idea of this repo is that instead of asking a question to your favorite LLM provider (e.g. OpenAI GPT 5.1, Google Gemini 3.0 Pro, Anthropic Claude Sonnet 4.5, xAI Grok 4, eg.c), you can group them into your "LLM Council". This repo is a simple, local web app that essentially looks like ChatGPT except it uses OpenRouter to send your query to multiple LLMs, it then asks them to review and rank each other's work, and finally a Chairman LLM produces the final response.

In a bit more detail, here is what happens when you submit a query:

1. **Stage 1: First opinions**. The user query is given to all LLMs individually, and the responses are collected. The individual responses are shown in a "tab view", so that the user can inspect them all one by one.
2. **Stage 2: Review**. Each individual LLM is given the responses of the other LLMs. Under the hood, the LLM identities are anonymized so that the LLM can't play favorites when judging their outputs. The LLM is asked to rank them in accuracy and insight.
3. **Stage 3: Final response**. The designated Chairman of the LLM Council takes all of the model's responses and compiles them into a single final answer that is presented to the user.

## War Room fork: run on your subscriptions (no API keys)

This `war-room` branch replaces OpenRouter with the coding CLIs you are already
signed into, so the council runs on your ChatGPT, Claude and Grok plans:

| Member  | CLI | Sign in once with |
|---------|-----|-------------------|
| ChatGPT | [Codex CLI](https://github.com/openai/codex) (`codex exec`) | `codex login` (ChatGPT plan) |
| Claude  | [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude -p`) | `claude` then `/login` (Claude plan) |
| Grok    | Grok Build (`grok -p`) | `grok login` (grok.com plan) |

Each call runs as a one-shot subprocess in an empty temporary directory with
tools disabled (Codex: read-only sandbox), so members only answer and never
touch your files. Usage counts against each plan's normal limits.

Run it:

```bash
uv sync
(cd frontend && npm install)
./start.sh            # then open http://localhost:5173
# or, headless:
uv run python -m backend.ask "Should we self-host email?"
```

Configure with environment variables (or `.env`):

```bash
COUNCIL_MEMBERS=chatgpt,claude,grok      # add a model override: claude:claude-opus-5
COUNCIL_CHAIRMAN=claude                  # who writes the final verdict
COUNCIL_TITLE_MEMBER=grok                # who names conversations
COUNCIL_TIMEOUT=300                      # seconds per call
# Optional OpenRouter members still work:
# COUNCIL_MEMBERS=chatgpt,claude,grok,openrouter:google/gemini-3-pro-preview
# OPENROUTER_API_KEY=sk-or-v1-...
```

A full three-stage round with three members takes about a minute.

## War Room in Slack

Ask the council from Slack. The bot runs locally on the Mac (Slack
[Bolt for Python](https://docs.slack.dev/tools/bolt-python/) in Socket Mode,
so there is no public URL or server to host) and uses the same subscription CLIs.

- `@War Room <question>` in any channel the bot is in (e.g. `#war-room`), or `/warroom <question>`.
- The bot replies in a thread: "Convening the council…", then each member's answer
  as it finishes (one message per member), then the peer-review summary (average
  rank plus a shortened critique from each reviewer), then the chairman's verdict.
  Long answers are converted to Slack formatting and split into several messages.
- Reply in that thread (no mention needed) to ask a follow-up; earlier questions and
  verdicts from the thread are passed to the council as context.
- If a member fails or times out, the thread says so and the council carries on.
  One council runs at a time; extra requests queue (`WARROOM_MAX_CONCURRENT`).
- `/warroom roster` shows the council with buttons to enable/disable, set the chair,
  test, or remove a member. Subcommands (all ephemeral):
  `/warroom add NAME provider [model] [color]`, `remove NAME`, `enable|disable NAME`,
  `chair NAME`, `title NAME`, `models [provider]`, `test NAME`, `reset`, `help`.
  Edits write `~/.config/war-room/council.json` and apply to the next council in
  both Slack and the TUI. Set `WARROOM_ADMINS` to a comma-separated list of Slack
  user IDs to limit who can edit (unset = anyone in the workspace).
- Context: `/warroom context ~/Documents/Proposal` makes the council read that folder
  (on the Mac running the bot) for every question in the channel; `/warroom context`
  shows it, `/warroom context off` removes it, `--folder` / `--inline` force a mode.
  Files attached to an @mention or a thread reply are downloaded and become that
  thread's context (they win over the channel folder; an @mention with only files asks
  for a review). See [Context folders](#context-folders).

### 1. Create the Slack app (one time)

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**.
2. Pick your workspace, choose YAML, paste [`slack/manifest.yaml`](slack/manifest.yaml), then **Create**.
   If the app already exists, open it → **App Manifest**, paste the updated YAML, and **Save**.
   (Roster buttons need Interactivity enabled; Socket Mode still needs no Request URL.)
   The manifest now includes the `files:read` scope (attachments as context). Adding a scope
   means Slack asks you to **reinstall**: after saving the manifest, open **Install App** →
   **Reinstall to Workspace** → **Allow**. The bot token normally stays the same; if Slack shows
   a new one, update `SLACK_BOT_TOKEN` in `.env`.
3. **Install App** (left sidebar) → **Install to Workspace** → **Allow**.
   Copy the **Bot User OAuth Token** (`xoxb-…`).
4. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**.
   Name it e.g. `socket`, add the scope `connections:write`, **Generate**, and copy the token (`xapp-…`).

### 2. Add the tokens

```bash
cp .env.example .env      # .env is git-ignored
# edit .env:
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

### 3. Start it

```bash
./slack.sh                                  # stays connected; Ctrl+C to stop
./slack.sh --dry-run "Should we self-host email?"   # no Slack: print the posts it would make
./slack.sh --fake --dry-run "test"          # canned answers, no CLI calls (formatting check)
```

Then in Slack create `#war-room` (or use any channel) and invite the bot:
`/invite @War Room`. Mention it with a question to start.

To run it at login, install the LaunchAgent template (logs go to
`~/Library/Logs/llm-council-warroom.log`):

```bash
sed -e "s#__REPO__#$PWD#g" -e "s#__HOME__#$HOME#g" \
  slack/com.llm-council.warroom.plist.template > ~/Library/LaunchAgents/com.llm-council.warroom.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.llm-council.warroom.plist
# stop: launchctl bootout gui/$(id -u)/com.llm-council.warroom
```

Tests for the Slack formatting and wiring: `uv run python -m unittest discover -s tests`.

## War Room in the terminal (TUI + Mac app)

```bash
uv run python -m backend.tui           # ask at the bottom; stages render live
uv run python -m backend.tui --fake    # canned answers, no CLI calls
uv run python -m backend.tui --ask "Should we self-host email?"
```

Stage 1 shows one tab per member, Stage 2 the peer-ranking table with
collapsible critiques, Stage 3 the chairman's verdict. Follow-ups in the same
session include the earlier questions and verdicts. Keys: `ctrl+n` new session,
`ctrl+s` save a Markdown transcript to `~/Documents/War Room/`, `ctrl+c` (or `q`
outside the input box) to quit. Sessions are also stored in `data/conversations/`.

### Choosing the council

The roster (members, chair, title writer) lives in `~/.config/war-room/council.json`
(override with `WARROOM_COUNCIL_CONFIG`); without it the `COUNCIL_*` env defaults
apply. The TUI, Slack bot, `ask.py` and the web backend read it at the start of
every question. Several members can share a CLI with different models.

- TUI: `ctrl+e` opens Council settings: `space` on/off, `a` add (name, provider,
  model, color), `del` remove, `[` / `]` reorder, `c` or the radio list to pick
  the chair (any member, on or off), `t` sends a tiny prompt to check the CLI,
  `ctrl+s` save, `esc` cancel. At least 2 members must be on.
- CLI: `uv run python -m backend.council_config list | add NAME -p claude -m opus -c pink |
  remove NAME | enable NAME | disable NAME | chair NAME | title NAME | up/down NAME |
  test NAME | models [PROVIDER] | reset`
- Slack: `/warroom roster` shows the council with edit buttons; `/warroom help` lists the text commands. Same file as the TUI.

Model ids: codex takes `-m` (the list comes from `~/.codex/models_cache.json`),
claude takes `--model` aliases `opus`/`sonnet`/`haiku` or a full `claude-…`
name, grok takes `-m` (`grok models` lists them), openrouter needs a model id.

### Context folders

Give the council a folder (or a single file) and the members answer about its contents,
citing file paths; reviewers and the chair get the files too.

- TUI: `uv run python -m backend.tui --context ~/Documents/Proposal`, or type
  `/context PATH` in the input (`/context` shows it, `/context off` drops it,
  add `--folder` / `--inline` to force a mode). The bar under the title shows
  folder · files · size · mode. A new session (`ctrl+n`) clears it. Saved
  transcripts record the folder and file list for each question.
- Mac app: drop a folder on the War Room icon, or `open -a "War Room" ~/Documents/Proposal`
  (or `open -a "War Room" --args --context /abs/path`).
- CLI: `uv run python -m backend.ask --context ~/Documents/Proposal "What are the weak points?"`
  (`--context-mode inline|folder|auto`).
- Slack: `/warroom context PATH` per channel, or attach files (above).

What gets read: text files of any kind (code, Markdown, JSON, CSV, YAML, logs…),
PDFs (via `pdftotext` from poppler, or `pypdf` if installed) and `.docx`. Skipped:
hidden files/folders, `.gitignore`d files when the folder is in a git repo,
`node_modules`/build/venv folders, lockfiles, binaries and images; the skipped list
is shown to the members.

Two modes (`auto` picks):

1. **inline** (default): the extracted text is pasted into the prompt with a
   `===== FILE: path =====` header per file, so every member sees the same material.
   Budget `COUNCIL_CONTEXT_MAX_CHARS` (default 400,000 characters, roughly 100k tokens),
   at most `COUNCIL_CONTEXT_MAX_FILES` (400) files, each file capped at
   `COUNCIL_CONTEXT_MAX_FILE_CHARS` (150,000). Stage 2 reviewers and the chair get the
   full text when it's under `COUNCIL_CONTEXT_REVIEW_CHARS` (150,000), otherwise just
   the file list.
2. **folder access** (automatic when over budget, or forced): each member runs with
   the folder as its working directory and read-only file tools, and opens what it needs.
   Codex: `--sandbox read-only --cd FOLDER`, web search off (it reads with shell
   commands; the sandbox blocks writes and network). Claude: `--tools Read,Glob,Grep`,
   `--permission-mode dontAsk`, Bash/Edit/Write/Web disallowed. Grok: `--tools
   read_file,list_dir,grep --sandbox read-only --permission-mode dontAsk
   --disable-web-search`. OpenRouter members have no file tools, so they get the inline
   text cut to the budget. Calls get `COUNCIL_TIMEOUT` × `COUNCIL_CONTEXT_TIMEOUT_FACTOR`
   (default 2). Reviewers and the chair also run in the folder and may open files to
   check citations.

`COUNCIL_CONTEXT_MODE=auto|inline|folder` sets the default. Note: Codex's and Grok's
read-only sandboxes block writes but can still *read* outside the folder; Claude is
limited to the folder. Only point folder mode at material you're happy for the
models to see, and keep `WARROOM_ADMINS` set when the Slack bot is in shared channels.

Slack safety: `/warroom context PATH` is limited to `WARROOM_ADMINS` (when set),
the path must be inside `WARROOM_CONTEXT_ROOTS` (comma-separated, default your home
folder) and can't go through hidden folders (`~/.ssh`, `~/.config`…). Attachments
are saved under `~/Library/Caches/war-room/slack-files/` (pruned after 14 days),
max `WARROOM_MAX_FILE_MB` (25) each.

`macos/build-app.sh` (re)builds `~/Applications/War Room.app`. It opens the TUI
in its own Ghostty window (War Room icon in the Dock) and quits completely when
that window closes: the TUI kills any running codex/claude/grok calls on hangup,
and the window's wrapper script sweeps anything left. Re-run the build script
after moving the repo. Extra arguments pass through:
`open -a "War Room" --args --fake`. The app's executable is a small Swift
program (`macos/WarRoomLauncher.swift`, compiled by the build script with the
Xcode command-line tools) so folders dropped on the icon become the context
folder; without `swiftc` it falls back to the plain launcher script.

## Vibe Code Alert

This project was 99% vibe coded as a fun Saturday hack because I wanted to explore and evaluate a number of LLMs side by side in the process of [reading books together with LLMs](https://x.com/karpathy/status/1990577951671509438). It's nice and useful to see multiple responses side by side, and also the cross-opinions of all LLMs on each other's outputs. I'm not going to support it in any way, it's provided here as is for other people's inspiration and I don't intend to improve it. Code is ephemeral now and libraries are over, ask your LLM to change it in whatever way you like.

## Setup

### 1. Install Dependencies

The project uses [uv](https://docs.astral.sh/uv/) for project management.

**Backend:**
```bash
uv sync
```

**Frontend:**
```bash
cd frontend
npm install
cd ..
```

### 2. Configure API Key

Create a `.env` file in the project root:

```bash
OPENROUTER_API_KEY=sk-or-v1-...
```

Get your API key at [openrouter.ai](https://openrouter.ai/). Make sure to purchase the credits you need, or sign up for automatic top up.

### 3. Configure Models (Optional)

Edit `backend/config.py` to customize the council:

```python
COUNCIL_MODELS = [
    "openai/gpt-5.1",
    "google/gemini-3-pro-preview",
    "anthropic/claude-sonnet-4.5",
    "x-ai/grok-4",
]

CHAIRMAN_MODEL = "google/gemini-3-pro-preview"
```

## Running the Application

**Option 1: Use the start script**
```bash
./start.sh
```

**Option 2: Run manually**

Terminal 1 (Backend):
```bash
uv run python -m backend.main
```

Terminal 2 (Frontend):
```bash
cd frontend
npm run dev
```

Then open http://localhost:5173 in your browser.

## Tech Stack

- **Backend:** FastAPI (Python 3.10+), async httpx, OpenRouter API
- **Frontend:** React + Vite, react-markdown for rendering
- **Storage:** JSON files in `data/conversations/`
- **Package Management:** uv for Python, npm for JavaScript
