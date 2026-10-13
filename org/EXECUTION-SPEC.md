# Machine organization — execution spec (2026-10-13)

Driver: Claude Opus 5.5 session zero-suminc-3d. Full design (read the sections you need):
/Users/zero-suminc./Inbox/plans/2026-10-13-machine-org-design-sol.md (GPT-6.1 Sol).
Project registry (single source of truth): /Users/zero-suminc./.config/zs-ops/org/projects.json
Config/manifests backup of this run: /Users/zero-suminc./Backups/machine-org/20261013-060040/
The old weekly sweeper (launchd com.zs.inbox-sweep) is UNLOADED (frozen) until the new engine replaces it.

## Driver decisions that override or simplify the Sol design
1. Inbox layout = Sol's: `~/Inbox/<type>/<project>/<YYYY-MM>/<item>`; handoffs =
   `~/Inbox/notes/handoffs/<project>/<YYYY-MM>/<item>`. Types: html plans pdfs screenshots
   notes misc. Admin at Inbox L1: `Potential Deletes/`, `_views/`, `_shortcuts/`, `_archive/`.
   `_unassigned` is the project for unknowns.
2. Bundles (directories) move as one unit. Never descend into a bundle; never route its children.
3. Handoffs modified in the last 7 days stay flat in `notes/handoffs/` (live resume paths);
   the daily job files them once they are >7 days old. Readers search recursively.
4. Desktop: exactly `Projects` alias, `Inbox` alias, plus the Devin Search desktop bar
   (app window, not a file). An item dropped on Desktop is routed; a Finder alias to its new
   location is left on Desktop for 3 days, then removed (item stays in search Recent).
   Devin explicitly asked for that shortcut behavior.
5. Desktop design material is IN USE by the design-library session (zero-suminc-5f) and
   registered in ~/projects/tools/zs-library/library.yaml. It moves to a stable home, NOT to
   the drive: `~/projects/design-research/` (study/, design-extra/, desktop-docs/ ...).
   library.yaml paths are updated on a zs-library feature branch in a separate worktree
   (never touch the 5f session's working tree), PR opened, 5f told before merge.
6. Daily automation ships now (not after a 7-day audit), but its mutations are limited to:
   routing NEW top-level items into the layout, filing >7-day handoffs, moving >30-day
   completed items to cold storage ONLY via verified tar.zst on /Volumes/DevinBackUp (list +
   hash verified, source removed only after verify), and moving uncertain items to
   `Potential Deletes/`. It never deletes unique files, never touches repos, never touches
   ~/Backups. Every mutation is written to a JSONL manifest with an undo command.
7. Jev (`~/bin/jev-decide`, or MCP jev_evaluate) is used only to pick a project slug from the
   registry (closed set + `_unassigned`) for items alias matching cannot resolve. Accept only
   probability >= 0.85; otherwise `_unassigned`. Send only file/bundle names and the first
   ~300 chars of a README/markdown title — never contracts, health data, secrets, transcripts.
8. Naming for NEW outputs: `YYYY-MM-DD-<project>-<subject>.<ext>` (kebab-case, ASCII,
   <=64-char subject). Existing items keep their names during migration (no mass rename),
   except a date prefix may be added to undated top-level items when moved.
9. Newest first: Finder list view, sort by Date Modified descending on Inbox type, project and
   month folders (AppleScript); month folders are named YYYY-MM.
10. Screenshots: `defaults write com.apple.screencapture location ~/Inbox/screenshots/_unassigned/<YYYY-MM>`;
    the daily job advances the month.

## Protected (do not move/rename/delete unless your brief explicitly says so)
- Repos stay where they are. No repo moves in this run.
- Design-library session paths: ~/projects/tools/{zs-library,zs-skills,zs-mcp-gateway,dsh-jev-mcp,third-party},
  ~/projects/zs-land, /Volumes/DevinBackUp/fonts/google-fonts, ~/Backups/ (all of it),
  ~/.zshenv agent-shell block, ~/projects/clients/*/DESIGN.md, ~/projects/mm-source-kits,
  ~/projects/design-captures, ~/projects/prompt-catalog, ~/Library/Fonts.
  In zs-skills never edit design-persona, ui-ux-buddy, design-contract (5f's branch feat/design-preflight-smoke).
- ~/.claude/AGENTS.md has open PR #56 (claude-dotfiles, branch docs/agents-paths-zsh).
- App data, agent histories (~/.claude/projects, ~/.codex, ~/.hermes): no changes.

## Write contract (implemented by the org-engine builder)
`~/bin/inbox-path <project-or-alias> <type|handoffs> <subject> [--ext md] [--bundle] [--date YYYY-MM-DD] [--json]`
prints an absolute, collision-free path `~/Inbox/<type>/<slug>/<YYYY-MM>/<YYYY-MM-DD>-<slug>-<subject>[.ext]`
and creates the parent dir. Unknown project → `_unassigned`.
`~/bin/inbox-find [--project X] [--type T] [--latest] [--limit N] [query]` lists matching
items newest first, searching new layout AND legacy flat locations.
Python import: `/Users/zero-suminc./.config/zs-ops/org/inbox_paths.py` (functions `allocate`, `find`, `resolve_project`).
