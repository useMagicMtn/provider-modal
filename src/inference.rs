//! Translate the SDK chat schema to Modal's OpenAI-compatible endpoints.
use crate::ModalProvider;
use anyhow::{Context, Result, ensure};
use magicmtn_sdk::{
    BoxFuture, InferenceBackend, InferenceRequest, InferenceResponse,
    plugin::{CredentialSpec, CredentialValues, ErrorCode, ProviderError},
};
use serde_json::{Value, json};
use std::sync::Arc;

impl ModalProvider {
    /// Set the endpoint's OpenAI API base URL, including `/v1`.
    pub fn with_inference_endpoint(mut self, endpoint: impl Into<String>) -> Self {
        self.inference_endpoint = Some(endpoint.into());
        self
    }

    /// Attach `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET` separately
    /// from the workspace credentials used by sandbox operations.
    pub fn with_inference_credential(mut self, credential: CredentialValues) -> Self {
        self.inference_credential = Some(Arc::new(credential));
        self
    }

    async fn infer_inner(&self, request: &InferenceRequest) -> Result<InferenceResponse> {
        if request.task != "chat" {
            return Err(error(
                ErrorCode::UnsupportedOperation,
                "unsupported Modal inference task",
            ));
        }
        let body = chat_body(request)
            .map_err(|_| error(ErrorCode::InvalidRequest, "invalid Modal chat request"))?;
        if let Some(endpoint_id) = request.model.model.strip_prefix("endpoint:") {
            super::deployments::validate_endpoint_id(endpoint_id)?;
            ensure!(
                self.credential.is_some(),
                "Modal workspace credentials are required"
            );
            let raw = self
                .call(json!({"operation":"infer_endpoint", "endpoint_id":endpoint_id, "body":body}))
                .await?;
            return chat_response(&raw).map_err(|_| {
                error(
                    ErrorCode::UpstreamUnavailable,
                    "Modal returned an invalid chat response",
                )
            });
        }
        let endpoint = self
            .inference_endpoint
            .as_deref()
            .filter(|url| !url.is_empty())
            .context("configure MAGICMTN_MODAL_INFERENCE_URL or with_inference_endpoint")?;
        let file_credential = self
            .inference_credential_file
            .as_deref()
            .filter(|_| self.inference_credential.is_none())
            .map(read_credential)
            .transpose()?;
        let credential = self
            .inference_credential
            .as_deref()
            .or(file_credential.as_ref())
            .context("Modal inference requires Proxy Token credentials")?;
        CredentialSpec {
            fields: vec![
                "MODAL_PROXY_TOKEN_ID".into(),
                "MODAL_PROXY_TOKEN_SECRET".into(),
            ],
        }
        .check_values(credential)?;
        let raw = self
            .call_with_credential(
                json!({"operation":"infer", "endpoint":endpoint, "body":body}),
                Some(credential),
            )
            .await?;
        chat_response(&raw).map_err(|_| {
            error(
                ErrorCode::UpstreamUnavailable,
                "Modal returned an invalid chat response",
            )
        })
    }
}

fn read_credential(path: &std::path::Path) -> Result<CredentialValues> {
    use std::io::Read;
    ensure!(
        path.is_absolute(),
        "Modal inference secret file must use an absolute path"
    );
    let file = std::fs::File::open(path)
        .map_err(|_| anyhow::anyhow!("could not open Modal inference secret file"))?;
    let mut bytes = zeroize::Zeroizing::new(Vec::new());
    file.take(16 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| anyhow::anyhow!("could not read Modal inference secret file"))?;
    ensure!(
        bytes.len() <= 16 * 1024,
        "Modal inference secret file exceeds 16 KiB"
    );
    serde_json::from_slice(&bytes)
        .map_err(|_| anyhow::anyhow!("invalid Modal inference secret file"))
}

fn error(code: ErrorCode, message: &str) -> anyhow::Error {
    ProviderError {
        code,
        retryable: false,
        message: message.into(),
    }
    .into()
}

fn chat_body(request: &InferenceRequest) -> Result<Value> {
    ensure!(request.task == "chat", "unsupported inference task");
    ensure!(
        request
            .model
            .provider
            .as_deref()
            .is_none_or(|p| p == "modal"),
        "incorrect provider"
    );
    ensure!(
        !request.model.model.trim().is_empty() && request.model.version.is_none(),
        "invalid model"
    );
    let mut body = if request.model.parameters.is_null() {
        json!({})
    } else {
        request.model.parameters.clone()
    };
    let parameters = body.as_object().context("parameters must be an object")?;
    const ALLOWED: &[&str] = &[
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "repetition_penalty",
        "seed",
        "response_format",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning_effort",
    ];
    ensure!(
        parameters.keys().all(|key| ALLOWED.contains(&key.as_str())),
        "unsupported parameter"
    );
    ensure!(
        request
            .input
            .as_object()
            .is_some_and(|o| o.keys().all(|k| matches!(k.as_str(), "messages" | "tools"))),
        "unsupported input field"
    );
    let input = magicmtn_sdk::chat::normalize_input(&request.input)?;
    let messages = input["messages"].as_array().context("messages required")?;
    ensure!(!messages.is_empty(), "messages required");
    for message in messages {
        ensure!(
            matches!(
                message["role"].as_str(),
                Some("system" | "developer" | "user" | "assistant" | "tool")
            ),
            "invalid role"
        );
        ensure!(message["content"].is_string(), "text content required");
        if message["role"] == "tool" {
            ensure!(
                message["tool_call_id"]
                    .as_str()
                    .is_some_and(|id| !id.is_empty()),
                "tool result requires call ID"
            );
        }
    }
    body["model"] = json!(request.model.model);
    body["messages"] = input["messages"].clone();
    if let Some(tools) = input.get("tools") {
        ensure!(tools.is_array(), "tools must be an array");
        body["tools"] = tools.clone();
    }
    body["stream"] = json!(false);
    Ok(body)
}

