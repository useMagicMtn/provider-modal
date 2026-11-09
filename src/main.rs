use anyhow::Result;
use magicmtn_provider_modal::ModalProvider;
use magicmtn_sdk::plugin::{Capabilities, SandboxOperation::*};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    magicmtn_provider_framework::run_sandbox(
        "modal",
        env!("CARGO_PKG_VERSION"),
        Capabilities::sandbox(vec![Create, Terminate, Exec, ExecBounded]),
        ModalProvider::from_env(),
    )
    .await
}
