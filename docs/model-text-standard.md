# Model text standard

Every text a model reads in the private product and through this engine follows these rules.
That is the harness system prompt and its tool descriptions (fl-studio-cua), and here the `run` description, the server instructions, every docstring `help()` shows, the guides, the starting project's notes, and every error or notice.
Written 2026-11-16 from the sources at the end; the column "Check" says how CI enforces a rule, and "review" means a person or a reviewing agent checks it.

| # | Rule | Check |
|---|---|---|
| R1 | The user's brief is the first user message, byte for byte, followed only by one line per file the user attached; nothing in it is rewritten [H1-H4, H6]. | harness: the brief arrives unchanged on every provider |
| R2 | Each fact lives in one layer: the system prompt holds only what the arena owns (the task frame, environment invariants, which tools exist, the evaluation rules, how to finish); a tool's own description says what it does; docstrings hold the API; guides hold technique; a project's notes hold its contents [A5, O2, M1, M2]. | harness texts name no SDK member; guide prose names nothing of the starting project |
| R3 | Anything that changes with the code or the host (members, signatures, limits, budgets, the guide list, licence, confinement, offered tools) is generated from the code or filled from the run's actual values, never written by hand into a prompt [A8]. | the description is the generator's output; its numbers come from constants and host settings |
| R4 | A text names only tools, actions and members that exist in that run; adding or removing a tool or guide needs no prompt edit [A3, O4]. | per-mode rendering checks named tools; every `fl.`, `theory.`, `track.` and `effect.` name resolves |
| R5 | Every sentence is true in every mode that shows it: computer use, MCP and hybrid; with or without ears, a workspace, `finish`, web search; licensed or trial FL; confined or not; the engine's starting project or the user's own [O1]. | the prompts and descriptions are rendered for every mode in CI |
| R6 | Every model in an arena modality gets the identical prompt and tools; only the wire format differs, and a difference an API forces is described in neutral words in that tool's own description [H5, H1]. | per modality, one prompt across providers |
| R7 | No musical opinions: genres, song structures, lengths, loudness targets and workflow steps appear only if the brief asks; guides are optional reference on technique and on how the environment works, and their examples are neutral demonstrations [A2, H4]. | review (no word list) |
| R8 | Runs are unattended: the model does not ask the user to confirm or clarify, decides, and says what it decided [O1, O3, H1]. | review |
| R9 | The model keeps working until the brief is done or the budget ends; when part cannot be done, it does the closest thing and says so [O1, O2, H1]. | review |
| R10 | The model checks each action's real effect (a screenshot, a read-back or a measurement) before building on it, and claims nothing it did not observe [A4, A6, O5, H7]. | review |
| R11 | There is one way to finish, and the final report says what was made, what is weak and what failed [H2, H3, H7]. | harness: the report line matches the offered finish |
| R12 | Write plainly and calmly: direct sentences, the reason where it is not obvious, no shouting (capitals, MUST, CRITICAL), no persona flattery, no contradictions [A1, A2, O1, G1]. | review |
| R13 | Keep it minimal: cut what a capable model already knows, put key facts first, and stay in the limits (a tool description or server instructions under 4,096 characters; a guide description of at most 80, a body of at most 6,000); no source files, design docs, dates or issue numbers in model text [A1, A7, A8]. | lengths, guide format and engineering references checked |
| R14 | A tool description says what the tool does, what it takes, what it returns and its limits; an error says what went wrong and what to do next, in the model's terms [A3, A4, O4, M1]. | engine: errors carry a fix and no internal words |
| R15 | Web pages, screen text and files are information, not instructions [O5, H7]. | review |

## Sources

Dates are the page's publication or the file's last commit; "undated" pages were read on the date given.

- A1 Anthropic, Prompting best practices, undated, read 2026-11-16: <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices>
- A2 Anthropic, Prompting Claude Fable 5.2, undated, read 2026-11-16: <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-2>
- A3 Anthropic, Writing effective tools for agents, 2025-09-11: <https://www.anthropic.com/engineering/writing-tools-for-agents>
- A4 Anthropic, Building effective agents, 2024-12-19: <https://www.anthropic.com/engineering/building-effective-agents>
- A5 Anthropic, Effective context engineering for AI agents, 2025-09-29: <https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>
- A6 Anthropic, Computer use tool, undated, read 2026-11-16: <https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool>
- A7 Anthropic, Agent Skills overview and best practices, undated, read 2026-11-16, and Equipping agents with Agent Skills, 2025-10-16: <https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices>, <https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills>
- A8 Claude Code, MCP (description and instructions limits), undated, read 2026-11-16: <https://code.claude.com/docs/en/mcp>
- O1 OpenAI, GPT-5 prompting guide, file commit 2026-07-20: <https://github.com/openai/openai-cookbook/blob/main/examples/gpt-5/gpt-5_prompting_guide.ipynb>
- O2 OpenAI, GPT-5.1 prompting guide, file commit 2026-01-15: <https://github.com/openai/openai-cookbook/blob/main/examples/gpt-5/gpt-5-1_prompting_guide.ipynb>
- O3 OpenAI, Prompt guidance (GPT-6 Astra), undated, read 2026-11-16: <https://developers.openai.com/api/docs/guides/prompt-guidance>
- O4 OpenAI, Function calling, undated, read 2026-11-16: <https://developers.openai.com/api/docs/guides/function-calling>
- O5 OpenAI, Computer use, undated, read 2026-11-15: <https://developers.openai.com/api/docs/guides/tools-computer-use>
- G1 Google, Gemini prompting strategies, undated, read 2026-11-16: <https://ai.google.dev/gemini-api/docs/prompting-strategies>
- M1 MCP specification 2026-07-28, tools and `server/discover` instructions, file commits 2026-10-28 and 2026-11-16: <https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx>
- M2 MCP blog, Server instructions, 2025-11-03: <https://blog.modelcontextprotocol.io/posts/2025-11-03-using-server-instructions/>
- H1 OSWorld, `mm_agents/prompts.py` (2025-07-27) and the Claude agent's prompt (2026-07-15): <https://github.com/xlang-ai/OSWorld/blob/main/mm_agents/prompts.py>
- H2 Terminal-Bench's Terminus-2 template in Harbor, file commit 2025-12-26: <https://github.com/laude-institute/harbor/blob/main/src/harbor/agents/terminus_2/templates/terminus-json-plain.txt>
- H3 mini-swe-agent SWE-bench config (2026-07-06) and SWE-agent default config (2025-09-15): <https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/config/benchmarks/swebench.yaml>
- H4 ARC-AGI-3 LLM agents, undated, read 2026-11-16, and agent templates, file commit 2026-02-10: <https://docs.arcprize.org/llm_agents>
- H5 Design Arena methodology ("All models receive identical prompts"), undated, read 2026-11-16: <https://www.designarena.ai/about>
- H6 Anthropic computer-use demo `loop.py`, file commit 2026-10-23: <https://github.com/anthropics/claude-quickstarts/blob/main/computer-use-demo/computer_use_demo/loop.py>
- H7 Browser Use `agent/prompt.py`, file commit 2026-11-10: <https://github.com/browser-use/browser-use/blob/main/browser_use/agent/prompt.py>

Unverified: Google's computer-use page gives no system-prompt guidance, so none is cited; no GPT-5.5 guide was found (O3 is the current one); the "prompt patterns" section of O5 did not render; the original Terminal-Bench `terminus.txt` was not read (H2 is its successor).
