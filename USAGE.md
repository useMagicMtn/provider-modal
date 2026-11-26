# Use the Modal provider

Use a Modal workspace and environment where your token can manage apps, endpoints, Proxy Tokens, prepared images, and sandboxes. Build and install the provider following the [README](README.md). Use a MagicMtn host with support for provider credential protocol v2.

## Install the provider

```sh
magic provider install modal
```

The package includes a private Python 3.13 runtime, the pinned Modal SDK, and trusted TLS certificates. Installation checks the runtime before activating the provider. Linux packages use glibc. Stop the local server before installing or upgrading a provider.

## Prepare images with MagicMtn

Start the local server. Configuration creates or selects the `magicmtn` app and saves account access. The host supplies a model when it creates an inference deployment.

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

Use `--option app=NAME` during configuration to select an app name. Repeated configuration and token rotation preserve the saved app ID. `magic provider verify modal` checks the saved app identity without creating resources. Inference deployment creation preserves the endpoint ID and generated credentials for repeated requests with the same resource name and specification.

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

| Setting                                | Purpose                                                                       |
| -------------------------------------- | ----------------------------------------------------------------------------- |
| `MAGICMTN_MODAL_APP`                   | App name in the Modal environment. Defaults to `magicmtn`.                    |
| `MAGICMTN_MODAL_PREPARED_IMAGES`       | JSON map from OCI digest references to Modal image IDs.                       |
| `MAGICMTN_MODAL_PYTHON`                | Python interpreter for hosts that set dependency paths.                       |
| `MAGICMTN_MODAL_APP_ID`                | Optional expected Modal app ID for an SDK host.                               |
| `MAGICMTN_MODAL_INFERENCE_URL`         | OpenAI-compatible HTTPS API base URL, including `/v1`.                        |
| `MODAL_PROXY_TOKEN_ID`                 | Proxy Token ID for inference endpoint authentication.                         |
| `MODAL_PROXY_TOKEN_SECRET`             | Proxy Token secret for inference endpoint authentication.                     |
| `MAGICMTN_MODAL_INFERENCE_SECRET_FILE` | Absolute path to the private JSON Proxy Token file.                           |
| `MAGICMTN_MODAL_STATE_DIRECTORY`       | Private persistent directory for generated credentials and resource receipts. |
| `MAGICMTN_MODAL_ENVIRONMENT`           | Saved Modal environment for app and endpoint lookups.                         |

For Rust library embedding, use the crate with `default-features = false`. Construct `ModalProvider::new("/absolute/path/to/.venv/bin/python")` with the interpreter path and call `with_credential` with your token pair as `magicmtn_sdk::plugin::CredentialValues`. Set the app and image mapping in the host process before starting it. To create a sandbox, pass `image:<digest-pinned OCI reference>|<session UUID>` to `SandboxBackend::create_with_ttl` and keep the returned `ProviderSandbox` for subsequent commands and termination.

## Chat inference

Configure a workspace API token once:

```sh
magic provider configure modal --secret-file ./modal-credentials.json
```

Account configuration establishes access and saves the private state location. Model selection belongs to an inference deployment: the host supplies `InferenceDeploymentSpec.model` when creating the resource. The provider provisions that model's [Modal Model API](https://modal.com/docs/guide/model-apis), discovers its serving URL, creates a Proxy Token, and grants it access to the endpoint's environment. Shared inference uses pay-per-token billing. The local host supplies the private state directory automatically, and credentials stay on the provider host.

The default app name is `magicmtn`; select another with `--option app=NAME`. Each deployment has its own caller-selected resource name and model. Repeated creates with the same name and specification reuse the resource; changing an existing resource's model or serving settings requires a new resource name.

The `chat` task accepts text messages and OpenAI function tools. It normalizes historical tool calls and results to the SDK conventions and returns `output.message`, `output.usage`, and `output.finish_reason`, with the served model in `InferenceResponse.model`. Supported parameters are `temperature`, `top_p`, `top_k`, `min_p`, `max_tokens`, `max_completion_tokens`, `stop`, `presence_penalty`, `frequency_penalty`, `repetition_penalty`, `seed`, `response_format`, `tool_choice`, `parallel_tool_calls`, and `reasoning_effort`; the endpoint validates model-specific support. Put the served model identifier in `model` and leave `version` empty.

Inference uses non-streaming requests with a 290-second HTTP timeout and a 2 MiB response limit. HTTPS requests reject redirects and make one attempt per invocation. Model execution releases the reconciliation lock so concurrent inference calls can proceed.

## Manage inference deployments from an SDK host

Construct a provider with workspace credentials and a private persistent directory, configure account access, and create a deployment for the selected model through `InferenceDeploymentBackend`. Preserve the returned connection settings and pass them to subsequent executable processes. A Rust host can address the returned `inference_model` directly:

