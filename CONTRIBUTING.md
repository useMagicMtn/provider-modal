# Contributing

Follow the [README](README.md) for source installation and the [usage guide](USAGE.md) for Modal configuration. The Rust executable implements the MagicMtn `SandboxBackend` interface using the official Modal Python SDK and `InferenceBackend` using OpenAI-compatible HTTPS endpoints. `InferenceDeploymentBackend` creates, inspects, and permanently deletes provider-owned endpoints. The adapter source is embedded in the executable.

## Development setup

Install [Bazelisk](https://github.com/bazelbuild/bazelisk#installation) and run:

```sh
bazel build //:provider //:bridge
bazel test //...
```

Bazel fetches the public MagicMtn core at the revision in [MODULE.bazel](MODULE.bazel) and installs Python 3.13 and the hashed dependencies in `requirements.lock`. The workflow runs on x86-64 Linux, ARM64 Linux, and Apple silicon. To test a sibling MagicMtn checkout, use `bazel test --override_module=magicmtn=../magicmtn //...`.

For Cargo, copy [.cargo/config.toml.example](.cargo/config.toml.example) to `.cargo/config.toml` and adjust the paths if needed. Install the Modal SDK in a virtual environment as described in the [usage guide](USAGE.md#install-dependencies-for-source-sdk-use).

```sh
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo check --locked --lib --no-default-features
.venv/bin/python -m unittest discover -s tests
```

Bridge unit tests use fake Modal objects and mocked inference HTTP transport. Rust tests check chat schemas, tool calls, and executable discovery and dispatch. The SDK import test uses the pinned real SDK. All automated tests run without a Modal account. Use an independent Modal app for live verification. Prepare an image, launch a sandbox, run a command, rotate to a second token for the same app, run another command, and terminate the sandbox. Confirm cleanup in Modal.

## Update Python dependencies

After editing `requirements.txt`, regenerate the lock with [uv](https://docs.astral.sh/uv/getting-started/installation/) and rerun the checks.

```sh
uv pip compile --universal --generate-hashes --python-version 3.13 requirements.txt -o requirements.lock
bazel test //...
```

## Provider behavior

Explicit provider setup creates or selects an app and returns its name and immutable ID. Read-only verification and workload operations look up the saved app without creating a replacement. The host passes the token in the protocol v2 startup exchange. Rust passes it to the Python adapter over stdin. On startup the provider can advertise its capabilities without credentials.

The adapter resolves each OCI digest to a prepared Modal image ID. It runs the lifetime process and commands as the `sandbox` user and probes the workspace and sandbox agent before returning a new sandbox. The returned metadata records the image, execution user, and VM runtime. Subsequent commands verify these values and the provider's ownership tags before executing. Termination checks ownership and can clean up older provider-owned sandboxes.

The adapter does not replay uncertain remote operations. If a create times out, inspect Modal for `magicmtn-session-<session UUID>` before making another request. The sandbox's wall-clock TTL continues while commands run.

## Managed endpoint verification

Use a private persistent directory and an independent Modal app for live checks. Configure account access and confirm that setup creates no endpoint or Proxy Token. Create a shared inference deployment with an explicit model, repeat creation to confirm endpoint and credential reuse, invoke a small chat request, request deletion, and poll until the remote lifecycle confirms teardown. Revoke the generated test Proxy Token after validation. Endpoint and Proxy Token creation receipts must be durable before remote mutation; a timed-out create must be reconciled by name rather than replayed.

The endpoint adapter uses the pinned SDK's endpoint and Proxy Token protobuf APIs through its synchronizer, keeping RPCs on the authenticated client's event loop. The import test checks the required protobuf fields. Extend the fake-client tests when changing RPC shapes or lifecycle mappings.

## Package a release

Install `uv==0.13.10` and run `./package.sh` on the intended target architecture. It writes a native archive to `dist/` containing the provider executable, the Apache License 2.0, and a relocatable Python runtime with the hash-locked Modal dependencies. `package.json` records the executable and runtime checksums, protocol version, interpreter path, and import check. The installer checks the complete runtime before activating the release. Install the archive with `magic provider install --file PATH_TO_PACKAGE`. Use a new package version for a changed release binary so existing sandboxes retain their pinned provider release.
