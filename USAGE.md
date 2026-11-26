# Use the Modal provider

Use a Modal workspace and environment where your token can access the app, prepared image, and sandboxes. Build and install the provider following the [README](README.md). Use a MagicMtn host with support for provider credential protocol v2.

## Install the provider

```sh
magic provider install modal
```

The package includes a private Python 3.13 runtime, the pinned Modal SDK, and trusted TLS certificates. Installation checks the runtime before activating the provider. Linux packages use glibc. Stop the local server before installing or upgrading a provider.

## Prepare images with MagicMtn

Start the local server. Configuration creates or selects the `magicmtn` app in the token's workspace and environment.

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
magic provider configure modal --secret-file ./modal-credentials.json
magic provider verify modal
magic images list
magic images prepare canonical --provider modal
magic computer new --image canonical --provider modal
```

Use `--option app=NAME` during configuration to select an app name. Repeated configuration and token rotation preserve the saved app ID. `magic provider verify modal` checks that connection without creating resources.

Preparation imports Canonical's immutable AMD64 image into your Modal app, validates its runtime and Pi in a temporary sandbox, cleans up that sandbox, and saves the prepared image ID in the local deployment. Repeating the command reuses the saved preparation. No image-ID mapping or Canonical edit is needed.

Register an existing custom image with a new name:

```sh
magic images add my-dev --from ghcr.io/my-org/dev:1.0
magic images prepare my-dev --provider modal
magic computer new --image my-dev --provider modal
```

The image must be readable from its registry and include Python, Pi, `/bin/sh`, `/bin/su`, an executable `/usr/local/bin/magicmtn-sandbox-agent`, and a `sandbox` user with UID and GID 10001 that can write to `/workspace`. Preparation validates your image without modifying it. A failed validation cleans up the temporary sandbox and leaves the image unavailable for named-image launches.

The encrypted credential store retains the token locally. Image preparation uses the verified app connection and records the provider artifact separately for each connection, provider release, platform, and digest. Existing sandboxes retain their frozen image and mapping across provider upgrades.

## Install dependencies for source SDK use

For a host that uses the Rust library directly, install the pinned dependencies in an interpreter dedicated to that host:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Pass its absolute interpreter path through `ModalProvider::new` or `MAGICMTN_MODAL_PYTHON`. Attach credentials, invoke the `ProviderSetup` contract to configure the app, and persist the returned settings before accepting workload requests.

## SDK host integration

For executable hosting, use the installed package's runtime environment and deliver the Modal token through the protocol v2 credential exchange. Invoke provider setup explicitly, persist its returned app name and ID, and pass those settings to subsequent provider processes. Account verification checks the saved app and returns its object ID without creating an app.

| Setting                                | Purpose                                                    |
| -------------------------------------- | ---------------------------------------------------------- |
| `MAGICMTN_MODAL_APP`                   | App name in the Modal environment. Defaults to `magicmtn`. |
| `MAGICMTN_MODAL_PREPARED_IMAGES`       | JSON map from OCI digest references to Modal image IDs.    |
| `MAGICMTN_MODAL_PYTHON`                | Python interpreter for hosts that set dependency paths.    |
| `MAGICMTN_MODAL_APP_ID`                | Optional expected Modal app ID for an SDK host.            |
| `MAGICMTN_MODAL_INFERENCE_URL`         | OpenAI-compatible HTTPS API base URL, including `/v1`.     |
| `MODAL_PROXY_TOKEN_ID`                 | Proxy Token ID for inference endpoint authentication.      |
| `MODAL_PROXY_TOKEN_SECRET`             | Proxy Token secret for inference endpoint authentication.  |
| `MAGICMTN_MODAL_INFERENCE_SECRET_FILE` | Absolute path to the private JSON Proxy Token file.        |

For Rust library embedding, use the crate with `default-features = false`. Construct `ModalProvider::new("/absolute/path/to/.venv/bin/python")` with the interpreter path and call `with_credential` with your token pair as `magicmtn_sdk::plugin::CredentialValues`. Set the app and image mapping in the host process before starting it. To create a sandbox, pass `image:<digest-pinned OCI reference>|<session UUID>` to `SandboxBackend::create_with_ttl` and keep the returned `ProviderSandbox` for subsequent commands and termination.

## Chat inference

Use a [Modal Model API](https://modal.com/docs/guide/model-apis) or deploy an [OpenAI-compatible vLLM server](https://modal.com/docs/examples/vllm_inference). Enable Modal Proxy Token authentication on your server and copy the API base URL and served model name from its configuration. The API base URL includes `/v1`; the provider appends `/chat/completions`.

Save your [Modal Proxy Token](https://modal.com/docs/guide/webhook-proxy-auth) pair in a private JSON file on the provider host:

```json
{
  "MODAL_PROXY_TOKEN_ID": "YOUR_PROXY_TOKEN_ID",
  "MODAL_PROXY_TOKEN_SECRET": "YOUR_PROXY_TOKEN_SECRET"
}
```

Restrict the file to the host user with `chmod 600 /absolute/path/modal-proxy-credentials.json`. Save the endpoint and absolute secret file path in the provider connection:

```sh
magic provider configure modal --secret-file ./modal-credentials.json --option inference_url=https://YOUR_ENDPOINT/v1 --option inference_secret_file=/absolute/path/modal-proxy-credentials.json
```

The workspace token saved by `--secret-file` handles sandbox setup and verification. The separate Proxy Token file stays on the provider host and is read for each inference invocation, allowing token rotation by replacing its contents. Connection configuration stores its path. The provider sends Proxy Tokens to the adapter over stdin and authenticates endpoint requests with `Authorization: Bearer <proxy-token-id>.<proxy-token-secret>`.

SDK hosts can set `MAGICMTN_MODAL_INFERENCE_URL` and `MAGICMTN_MODAL_INFERENCE_SECRET_FILE` in the provider process environment. `ModalProvider::from_env` also accepts `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET` from an explicitly supplied runtime environment. Explicit credentials take precedence over the secret file. Rust hosts attach Proxy Tokens explicitly with `with_inference_credential`:

```rust
use magicmtn_provider_modal::ModalProvider;
use magicmtn_sdk::{InferenceBackend, InferenceRequest, InferenceResponse, ModelConfiguration};
use magicmtn_sdk::plugin::CredentialValues;
use serde_json::json;

