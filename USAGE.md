# Use the Modal provider

Use a Modal workspace and environment where your token can access the app, prepared image, and sandboxes. Build and install the provider following the [README](README.md). Use a MagicMtn host with support for provider credential protocol v2.

## Install the Modal SDK

For image preparation or direct SDK use, install the provider's pinned dependencies in a virtual environment. Authenticate the preparation environment in the Modal workspace where you will run sandboxes.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/modal setup
```

The standalone MagicMtn server starts credentialed providers with a fixed executable search path. Install the dependencies from `requirements.txt` into the shared site-packages of the Python 3.11+ interpreter that resolves as `python3` along that path. For a separate SDK host, supply an absolute interpreter path through `MAGICMTN_MODAL_PYTHON` or `ModalProvider::new`.

On a dedicated host with your own Python installation under `/usr/local`, install the SDK with `/usr/local/bin/python3 -m pip install -r requirements.txt`. Verify the standalone interpreter before configuring the provider.

```sh
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin python3 -s -c 'import sys, modal; assert sys.version_info >= (3, 11); assert modal.__version__ == "1.6.1"; print(sys.executable)'
```

## Prepare a sandbox image

Select the `linux/amd64` digest of a [MagicMtn Canonical image](https://github.com/useMagicMtn/magicmtn/blob/main/docs/canonical-image-release.md) or a compatible OCI image. A compatible image provides Python, `/bin/sh`, `/bin/su`, an executable `/usr/local/bin/magicmtn-sandbox-agent`, and a `sandbox` user with UID and GID `10001` that can write to `/workspace`.

Set `IMAGE` to the full reference with its SHA-256 digest and `MAGICMTN_MODAL_APP` to an app name in your Modal environment. The example uses a public registry image. For other registries, configure access during [image import](https://modal.com/docs/guide/existing-images).

```sh
export IMAGE='ghcr.io/usemagicmtn/canonical@sha256:REPLACE_WITH_AMD64_DIGEST'
export MAGICMTN_MODAL_APP=magicmtn

prepared_image=$(.venv/bin/python - <<'PY'
import os

import modal

app = modal.App.lookup(os.environ["MAGICMTN_MODAL_APP"], create_if_missing=True)
image = modal.Image.from_registry(os.environ["IMAGE"]).build(app=app)
print(image.object_id)
PY
)
export MAGICMTN_MODAL_PREPARED_IMAGES=$(.venv/bin/python -c 'import json, sys; print(json.dumps({sys.argv[1]: sys.argv[2]}))' "$IMAGE" "$prepared_image")
```

Save the JSON mapping for future server starts. Set `IMAGE` to the same reference when creating a sandbox. Keep mappings for any image digests you still launch.

## Configure a standalone server

Stop the server before installing the provider archive. Start it from the terminal where you exported `MAGICMTN_MODAL_APP` and `MAGICMTN_MODAL_PREPARED_IMAGES`.

```sh
magic server start
```

In another terminal, create a file named `modal-credentials.json` with `umask 077`. Fill in both values from your [Modal API token](https://modal.com/settings).

```json
{
  "MODAL_TOKEN_ID": "REPLACE_WITH_TOKEN_ID",
  "MODAL_TOKEN_SECRET": "REPLACE_WITH_TOKEN_SECRET"
}
```

```sh
magic --local provider configure modal --secret-file ./modal-credentials.json
```

The host verifies the token against the configured app and saves the credential. To rotate a token for that app, run the same command with the replacement bundle. Then use the [README commands](README.md#run-a-sandbox) to create, exercise, and terminate a sandbox.

## SDK host integration

For executable hosting, pass these settings to the provider process and deliver the Modal token through the protocol v2 credential exchange. Provision the app in Modal before verifying credentials. Account verification returns the app's object ID.

| Setting                          | Purpose                                                    |
| -------------------------------- | ---------------------------------------------------------- |
| `MAGICMTN_MODAL_APP`             | App name in the Modal environment. Defaults to `magicmtn`. |
| `MAGICMTN_MODAL_PREPARED_IMAGES` | JSON map from OCI digest references to Modal image IDs.    |
| `MAGICMTN_MODAL_PYTHON`          | Python interpreter for hosts that set dependency paths.    |
| `MAGICMTN_MODAL_APP_ID`          | Optional expected Modal app ID for an SDK host.            |

For Rust library embedding, use the crate with `default-features = false`. Construct `ModalProvider::new("/absolute/path/to/.venv/bin/python")` with the interpreter path and call `with_credential` with your token pair as `magicmtn_sdk::plugin::CredentialValues`. Set the app and image mapping in the host process before starting it. To create a sandbox, pass `image:<digest-pinned OCI reference>|<session UUID>` to `SandboxBackend::create_with_ttl` and keep the returned `ProviderSandbox` for subsequent commands and termination.

## Sandbox behavior

Each sandbox uses Modal's VM runtime with one CPU, 512 MiB of memory, a writable `/workspace`, and outbound networking. Commands run as the `sandbox` user. Supply a wall-clock lifetime from 1 to 86,400 seconds through the host. Direct library calls to `create` use one hour. Create a new sandbox when the lifetime expires.

Bounded execution reports stdout, stderr, exit code, elapsed time, and timeout and truncation flags. Use explicit shell arguments such as `/bin/sh -c` for shell syntax. After an uncertain timeout, inspect the sandbox in Modal before retrying. Sandboxes are named `magicmtn-session-<session UUID>`.
