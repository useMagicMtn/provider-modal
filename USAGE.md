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

## Prepare images with MagicMtn

Start the local server. The Modal app must already exist in the token's environment.

```sh
magic server start
```

Save your token in a private file named `modal-credentials.json`:

```json
{
  "MODAL_TOKEN_ID": "YOUR_TOKEN_ID",
  "MODAL_TOKEN_SECRET": "YOUR_TOKEN_SECRET"
}
```

In another terminal:

```sh
magic --local provider configure modal --secret-file ./modal-credentials.json
magic --local images list
magic --local images prepare canonical --provider modal
magic --local computer new --image canonical --provider modal
```

Preparation imports Canonical's immutable AMD64 image into your Modal app, validates its runtime and Pi in a temporary sandbox, cleans up that sandbox, and saves the prepared image ID in the local deployment. Repeating the command reuses the saved preparation. No image-ID mapping or Canonical edit is needed.

Register an existing custom image with a new name:

```sh
magic --local images add my-dev --from ghcr.io/my-org/dev:1.0
magic --local images prepare my-dev --provider modal
magic --local computer new --image my-dev --provider modal
```

The image must be readable from its registry and include Python, Pi, `/bin/sh`, `/bin/su`, an executable `/usr/local/bin/magicmtn-sandbox-agent`, and a `sandbox` user with UID and GID 10001 that can write to `/workspace`. Preparation validates your image without modifying it. A failed validation cleans up the temporary sandbox and leaves the image unavailable for named-image launches.

The encrypted credential store retains the token locally. Image preparation uses the verified app connection and records the provider artifact separately for each connection, provider release, platform, and digest. Existing sandboxes retain their frozen image and mapping across provider upgrades.

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