fn chat_response(raw: &Value) -> Result<InferenceResponse> {
    let model = raw["model"]
        .as_str()
        .filter(|model| !model.is_empty())
        .context("response model required")?;
    let choices = raw["choices"].as_array().context("choices required")?;
    ensure!(choices.len() == 1, "exactly one choice required");
    let prefix = format!(
        "call_{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos()
    );
    let message = magicmtn_sdk::chat::normalize_message(&choices[0]["message"], &prefix)?;
    ensure!(
        message["role"] == "assistant" && message["content"].is_string(),
        "assistant text required"
    );
    let finish = choices[0]["finish_reason"]
        .as_str()
        .context("finish reason required")?;
    ensure!(
        matches!(finish, "stop" | "length" | "tool_calls" | "content_filter"),
        "invalid finish reason"
    );
    let usage = raw
        .get("usage")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or(json!({}));
    ensure!(usage.is_object(), "usage must be an object");
    Ok(InferenceResponse {
        model: model.into(),
        output: json!({"message":message,"usage":usage,"finish_reason":finish}),
    })
}

impl InferenceBackend for ModalProvider {
    fn infer<'a>(&'a self, request: &'a InferenceRequest) -> BoxFuture<'a, InferenceResponse> {
        Box::pin(async move { self.infer_inner(request).await })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request() -> InferenceRequest {
        serde_json::from_value(json!({"model":{"model":"test-model","provider":"modal","version":null,"parameters":{"max_tokens":32}},"task":"chat","input":{"messages":[{"role":"user","content":"Hello"}]}})).unwrap()
    }
    #[test]
    fn translates_chat_and_rejects_overrides() {
        let mut request = request();
        let body = chat_body(&request).unwrap();
        assert_eq!(body["model"], "test-model");
        assert_eq!(body["max_tokens"], 32);
        assert_eq!(body["stream"], false);
        for key in ["stream", "model", "messages", "n", "unknown"] {
            request.model.parameters = json!({key:true});
            assert!(chat_body(&request).is_err());
        }
        request.model.parameters = Value::Null;
        request.task = "embedding".into();
        assert!(chat_body(&request).is_err());
        request.task = "chat".into();
        request.model.provider = Some("other".into());
        assert!(chat_body(&request).is_err());
    }
    #[test]
    fn normalizes_tool_history_and_responses() {
        let mut request = request();
        request.input = json!({"messages":[{"role":"assistant","content":null,"tool_calls":[{"function":{"name":"run","arguments":{}}}]},{"role":"tool","tool_name":"run","content":"done"}]});
        let body = chat_body(&request).unwrap();
        assert_eq!(
            body["messages"][0]["tool_calls"][0]["id"],
            body["messages"][1]["tool_call_id"]
        );
        let mut raw = json!({"model":"resolved","choices":[{"message":{"role":"assistant","content":null,"reasoning_content":"thinking","tool_calls":[{"id":"call1","function":{"name":"run","arguments":{}}}]},"finish_reason":"tool_calls"}],"usage":{"total_tokens":42}});
        let result = chat_response(&raw).unwrap();
        assert_eq!(result.model, "resolved");
        assert_eq!(
            result.output["message"]["tool_calls"][0]["function"]["arguments"],
            "{}"
        );
        assert_eq!(result.output["usage"]["total_tokens"], 42);
        assert_eq!(result.output["message"]["reasoning_content"], "thinking");
        raw["choices"][0]["finish_reason"] = json!("error");
        assert!(chat_response(&raw).is_err());
        assert!(chat_response(&json!({"model":"x","choices":[]})).is_err());
    }
    #[tokio::test(flavor = "current_thread")]
    async fn managed_resource_reference_requires_workspace_credentials() {
        let provider = ModalProvider::new("/must-not-execute")
            .with_inference_endpoint("https://fixture.modal.direct/v1");
        let mut request = request();
        request.model.model = format!("endpoint:ep-{}", "A".repeat(22));
        assert!(
            provider
                .infer(&request)
                .await
                .unwrap_err()
                .to_string()
                .contains("workspace credentials")
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn validates_before_starting_adapter() {
        let mut invalid = request();
        invalid.input = json!({"messages":[]});
        let error = ModalProvider::new("/must-not-execute")
            .infer(&invalid)
            .await
            .unwrap_err();
        assert!(matches!(
            error.downcast_ref::<ProviderError>().unwrap().code,
            ErrorCode::InvalidRequest
        ));
    }
}
