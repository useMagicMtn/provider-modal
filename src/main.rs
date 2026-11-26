use anyhow::{Result, bail, ensure};
use magicmtn_provider_modal::ModalProvider;
use magicmtn_sdk::{
    BoxFuture, InferenceBackend, SandboxBackend,
    plugin::{Capabilities, CredentialSpec, Operation, SandboxOperation::*, Value},
    provider_setup::{ConfigurationOption, SetupSpec},
};
use std::sync::Arc;

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let mut capabilities =
        Capabilities::sandbox(vec![PrepareImage, Create, Terminate, Exec, ExecBounded]);
    capabilities.inference_tasks = vec!["chat".into()];
    let setup = SetupSpec {
        options: [
            (
                "app".into(),
                ConfigurationOption {
                    description: "Modal app name".into(),
                    default: Some("magicmtn".into()),
                },
            ),
            (
                "inference_url".into(),
                ConfigurationOption {
                    description: "Modal OpenAI-compatible HTTPS API base URL, including /v1".into(),
                    default: None,
                },
            ),
            (
                "inference_secret_file".into(),
                ConfigurationOption {
                    description:
                        "Absolute path to a private JSON file containing Modal Proxy Tokens".into(),
                    default: None,
                },
            ),
        ]
        .into(),
    };
    magicmtn_provider_framework::run_credentialed_with_setup(
        "modal",
        env!("CARGO_PKG_VERSION"),
        capabilities,
        CredentialSpec {
            fields: vec!["MODAL_TOKEN_ID".into(), "MODAL_TOKEN_SECRET".into()],
        },
        setup,
        |credential| async move {
            let provider = Arc::new(ModalProvider::from_env().with_credential(credential));
            let backend = provider.clone();
            let dispatch = move |operation| -> BoxFuture<'static, Value> {
                let provider = provider.clone();
                Box::pin(async move {
                    match operation {
                        Operation::Infer { request } => {
                            provider.infer(&request).await.map(Value::Inference)
                        }
                        Operation::SandboxPrepareImage { images } => provider
                            .prepare_image(&images)
                            .await
                            .map(Value::PreparedImage),
                        Operation::SandboxCreate {
                            template,
                            ttl_seconds,
                        } => {
                            let sandbox = if let Some(seconds) = ttl_seconds {
                                ensure!(seconds > 0, "sandbox TTL must be positive");
                                provider.create_with_ttl(&template, seconds).await?
                            } else {
                                provider.create(&template).await?
                            };
                            Ok(Value::Sandbox(sandbox))
                        }
                        Operation::SandboxTerminate { sandbox } => {
                            provider.terminate(&sandbox).await?;
                            Ok(Value::Done)
                        }
                        Operation::SandboxExec {
                            sandbox,
                            command,
                            args,
                        } => provider
                            .exec(&sandbox, &command, &args)
                            .await
                            .map(Value::Text),
                        Operation::SandboxExecBounded {
                            sandbox,
                            command,
                            args,
                            timeout_ms,
                            max_output_bytes,
                        } => provider
                            .exec_bounded(&sandbox, &command, &args, timeout_ms, max_output_bytes)
                            .await
                            .map(Value::Exec),
                        _ => bail!("unsupported Modal operation"),
                    }
                })
            };
            Ok((backend, dispatch))
        },
    )
    .await
}
