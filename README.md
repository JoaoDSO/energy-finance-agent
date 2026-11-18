# Energy Finance Agent

I built this project to explore a question that connects my interests in energy markets, finance and business analytics: **what can public company reports tell us about the cash behind large renewable-energy investments?** A sustainable-finance conference prompted me to look at that question more closely. I also wanted to build an AI-agent workflow where research, calculations and review leave a clear trail instead of producing a polished answer with no way to check it.

## How it works

```text
Official reports → research agent → analysis agent → independent review agent → my review
                       ↓                 ↓                    ↓
                  cited evidence   checked calculations   challenged claims
```

Claude Desktop or Codex can collect report evidence through local MCP tools. Codex then drafts an assessment using Python calculations, and a fresh Codex session reviews the sources, arithmetic and wording. The controller saves validated handoffs and stops for human review. The workflow runs on demand using existing subscriptions; it does not require a paid model API.

## Tested on two companies

| Case | What the workflow found |
| --- | --- |
| [Ørsted](case_studies/orsted.md) | Operating cash covered about **28%** of gross investment in 9M2026. Equity and disposals helped fund the programme, while future funding needs remained open. |
| [Vestas](case_studies/vestas.md) | EBIT improved, but operating cash flow fell **42%** year on year in 9M2026 and adjusted free cash flow declined by **€298m**. Working capital was central to the difference. |

Both cases used the same workflow with separate company reports, questions and calculation ledgers. The case files show the source links, calculations, review outcome and remaining uncertainties.

## Run locally

Python 3.11+ is required. Downloaded company reports and private agent traces are excluded from this repository.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe src/workflow.py fetch
.\.venv\Scripts\python.exe src/workflow.py index
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Use `--case vestas_cash_conversion_2026` for the second company. [Detailed run instructions](docs/RUN_LOCALLY.md) cover the Codex and free Claude routes.

The agents support analysis; they do not autonomously extract every financial metric or approve conclusions. The saved local evaluation checks evidence and handoff integrity, not financial accuracy or time saved. This is an educational project, not investment advice.
