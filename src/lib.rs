//! Modal sandboxes and OpenAI-compatible chat; the adapter is embedded in the plugin.
mod deployments;
mod inference;
use anyhow::{Context, Result, bail, ensure};
use magicmtn_sdk::plugin::CredentialValues;
use magicmtn_sdk::{BoxFuture, ExecResult, ProviderSandbox, SandboxBackend, ScpDirection};
use serde_json::{Value, json};
use std::{path::Path, process::Stdio, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
const ADAPTER: &str = include_str!("modal_bridge.py");

#[derive(Clone, Debug)]
pub struct ModalProvider {
    python: String,
    credential: Option<Arc<CredentialValues>>,
    inference_endpoint: Option<String>,
    inference_credential: Option<Arc<CredentialValues>>,
    inference_credential_file: Option<std::path::PathBuf>,
    state_directory: Option<std::path::PathBuf>,
}
impl ModalProvider {
    pub fn new(python: impl Into<String>) -> Self {
        Self {
            python: python.into(),
            credential: None,
            inference_endpoint: None,
            inference_credential: None,
            inference_credential_file: None,
            state_directory: None,
        }
    }
    pub fn from_env() -> Self {
        let mut provider =
            Self::new(std::env::var("MAGICMTN_MODAL_PYTHON").unwrap_or_else(|_| "python3".into()));
        provider.inference_endpoint = std::env::var("MAGICMTN_MODAL_INFERENCE_URL")
            .ok()
            .filter(|value| !value.is_empty());
        provider.state_directory =
            std::env::var_os("MAGICMTN_MODAL_STATE_DIRECTORY").map(Into::into);
        provider.inference_credential_file =
            std::env::var_os("MAGICMTN_MODAL_INFERENCE_SECRET_FILE").map(Into::into);
        let values: std::collections::BTreeMap<String, String> =
            ["MODAL_PROXY_TOKEN_ID", "MODAL_PROXY_TOKEN_SECRET"]
                .into_iter()
                .filter_map(|key| std::env::var(key).ok().map(|value| (key.into(), value)))
                .collect();
        if !values.is_empty() {
            provider.inference_credential = Some(Arc::new(CredentialValues(values)));
        }
        provider
    }
    pub fn with_credential(mut self, credential: CredentialValues) -> Self {
        self.credential = Some(Arc::new(credential));
        self
    }

    pub async fn verify_account(&self) -> Result<String> {
        ensure!(self.credential.is_some(), "Modal credentials are required");
        let result = self.call(json!({"operation": "verify_account"})).await?;
        let app_id = result.as_str().context("Modal did not return an app ID")?;
        ensure!(
            app_id.starts_with("ap-")
                && app_id.len() <= 128
                && app_id[3..].bytes().all(|byte| byte.is_ascii_alphanumeric())
                && app_id.len() > 3,
            "invalid Modal app ID"
        );
        Ok(app_id.to_owned())
    }

    async fn call(&self, request: Value) -> Result<Value> {
        self.call_with_credential(request, self.credential.as_deref())
            .await
    }

    async fn call_with_credential(
        &self,
        mut request: Value,
        credential: Option<&CredentialValues>,
    ) -> Result<Value> {
        if let Some(directory) = &self.state_directory {
            request["state_directory"] = json!(directory);
        }
        let inference = matches!(
            request["operation"].as_str(),
            Some("infer" | "infer_endpoint")
        );
        let deadline = if inference {
            310
        } else if request["operation"] == "prepare_image" {
            1700
        } else {
            55
        };
        let mut command = Command::new(&self.python);
        command
            .env_remove("MODAL_PROXY_TOKEN_ID")
            .env_remove("MODAL_PROXY_TOKEN_SECRET");
        if inference || self.credential.is_some() {
            command
                .env_remove("MODAL_TOKEN_ID")
                .env_remove("MODAL_TOKEN_SECRET");
        }
        let mut child = command
            .args(["-c", ADAPTER])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .context("could not start Modal Python; set MAGICMTN_MODAL_PYTHON")?;
        let mut input = child.stdin.take().context("adapter stdin missing")?;
        let output = child.stdout.take().context("adapter stdout missing")?;
        tokio::time::timeout(Duration::from_secs(deadline), async {
            let payload = zeroize::Zeroizing::new(serde_json::to_vec(&(&request, credential))?);
            input.write_all(&payload).await?;
            drop(payload);
            drop(input);
            let mut bytes = Vec::new();
            let limit = if inference {
                4 * 1024 * 1024
            } else {
                256 * 1024
            };
            output.take(limit + 1).read_to_end(&mut bytes).await?;
            ensure!(
                bytes.len() as u64 <= limit,
                "Modal adapter response too large"
            );
            ensure!(
                child.wait().await?.success(),
                "Modal operation failed; check SDK installation, credentials, and sandbox state"
            );
            let value: Value = serde_json::from_slice(&bytes)?;
            if let Some(error) = value.get("error").and_then(Value::as_str) {
                return Err(magicmtn_sdk::plugin::ProviderError {
                    code: magicmtn_sdk::plugin::ErrorCode::UpstreamUnavailable,
                    retryable: false,
                    message: error.into(),
                }
                .into());
            }
            Ok::<_, anyhow::Error>(value)
        })
        .await
        .context("Modal operation timed out; remote execution may still be active")?
    }
}

fn image_spec(template: &str) -> Result<(&str, uuid::Uuid)> {
    let (image, session) = template.strip_prefix("image:")
        .and_then(|value| value.rsplit_once('|'))
        .context("Modal requires image:<OCI reference>|<session UUID>; Dockerfile templates are unsupported")?;
    ensure!(
        !image.is_empty()
            && image.len() <= 512
            && !image.starts_with('-')
            && image
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"/_.:@-".contains(&c)),
        "invalid OCI image reference"
    );
    let session = uuid::Uuid::parse_str(session).context("invalid session UUID")?;
    ensure!(!session.is_nil(), "session UUID must not be nil");
    Ok((image, session))
}
fn sandbox_id(sandbox: &ProviderSandbox) -> Result<()> {
    ensure!(
        sandbox.provider_id.starts_with("sb-")
            && sandbox.provider_id.len() <= 128
            && sandbox
                .provider_id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-'),
        "invalid Modal sandbox ID"
    );
    Ok(())
}
impl SandboxBackend for ModalProvider {
    fn prepare_image<'a>(
        &'a self,
        images: &'a std::collections::BTreeMap<String, magicmtn_sdk::image_registry::Image>,
    ) -> BoxFuture<'a, magicmtn_sdk::image_registry::PreparedImage> {
        Box::pin(async move {
            let image = images
                .get("linux/amd64")
                .context("Modal requires a linux/amd64 image")?;
            ensure!(
                image
                    .digest
                    .strip_prefix("sha256:")
                    .is_some_and(|hash| hash.len() == 64
                        && hash.bytes().all(|byte| byte.is_ascii_hexdigit())),
                "invalid image digest"
            );
            let result = self
                .call(json!({"operation":"prepare_image","image":image.reference()}))
                .await?;
            let prepared: magicmtn_sdk::image_registry::PreparedImage =
                serde_json::from_value(result)?;
            ensure!(
                prepared.image == *image && prepared.platform == "linux/amd64",
                "Modal prepared a different image"
            );
            Ok(prepared)
        })
    }
    fn create<'a>(&'a self, template: &'a str) -> BoxFuture<'a, ProviderSandbox> {
        Box::pin(async move {
            let (image, session) = image_spec(template)?;
            Ok(serde_json::from_value(
                self.call(
                    json!({"operation":"create","image":image,"session":session.to_string()}),
                )
                .await?,
            )?)
        })
    }
    fn create_with_ttl<'a>(
        &'a self,
        template: &'a str,
        ttl_seconds: u32,
    ) -> BoxFuture<'a, ProviderSandbox> {
        Box::pin(async move {
            ensure!(
                (1..=86_400).contains(&ttl_seconds),
                "invalid Modal sandbox TTL"
            );
            let (image, session) = image_spec(template)?;
            Ok(serde_json::from_value(self.call(json!({"operation":"create","image":image,"session":session.to_string(),"ttl_seconds":ttl_seconds})).await?)?)
        })
    }
    fn terminate<'a>(&'a self, sandbox: &'a ProviderSandbox) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            sandbox_id(sandbox)?;
            self.call(json!({"operation":"terminate","sandbox":sandbox}))
                .await?;
            Ok(())
        })
    }
    fn exec<'a>(
        &'a self,
        sandbox: &'a ProviderSandbox,
        command: &'a str,
        args: &'a [String],
    ) -> BoxFuture<'a, String> {
        Box::pin(async move {
            let result = self
                .exec_bounded(sandbox, command, args, 30_000, 16_384)
                .await?;
            ensure!(
                !result.timed_out,
                "Modal execution timed out; do not replay automatically"
            );
            ensure!(
                result.exit_code == Some(0),
                "Modal command exited with {:?}: {}",
                result.exit_code,
                result.stderr
            );
            Ok(result.stdout)
        })
    }
    fn exec_bounded<'a>(
        &'a self,
        sandbox: &'a ProviderSandbox,
        command: &'a str,
        args: &'a [String],
        timeout_ms: u64,
        max_output_bytes: usize,
    ) -> BoxFuture<'a, ExecResult> {
        Box::pin(async move {
            sandbox_id(sandbox)?;
            ensure!(
                (1..=30_000).contains(&timeout_ms) && (1..=16_384).contains(&max_output_bytes),
                "invalid exec limits"
            );
            ensure!(
                !command.is_empty()
                    && command.len() <= 256
                    && !command.starts_with('-')
                    && !command.contains('\0'),
                "invalid program"
            );
            ensure!(
                args.len() <= 32
                    && args
                        .iter()
                        .all(|arg| arg.len() <= 2048 && !arg.contains('\0')),
                "invalid arguments"
            );
            Ok(serde_json::from_value(self.call(json!({"operation":"exec","sandbox":sandbox,"command":command,"args":args,"timeout_ms":timeout_ms,"max_output_bytes":max_output_bytes})).await?)?)
        })
    }
    fn ssh<'a>(&'a self, _sandbox: &'a ProviderSandbox) -> BoxFuture<'a, ()> {
        Box::pin(async { bail!("interactive Modal shells are unsupported") })
    }
    fn scp<'a>(
        &'a self,
        _sandbox: &'a ProviderSandbox,
        _local: &'a Path,
        _remote: &'a Path,
        _direction: ScpDirection,
    ) -> BoxFuture<'a, ()> {
        Box::pin(async { bail!("Modal file transfer is unsupported") })
    }
}

