# Interest profile

We run agent-driven software, game, content, ads and markets work on one workstation. We care about changes that alter how we build, steer or verify agents. We do not care about news.

## Domains

- harness: coding-agent runtimes (Claude Code, Codex, Gemini CLI), hooks, skills, plugins, subagents, evals, steering, prompting guidance per model.
- memory: agent memory, knowledge bases, retrieval, context management.
- research: papers and methods with code that change how agents are built or measured.
- gamedev: engines (Unreal, Godot), game-dev agent tooling, 3D and asset pipelines for games.
- assets: image, 3D, video and audio generation models we could run or call.
- content: text, image, video and audio generation for publishing, plus provenance rules and model shutdowns.
- ads: ad-platform APIs, agent features and policy changes.
- markets: quant and trading research that uses agents; data and venue API changes.
- models: new model releases, pricing, deprecations and retirement dates for the models we call.

## Durable versus decaying

- Durable: specs, protocols, design patterns, eval methods, security findings, open-source tools with real adoption.
- Decaying: model version numbers, benchmark ranks, prices, release dates, star counts. Keep these only when a deadline or breakage follows (a shutdown date, a removed parameter).
- A deprecation or retirement date is always worth surfacing: it breaks pipelines on a clock.

## Rubric

- adopt: a change we should make this week (a hook, a flag, a removed prompt rule, a migration before a shutdown).
- trial: worth one real task on our own code before deciding.
- watch: plausible but unproven (a new skill collection, a leaderboard entry, a one-vendor claim).
- ignore: marketing, customer stories, reposts, prerelease tags, generic AI news, anything off these domains.

Skills and plugins from the public ecosystem are mostly unproven and a quarter carry vulnerabilities. Never rate one above watch on popularity alone.
