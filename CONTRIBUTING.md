# Contributing

These instructions are for maintainers of the private Modal provider used by MagicMtn’s cloud offering.

This provider implements MagicMtn's `SandboxBackend` interface using Modal's official Python SDK. Model inference remains with the harness's selected inference provider.

## Development setup

On the machine running the MagicMtn server, install Python 3.11+ and the Modal SDK, then authenticate with your Modal account:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'modal==1.6.1'
.venv/bin/modal setup
export MAGICMTN_MODAL_PYTHON="$PWD/.venv/bin/python"
```

Pass `MAGICMTN_MODAL_PYTHON` to the server process before starting it. You can also authenticate with `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in that process's environment. Credentials stay on the host and are never injected into sandboxes. Set `MAGICMTN_MODAL_APP` to choose a Modal app; the default is `magicmtn`.

For local development, keep this repo beside the private `magicmtn` checkout and create an ignored `.cargo/config.toml` to use its SDK crates:

```toml
[patch.crates-io]
magicmtn-sdk = { path = "../magicmtn/sdk/rust/magicmtn-sdk" }
magicmtn-provider-framework = { path = "../magicmtn/sdk/rust/magicmtn-provider-framework" }
```

```sh
cargo test --locked
.venv/bin/python -m unittest discover -s tests
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
```

Unit tests use fake Modal objects and do not create cloud resources. The sibling
Cloud repository's opt-in `scripts/check-modal-provider.py` exercises the
compiled provider RPC against a prepared image, checks user identity and literal
arguments, runs all six real-Pi supervisor tests with mock inference, and
terminates the test sandbox. Pass Modal authentication only in the caller's
environment and set `MAGICMTN_MODAL_PYTHON` to the SDK interpreter. For library embedding, disable default features and construct `ModalProvider` with the Python interpreter path.

## Build and install for development

For maintainer development only, build with a Rust toolchain supporting edition 2024 and install the local archive into a development instance of MagicMtn:

```sh
./package.sh
magic provider add --file dist/modal-0.1.2-macos-aarch64.tar.gz
```

Use the archive matching your platform. MagicMtn stores the executable privately; it does not need to be on `PATH`. The Python adapter is embedded in the executable, but its Python interpreter and Modal SDK must be installed separately.

## Prepared sandbox images

Prepare the canonical Linux AMD64 sandbox image before creating sessions. The
provider no longer imports registry images or builds them inside the bounded
session-creation call. Private ECR authentication belongs exclusively to the
image-preparation step; use a scoped repository-read role through Modal OIDC.
The runtime provider needs no registry secret or AWS credentials.

Configure `MAGICMTN_MODAL_PREPARED_IMAGES` as non-secret connection configuration
mapping each exact OCI digest to its verified, prepared Modal image object ID:

```json
{
  "registry.example/magicmtn/sandbox@sha256:REPLACE_WITH_64_HEX_DIGEST": "im-REPLACE_WITH_VERIFIED_IMAGE_ID"
}
```

Publish a uniquely versioned Modal image during preparation and record its
`object_id`. Runtime creation uses that ID directly, not the mutable name. A
missing mapping, malformed ID, or image tag without a digest fails before
sandbox allocation; there is no inline-build fallback. Preserve mappings for
releases still in use. The backend's ARM64 architecture is independent of the
sandbox's required AMD64 architecture.

Enable Modal on a harness and select the registered OCI digest. The image must
include the MagicMtn supervisor and a `sandbox` account with UID/GID 10001,
writable `/workspace`, `/bin/su`, `/bin/sh`, and Python. The canonical image meets
this contract. Modal ignores OCI `USER`, so the provider explicitly switches to
this account for its lifetime process, readiness check, and every exec. Arguments
are shell-quoted as separate arguments before this switch. Readiness verifies the
UID/GID, supervisor executable, and workspace permissions; failure terminates the
new sandbox. The VM runtime is explicit: the canonical Pi/supervisor large-output
PTY test failed under gVisor and passed under VM.

New sandbox metadata and ownership tags record the prepared image ID, runtime,
and user. Reattachment checks those fields before executing. Cleanup remains
available for provider-owned older sandboxes, but execution through this release
requires the verified contract. Existing MagicMtn sessions retain their pinned
provider version; activate the new release for new sessions.

## Runtime behavior and limits

Sandboxes use the VM runtime with one CPU and 512 MiB memory. MagicMtn supplies a maximum lifetime through `create_with_ttl`; direct `create` calls retain the one-hour default. Modal accepts up to 24 hours. Outbound networking lets a detached harness contact its trusted gateway; this provider does not set an idle timeout. No inbound ports, host mounts, or secrets are attached. The lifetime limit still applies while work is running; this release does not implement lifetime extension. Command output and execution time follow the harness limits. An expired sandbox requires a new session. Dockerfile templates, interactive shells, file transfer, GPU configuration, and model hosting are outside this provider's current scope.

Timeouts do not prove a remote command stopped immediately. MagicMtn treats timed-out commands as uncertain and does not replay them automatically. A failed create may leave a named sandbox until its lifetime expires; inspect `magicmtn-session-<session UUID>` in Modal before retrying.
