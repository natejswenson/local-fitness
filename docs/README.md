# docs

- **[`mcp/`](mcp/)** — **the** user-facing documentation: one reference page
  per MCP tool (parameters, return shapes, worked examples, gotchas), plus the
  index that maps tools by area. MCP is the only client surface, so this
  directory is how anyone discovers what the app can do.
- **[deployment.md](deployment.md)** — start here to run local-fitness as a
  container: what the deploying side wires into compose (env vars, bind mounts,
  the reverse-proxy/DNS topology that lives in a separate infra repo).
- **[google-calendar.md](google-calendar.md)** — one-time Google OAuth setup
  for the plan → calendar sync, leading with the trap (a "Testing"-mode OAuth
  app has its refresh tokens expired by Google every 7 days).
- **`plans/`** — internal design docs and contracts kept as history (how
  features were designed), not user-facing documentation.
- **`handoffs/`** — point-in-time working notes handed between sessions;
  historical by nature, not kept current.
