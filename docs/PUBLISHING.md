# Publishing checklist

Steps that need the project owner's accounts. Everything else (building,
validating, tagging, GitHub release) is automated.

## Install path: GitHub, deliberately

There is no PyPI package and none is planned. The project installs fine
without one:

    pip install git+https://github.com/haxo98098/slicegrep

and the Claude Code plugin needs no pip at all:

    /plugin marketplace add haxo98098/slicegrep
    /plugin install slicegrep@slicegrep

Releases attach the wheel and sdist automatically, so anyone who wants a
built artifact can take one from the Releases page.

## 1. Community MCP directories (free listings, form submissions)

These index GitHub directly and need no package registry:

- https://mcpservers.org — "Submit" form
- https://mcp.so — "Submit" form
- https://glama.ai/mcp/servers — indexes GitHub automatically; check listing
- awesome-mcp-servers lists (e.g. github.com/punkpeye/awesome-mcp-servers)
  accept PRs adding one line

## 2. Claude Code plugin marketplace

The repo is its own marketplace (`.claude-plugin/marketplace.json`), so the
install commands above work for anyone the moment the repo is public. Nothing
to submit; listing sites that aggregate plugin marketplaces can be pointed at
the repo URL.

## 3. Repo presentation (owner only)

- About description and topics: `llm`, `agents`, `context-engineering`,
  `code-search`, `mcp`, `claude`, `tokens`, `python`
- Confirm the demo GIF renders on the repo landing page

## 4. Claude for Open Source application

https://claude.com/contact-sales/claude-for-oss — apply under the exception
clause ("apply anyway and tell us about it"), citing held-out benchmark
protocol with confirmation runs on virgin data, a documented ledger of
rejected variants, the omission accounting that lets a caller verify what was
withheld, and the MCP server plus plugin for the agent ecosystem.

## Note on the official MCP registry

https://registry.modelcontextprotocol.io requires a package on a supported
registry (PyPI or npm). Since this project ships via GitHub only, that
registry is out of scope. The community directories in step 1 work with a
GitHub URL alone.