```rust
use magicmtn_provider_modal::ModalProvider;
use magicmtn_sdk::{
    InferenceBackend, InferenceDeploymentBackend, InferenceDeploymentSpec,
    InferenceRequest, InferenceResponse, ModelConfiguration, InferenceDeploymentState,
};
use magicmtn_sdk::plugin::CredentialValues;
use magicmtn_sdk::provider_setup::ProviderSetup;
use serde_json::json;

async fn chat() -> anyhow::Result<InferenceResponse> {
    let provider = ModalProvider::new("/absolute/path/to/.venv/bin/python")
        .with_state_directory("/absolute/path/to/private/modal-state")
        .with_credential(CredentialValues([
            ("MODAL_TOKEN_ID".into(), std::env::var("MODAL_TOKEN_ID")?),
            ("MODAL_TOKEN_SECRET".into(), std::env::var("MODAL_TOKEN_SECRET")?),
        ].into()));
    let connection = provider.configure(&std::collections::BTreeMap::new()).await?;
    // Persist the account connection settings for subsequent provider processes.
    let deployment = InferenceDeploymentBackend::create(&provider, &InferenceDeploymentSpec {
        name: "example-chat".into(),
        model: "moonshotai/Kimi-K3".into(),
        configuration: json!({}),
    }).await?;
    // Persist this resource reference for inspection and deletion.
    let resource = deployment.resource;
    tokio::time::timeout(std::time::Duration::from_secs(300), async {
        loop {
            let status = InferenceDeploymentBackend::status(&provider, &resource).await?;
            match status.state {
                InferenceDeploymentState::Ready => return Ok::<_, anyhow::Error>(()),
                InferenceDeploymentState::Failed | InferenceDeploymentState::Deleted => {
                    anyhow::bail!("Modal endpoint failed or was deleted");
                }
                _ => tokio::time::sleep(std::time::Duration::from_secs(2)).await,
            }
        }
    }).await??;
    provider.infer(&InferenceRequest {
        model: ModelConfiguration {
            model: deployment.inference_model,
            provider: Some("modal".into()),
            version: None,
            parameters: json!({"max_tokens": 512}),
        },
        task: "chat".into(),
        input: json!({"messages": [{"role": "user", "content": "Hello!"}]}),
    }).await
}
```

Create, inspect, and delete resources with `InferenceDeploymentBackend::create`, `status`, and `delete`. Resource names reconcile repeated creates against the same immutable specification. The configuration accepts `serving_mode` (`shared` by default, or `dedicated`) and `routing_region` (`us-west` by default, `us-east`, or `eu-west`). Dedicated endpoints provision serving compute and bill for their resources. The provider chooses the serving recipe and automatic compute placement.

Deletion calls Modal's permanent endpoint stop operation. Poll `status` until `Deleted` to confirm teardown. A deleted endpoint stays deleted; a replacement uses a new resource name. Resource lifetime is independent of Pi sessions and provider processes. Private state records endpoint ownership, immutable settings, and mutation receipts; only resources recorded for the connection can be inspected, invoked, or deleted through this contract.

The state directory contains `deployments.json`, the private `proxy.json` credential file, and a reconciliation lock. Back it up with the local deployment. Writes use private files and atomic replacement. Uncertain endpoint creates are reconciled by their recorded names before another create is considered. An uncertain Proxy Token creation retains a receipt and token name for reconciliation; the adapter does not blindly replay it.

## Use an existing inference endpoint

SDK hosts and operators can also connect an existing OpenAI-compatible endpoint. Save its Proxy Token pair in a private file:

```json
{
  "MODAL_PROXY_TOKEN_ID": "YOUR_PROXY_TOKEN_ID",
  "MODAL_PROXY_TOKEN_SECRET": "YOUR_PROXY_TOKEN_SECRET"
}
```

```sh
magic provider configure modal --secret-file ./modal-credentials.json --option inference_url=https://YOUR_ENDPOINT/v1 --option inference_secret_file=/absolute/path/modal-proxy-credentials.json
```

This configuration uses the supplied endpoint and credentials. The API base URL includes `/v1`; the provider appends `/chat/completions`. Rust hosts can call `with_inference_endpoint` and `with_inference_credential`. `ModalProvider::from_env` accepts `MAGICMTN_MODAL_INFERENCE_URL` plus `MAGICMTN_MODAL_INFERENCE_SECRET_FILE`, or an explicitly supplied `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET` environment pair.

## Sandbox behavior

Each sandbox uses Modal's VM runtime with one CPU, 512 MiB of memory, a writable `/workspace`, and outbound networking. Commands run as the `sandbox` user. Supply a wall-clock lifetime from 1 to 86,400 seconds through the host. Direct library calls to `create` use one hour. Create a new sandbox when the lifetime expires.

Bounded execution reports stdout, stderr, exit code, elapsed time, and timeout and truncation flags. Use explicit shell arguments such as `/bin/sh -c` for shell syntax. After an uncertain timeout, inspect the sandbox in Modal before retrying. Sandboxes are named `magicmtn-session-<session UUID>`.