async fn chat() -> anyhow::Result<InferenceResponse> {
    let provider = ModalProvider::new("/absolute/path/to/.venv/bin/python")
        .with_inference_endpoint("https://YOUR_ENDPOINT/v1")
        .with_inference_credential(CredentialValues([
            ("MODAL_PROXY_TOKEN_ID".into(), std::env::var("MODAL_PROXY_TOKEN_ID")?),
            ("MODAL_PROXY_TOKEN_SECRET".into(), std::env::var("MODAL_PROXY_TOKEN_SECRET")?),
        ].into()));
    provider.infer(&InferenceRequest {
        model: ModelConfiguration {
            model: "YOUR_SERVED_MODEL".into(),
            provider: Some("modal".into()),
            version: None,
            parameters: json!({"max_tokens": 512, "temperature": 0.7}),
        },
        task: "chat".into(),
        input: json!({"messages": [{"role": "user", "content": "Hello!"}]}),
    }).await
}
```

The `chat` task accepts text messages and OpenAI function tools. It normalizes historical tool calls and results to the SDK conventions and returns `output.message`, `output.usage`, and `output.finish_reason`, with the served model in `InferenceResponse.model`. Supported parameters are `temperature`, `top_p`, `top_k`, `min_p`, `max_tokens`, `max_completion_tokens`, `stop`, `presence_penalty`, `frequency_penalty`, `repetition_penalty`, `seed`, `response_format`, `tool_choice`, `parallel_tool_calls`, and `reasoning_effort`; the selected endpoint validates model-specific parameter support. Put the served model identifier in `model` and leave `version` empty.

Inference uses non-streaming requests with a 290-second HTTP timeout and a 2 MiB response limit. Configure an HTTPS endpoint directly; redirects are rejected. The provider makes one attempt per invocation. After an uncertain failure, the host decides whether to issue another request. Model deployment and serving capacity are managed through Modal.

## Sandbox behavior

Each sandbox uses Modal's VM runtime with one CPU, 512 MiB of memory, a writable `/workspace`, and outbound networking. Commands run as the `sandbox` user. Supply a wall-clock lifetime from 1 to 86,400 seconds through the host. Direct library calls to `create` use one hour. Create a new sandbox when the lifetime expires.

Bounded execution reports stdout, stderr, exit code, elapsed time, and timeout and truncation flags. Use explicit shell arguments such as `/bin/sh -c` for shell syntax. After an uncertain timeout, inspect the sandbox in Modal before retrying. Sandboxes are named `magicmtn-session-<session UUID>`.
