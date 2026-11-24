use anyhow::{Result, bail, ensure};
use magicmtn_provider_modal::ModalProvider;
use magicmtn_sdk::{
    BoxFuture, SandboxBackend,
    plugin::{Capabilities, CredentialSpec, Operation, SandboxOperation::*, Value},
};
use std::sync::Arc;

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    magicmtn_provider_framework::run_credentialed(
        "modal",
        env!("CARGO_PKG_VERSION"),
        Capabilities::sandbox(vec![Create, Terminate, Exec, ExecBounded]),
        CredentialSpec {
            fields: vec!["MODAL_TOKEN_ID".into(), "MODAL_TOKEN_SECRET".into()],
        },
        |credential, verify_account| async move {
            let provider = Arc::new(ModalProvider::from_env().with_credential(credential));
            let account = if verify_account {
                Some(provider.verify_account().await?)
            } else {
                None
            };
            let dispatch = move |operation| -> BoxFuture<'static, Value> {
                let provider = provider.clone();
                Box::pin(async move {
                    match operation {
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
                        _ => bail!("unsupported sandbox operation"),
                    }
                })
            };
            Ok((account, dispatch))
        },
    )
    .await
}
