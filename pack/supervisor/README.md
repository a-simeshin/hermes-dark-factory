# Supervisor templates

Copyable material for running a stronger agent as the factory's supervisor, as described in [docs/supervisor-agent.md](../../docs/supervisor-agent.md). The supervisor checks, controls, directs and teaches; it never edits Hermes, the agents' files or the product's code.

| File | Use |
|---|---|
| [loop-prompt.md](loop-prompt.md) | The prompt the supervisor runs on a schedule, every 30 minutes for example. |
| [round-template.md](round-template.md) | The shape of one teaching round sent to the orchestrator. |
| [verify.md](verify.md) | Read-only commands the supervisor uses to check the artifact instead of the report. |

Replace the placeholders in angle brackets with your values. Nothing here needs write access to the board database or to the agents' files.