impl magicmtn_sdk::provider_setup::ProviderSetup for ModalProvider {
    fn configure<'a>(
        &'a self,
        options: &'a std::collections::BTreeMap<String, String>,
    ) -> magicmtn_sdk::BoxFuture<'a, magicmtn_sdk::provider_setup::ConfiguredConnection> {
        Box::pin(async move {
            ensure!(self.credential.is_some(), "Modal credentials are required");
            let connection: magicmtn_sdk::provider_setup::ConfiguredConnection =
                serde_json::from_value(
                    self.call(json!({"operation":"configure", "options":options}))
                        .await?,
                )?;
            connection.validate()?;
            ensure!(
                connection.account.starts_with("ap-")
                    && connection.account.len() > 3
                    && connection.account.len() <= 128
                    && connection.account[3..]
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric()),
                "invalid Modal app ID"
            );
            Ok(connection)
        })
    }
    fn verify(&self) -> magicmtn_sdk::BoxFuture<'_, String> {
        Box::pin(self.verify_account())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test(flavor = "current_thread")]
    async fn preparation_rejects_wrong_platform_before_starting_python() {
        let provider = ModalProvider::new("/must-not-execute");
        let images = std::collections::BTreeMap::from([(
            "linux/arm64".into(),
            magicmtn_sdk::image_registry::Image {
                repository: "example/image".into(),
                digest: format!("sha256:{}", "a".repeat(64)),
            },
        )]);
        assert!(
            provider
                .prepare_image(&images)
                .await
                .unwrap_err()
                .to_string()
                .contains("linux/amd64")
        );
    }
    #[test]
    fn validates_session_image_and_remote_identity() {
        let session = "cee98b1a-b843-40f1-97d2-1bfc96c9c676";
        assert_eq!(
            image_spec(&format!("image:alpine:3.24.1|{session}"))
                .unwrap()
                .0,
            "alpine:3.24.1"
        );
        for template in [
            "/tmp/Containerfile",
            "image:alpine",
            "image:alpine|bad",
            "image:a b|cee98b1a-b843-40f1-97d2-1bfc96c9c676",
        ] {
            assert!(image_spec(template).is_err());
        }
        let mut sandbox = ProviderSandbox {
            provider_id: "sb-test".into(),
            status: None,
            create_time_ms: None,
            metadata: None,
        };
        assert!(sandbox_id(&sandbox).is_ok());
        sandbox.provider_id = "apple-container".into();
        assert!(sandbox_id(&sandbox).is_err());
    }
}
