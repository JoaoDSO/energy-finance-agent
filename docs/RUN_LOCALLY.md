# Run locally

Start with the setup commands in the [README](../README.md). `fetch` downloads the configured public reports, and `index` extracts searchable pages. Inspect tables visually before relying on their values; the original case ledgers were curated and checked by the builder.

To explore the source tools without an agent call:

```powershell
.\.venv\Scripts\python.exe src/workflow.py search "cash flow"
.\.venv\Scripts\python.exe src/workflow.py --case vestas_cash_conversion_2026 search "working capital"
```

For an agent run, sign in to Codex CLI using your existing subscription, then start a fresh on-demand run:

```powershell
codex login
.\.venv\Scripts\python.exe src/workflow.py run --fresh --research-client codex
```

For the Vestas case, add `--case vestas_cash_conversion_2026` before `run`. If using free Claude Desktop, start a fresh run without `--research-client codex`; the controller prepares a task and waits for you to use the local MCP connector in Claude. Merge the relevant entry from the [connector template](../config/claude_desktop.example.json) into your Claude Desktop configuration, replacing both absolute path placeholders with your own paths. After Claude submits a validated research pack, resume with the run ID shown by the controller. The free Claude session is user initiated; the controller cannot launch it automatically.

Run `workflow.py status` to see the review queue. A model pass stops for a human decision; it never authorises publication or financial claims by itself. The short files under `case_studies/` summarise the local reviewed cases; the full agent traces are not part of this repository.
