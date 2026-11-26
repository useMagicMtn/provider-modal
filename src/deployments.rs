//! Lifecycle of provider-owned Modal inference endpoints.
use crate::ModalProvider;
use anyhow::{Result, ensure};
use magicmtn_sdk::{
    BoxFuture, InferenceDeployment, InferenceDeploymentBackend, InferenceDeploymentRef,
    InferenceDeploymentSpec,
};
use serde_json::{Value, json};

pub(crate) fn validate_endpoint_id(id: &str) -> Result<()> {
    ensure!(
        id.strip_prefix("ep-").is_some_and(|suffix| {
            suffix.len() == 22 && suffix.bytes().all(|b| b.is_ascii_alphanumeric())
        }),
        "invalid Modal endpoint ID"
    );
    Ok(())
}

impl ModalProvider {
    /// Use a private, persistent directory for resource identities and generated
    /// inference credentials. Local MagicMtn installations supply this during setup.
    pub fn with_state_directory(mut self, directory: impl Into<std::path::PathBuf>) -> Self {
        self.state_directory = Some(directory.into());
        self
    }

    async fn deployment_call(&self, operation: &str, arguments: Value) -> Result<Value> {
        ensure!(
            self.credential.is_some(),
            "Modal workspace credentials are required"
        );
        let mut request = arguments;
        request["operation"] = json!(operation);
        self.call(request).await
    }
}

impl InferenceDeploymentBackend for ModalProvider {
    fn create<'a>(
        &'a self,
        spec: &'a InferenceDeploymentSpec,
    ) -> BoxFuture<'a, InferenceDeployment> {
        Box::pin(async move {
            ensure!(
                !spec.name.is_empty()
                    && spec.name.len() <= 64
                    && spec
                        .name
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b)),
                "invalid Modal deployment name"
            );
            ensure!(
                !spec.model.is_empty()
                    && spec.model.len() <= 256
                    && spec
                        .model
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"/_.-".contains(&b)),
                "invalid Modal model identifier"
            );
            let value = self
                .deployment_call("endpoint_create", json!({"spec":spec}))
                .await?;
            Ok(serde_json::from_value(value)?)
        })
    }

    fn status<'a>(
        &'a self,
        resource: &'a InferenceDeploymentRef,
    ) -> BoxFuture<'a, InferenceDeployment> {
        Box::pin(async move {
            validate_endpoint_id(&resource.provider_id)?;
            let value = self
                .deployment_call(
                    "endpoint_status",
                    json!({"endpoint_id":resource.provider_id}),
                )
                .await?;
            Ok(serde_json::from_value(value)?)
        })
    }

    fn delete<'a>(&'a self, resource: &'a InferenceDeploymentRef) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            validate_endpoint_id(&resource.provider_id)?;
            self.deployment_call(
                "endpoint_delete",
                json!({"endpoint_id":resource.provider_id}),
            )
            .await?;
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test(flavor = "current_thread")]
    async fn rejects_invalid_resources_before_starting_python() {
        let provider = ModalProvider::new("/must-not-execute");
        let resource = InferenceDeploymentRef {
            provider_id: "other-provider".into(),
        };
        assert!(
            provider
                .status(&resource)
                .await
                .unwrap_err()
                .to_string()
                .contains("endpoint ID")
        );
        let spec = InferenceDeploymentSpec {
            name: "../unsafe".into(),
            model: "acme/ChosenModel".into(),
            configuration: Value::Null,
        };
        assert!(
            InferenceDeploymentBackend::create(&provider, &spec)
                .await
                .unwrap_err()
                .to_string()
                .contains("deployment name")
        );
    }
}
