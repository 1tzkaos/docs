# Mintlify documentation

## Working relationship
- You can push back on ideas-this can lead to better documentation. Cite sources and explain your reasoning when you do so
- ALWAYS ask for clarification rather than making assumptions
- NEVER lie, guess, or make up information

## Project context
- Format: MDX files with YAML frontmatter
- Config: docs.json for navigation, theme, settings
- Components: Mintlify components

## Content strategy
- Document just enough for user success - not too much, not too little
- Prioritize accuracy and usability of information
- Make content evergreen when possible
- Search for existing information before adding new content. Avoid duplication unless it is done for a strategic reason
- Check existing patterns for consistency
- Start by making the smallest reasonable changes

## Frontmatter requirements for pages
- title: Clear, descriptive page title
- description: Concise summary for SEO/navigation

## Writing standards
- Second-person voice ("you")
- Prerequisites at start of procedural content
- Test all code examples before publishing
- Match style and formatting of existing pages
- Include both basic and advanced use cases
- Language tags on all code blocks
- Alt text on all images
- Relative paths for internal links

## Git workflow
- NEVER use --no-verify when committing
- Ask how to handle uncommitted changes before starting
- Create a new branch when no clear branch exists for changes
- Commit frequently throughout development
- NEVER skip or disable pre-commit hooks

## Do not
- Skip frontmatter on any MDX file
- Use absolute URLs for internal links
- Include untested code examples
- Make assumptions - always ask for clarification

## Project-specific notes (Dexploit)

### OpenAPI is the contract

`api-reference/openapi.json` is the source of truth for the REST surface. If the Rust API changes, update `openapi.json` in the same PR. Mintlify groups operations by their first `tags` value into the navigation groups defined in `docs.json` — adding a new endpoint means adding both an entry under `paths` and (if it's the first of its kind) a tag-matching `group` in the API-reference tab.

Source-of-truth handlers (read these before changing endpoint docs):

- REST raw swaps: `/opt/Dexploit-Swaps/crates/swaps-api/src/handlers.rs`
- REST OHLCV: `/opt/Dexploit-Swaps/crates/swaps-ohlcv-api/src/handlers/`
- WebSocket: `/opt/Dexploit-Swaps/crates/swaps-streamer/src/main.rs` and `filters.rs`
- gRPC: `/opt/Dexploit-Swaps/crates/swaps-streamer/src/grpc_server.rs` plus `examples/grpc/typescript/.../proto/swaps.proto` (canonical client-facing proto)
- Quotas/tiers: `/opt/Dexploit-Swaps/crates/swaps-api/src/quota.rs`

### Examples repo is the canonical home for runnable code

`https://github.com/DexploitV1/Dexploit-Examples`. Don't duplicate runnable client code in MDX. Inline TypeScript snippets here are illustrative; full clients live in the examples repo.

### Validate before push

```bash
npx --yes mintlify validate     # strict: warnings → exit
npx --yes mintlify broken-links # internal + external link check
```

Both must pass.
