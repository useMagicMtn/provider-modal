# Agent development guide

Use the [Google Rust Guide]. Follow [CONTRIBUTING.md](CONTRIBUTING.md) for builds, tests, and packaging. Put setup and usage in [README.md](README.md) and [USAGE.md](USAGE.md).

- `src/main.rs` starts the credential-aware provider executable and declares supported operations.
- `src/lib.rs` validates requests and invokes the embedded Python adapter.
- `src/deployments.rs` implements endpoint lifecycle and persistent-state selection.
- `src/inference.rs` implements text-chat translation and separate Proxy Token credentials.
- `src/modal_bridge.py` uses the Modal SDK for app lookup, sandbox lifecycle, and bounded command execution, HTTPS inference transport, and managed endpoint reconciliation.
- `tests/test_bridge.py` exercises the adapter with fake Modal objects. The SDK import test checks the installed dependency.

Preserve credential-free discovery, explicit credential delivery, app identity checks, digest-pinned image mappings, sandbox ownership checks, argument quoting, and execution limits. Keep credentials, local Cargo patches, and build outputs out of source control. Keep each Markdown paragraph or list item on one source line and pad Markdown tables.

[Google Rust Guide]: https://google.github.io/styleguide/rustguide.html
