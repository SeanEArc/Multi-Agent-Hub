# Security policy

Multi-Agent Hub starts AI agents that can edit files and run commands on your PC, so security reports
are taken seriously. Thank you for helping.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.** Report it privately through GitHub:
go to the repository's **Security** tab and choose **Report a vulnerability**. Only the maintainer
sees the report.

Include what you can: the version or commit, what an attacker can do, and steps to reproduce. You
should get a first reply within a week. Once a fix is released, you'll be credited in the release notes
unless you'd rather not be.

## Supported versions

The project is in early development (0.x). Only the latest release and the `main` branch get security
fixes.

## What counts

In scope, for example:
- Another program, a website or another user on the PC reaching the hub's local API without the token,
  or getting the token.
- A web page or a malicious repository making the hub start an agent, run a command or read files.
- The hub exposing, logging or storing Claude or Codex login tokens.
- Importing a skill (folder, git or zip) running code or writing outside the skills library.
- Paths escaping the working folder in the local model's tools.

Not in scope:
- What an agent does inside the folder you gave it, or reading other files on your PC: worktrees are
  documented as not being a sandbox. Container isolation is planned.
- Weaknesses in Claude Code, the Codex CLI or Ollama themselves; report those to Anthropic, OpenAI or
  the Ollama project.
- Prompt injection that makes an agent misbehave within the permissions you gave it, unless the hub
  itself grants more than it should.
