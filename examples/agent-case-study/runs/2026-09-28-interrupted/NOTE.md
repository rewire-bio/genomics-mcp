# Interrupted first attempt (not a result)

- Claude Code session started 2026-09-28T09:10:36Z with `--model opus`, same prompt, panel and server as the later complete run. The prompt then used the protocol wording from commit c30f740.
- The session began on `claude-opus-5-5`. After the reference lookups, Opus 5.5's safeguards flagged it (category `bio`), and Claude Code automatically continued on `claude-opus-5` (`run.json`, `model_fallbacks`).
- It made 9 MCP calls: 4 `normalize_variant`, 2 `lookup_gene` and 3 `compare_samples`. The first 7 returned results. Calls 8 and 9 (the E55 and E58 windows for 16 files) returned `Connection closed`: the launcher's parent process ended (the author's session hit its Claude usage limit), which stopped Claude Code and the stdio server. No final report was produced.
- The session ran with `--no-session-persistence`, so it cannot be resumed. It is kept, redacted, for the record. None of its values are used in the results.
- The server work directory held 0 bytes of data afterwards (only three empty isolation files that the server creates). The directory was then deleted.
