//! Verify setup routing against the actual executable without a Modal account.
#![cfg(all(unix, feature = "plugin"))]
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    os::unix::fs::PermissionsExt,
    process::{Command, Stdio},
};

#[test]
fn configuration_and_verification_are_explicit_workload_startup_is_local() {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "magicmtn-modal-setup-{}-{stamp}",
        std::process::id()
    ));
    std::fs::create_dir(&root).unwrap();
    struct Cleanup(std::path::PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    let _cleanup = Cleanup(root.clone());
    let secret_file = root.join("proxy.json");
    std::fs::write(
        &secret_file,
        r#"{"MODAL_PROXY_TOKEN_ID":"wk-fixture","MODAL_PROXY_TOKEN_SECRET":"ws-fixture"}"#,
    )
    .unwrap();
    let log = root.join("calls");
    let python = root.join("python");
    let script = r#"#!/bin/sh
IFS= read -r payload || test -n "$payload"
case "$payload" in
 *'"operation":"configure"'*)
  printf 'configure\n' >> '__LOG__'
  printf '%s\n' '{"account":"ap-Fixture","configuration":{"MAGICMTN_MODAL_APP":"fixture","MAGICMTN_MODAL_APP_ID":"ap-Fixture"}}' ;;
 *'"operation":"verify_account"'*)
  printf 'verify\n' >> '__LOG__'
  printf '%s\n' '"ap-Fixture"' ;;
 *'"operation":"infer"'*)
  case "$payload" in *'"MODAL_TOKEN_ID"'*) exit 1 ;; esac
  case "$payload" in *'"MODAL_PROXY_TOKEN_ID":"wk-fixture"'*) ;; *) exit 1 ;; esac
  printf 'infer\n' >> '__LOG__'
  printf '%s\n' '{"model":"resolved","choices":[{"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],"usage":{"total_tokens":2}}' ;;
 *'"operation":"endpoint_create"'*|*'"operation":"endpoint_status"'*)
  printf 'deployment\n' >> '__LOG__'
  printf '%s\n' '{"resource":{"provider_id":"__ENDPOINT__"},"model":"moonshotai/Kimi-K3","inference_model":"endpoint:__ENDPOINT__","state":"ready","ready_replicas":null}' ;;
 *'"operation":"endpoint_delete"'*)
  printf 'delete\n' >> '__LOG__'
  printf '%s\n' 'null' ;;
 *'"operation":"infer_endpoint"'*)
  case "$payload" in *'"MODAL_PROXY_TOKEN_ID"'*) exit 1 ;; esac
  case "$payload" in *'"MODAL_TOKEN_ID":"fixture"'*) ;; *) exit 1 ;; esac
  printf 'managed-infer\n' >> '__LOG__'
  printf '%s\n' '{"model":"managed-model","choices":[{"message":{"role":"assistant","content":"managed hello"},"finish_reason":"stop"}]}' ;;
 *'"operation":"terminate"'*)
  printf 'terminate\n' >> '__LOG__'
  printf '%s\n' 'null' ;;
 *) exit 1 ;;
esac
"#.replace("__LOG__", &log.to_string_lossy().replace('\'', "'\\''")).replace("__ENDPOINT__", &format!("ep-{}", "A".repeat(22)));
    std::fs::write(&python, script).unwrap();
    std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755)).unwrap();
    let executable = std::env::var_os("MAGICMTN_TEST_PROVIDER")
        .or_else(|| {
            option_env!("CARGO_BIN_EXE_magicmtn-provider-modal").map(std::ffi::OsString::from)
        })
        .expect("provider executable required");
    let startup = |credential, verify| json!({"id":1,"protocol_version":2,"provider":"modal","credential":credential,"verify_account":verify});
    let credential = json!({"MODAL_TOKEN_ID":"fixture","MODAL_TOKEN_SECRET":"fixture"});
    let mut command = Command::new(executable);
    command
        .env_clear()
        .env("MAGICMTN_MODAL_PYTHON", &python)
        .env(
            "MAGICMTN_MODAL_INFERENCE_URL",
            "https://fixture.modal.run/v1",
        )
        .env("MAGICMTN_MODAL_INFERENCE_SECRET_FILE", &secret_file)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped());
    let exchange = |child: &mut std::process::Child, input: Value| -> Value {
        writeln!(child.stdin.as_mut().unwrap(), "{input}").unwrap();
        let mut output = String::new();
        BufReader::new(child.stdout.as_mut().unwrap())
            .read_line(&mut output)
            .unwrap();
        serde_json::from_str(&output).unwrap()
    };
    let mut discovery = command.spawn().unwrap();
    let manifest = exchange(&mut discovery, startup(Value::Null, false));
    assert!(manifest["outcome"]["ok"]["value"]["capabilities"]["setup"].is_object());
    assert_eq!(
        manifest["outcome"]["ok"]["value"]["capabilities"]["inference_tasks"],
        json!(["chat"])
    );
    assert_eq!(
        manifest["outcome"]["ok"]["value"]["capabilities"]["inference_deployment_operations"],
        json!(["create", "status", "delete"])
    );
    assert!(
        manifest["outcome"]["ok"]["value"]["capabilities"]["setup"]["options"]["state_directory"]
            .is_object()
    );
    assert!(
        manifest["outcome"]["ok"]["value"]["capabilities"]["setup"]["options"]
            .get("model")
            .is_none()
    );
    let choices = &manifest["outcome"]["ok"]["value"]["capabilities"]["inference_models"];
    assert_eq!(choices[0]["id"], "moonshotai/Kimi-K3");
    assert_eq!(choices[0]["name"], "Kimi K3");
    assert!(
        manifest["outcome"]["ok"]["value"]["capabilities"]
            .get("default_model")
            .is_none()
    );
    assert!(discovery.wait().unwrap().success());
    assert!(!log.exists());

    let mut provider = command.spawn().unwrap();
    assert_eq!(
        exchange(&mut provider, startup(credential.clone(), false))["outcome"]["ok"]["kind"],
        "description"
    );
    assert!(
        !log.exists(),
        "normal startup must not verify or configure an account"
    );
    let operation = |id, name, arguments| json!({"id":id,"protocol_version":2,"provider":"modal","operation":{"name":name,"arguments":arguments}});
    let configured = exchange(
        &mut provider,
        operation(
            2,
            "provider_configure",
            json!({"options":{"app":"fixture"}}),
        ),
    );
    assert_eq!(
        configured["outcome"]["ok"]["value"]["account"],
        "ap-Fixture"
    );
    let terminated = exchange(
        &mut provider,
        operation(
            3,
            "sandbox_terminate",
            json!({"sandbox":{"provider_id":"sb-Fixture","status":null,"create_time_ms":null,"metadata":null}}),
        ),
    );
    assert_eq!(terminated["outcome"]["ok"]["kind"], "done");
    let inferred = exchange(
        &mut provider,
        operation(
            4,
            "infer",
            json!({"request": {
                "model": {"model":"test","provider":"modal","version":null,"parameters":null},
                "task":"chat","input":{"messages":[{"role":"user","content":"Hi"}]}
            }}),
        ),
    );
    assert_eq!(inferred["outcome"]["ok"]["kind"], "inference");
    assert_eq!(inferred["outcome"]["ok"]["value"]["model"], "resolved");
    assert_eq!(
        inferred["outcome"]["ok"]["value"]["output"]["message"]["content"],
        "hello"
    );
    let endpoint = format!("ep-{}", "A".repeat(22));
    let deployment = exchange(
        &mut provider,
        operation(
            5,
            "inference_deployment_create",
            json!({"spec":{
                "name":"example", "model":"moonshotai/Kimi-K3", "configuration":{}
            }}),
        ),
    );
    assert_eq!(
        deployment["outcome"]["ok"]["value"]["resource"]["provider_id"],
        endpoint
    );
    let resource = json!({"provider_id":endpoint});
    let status = exchange(
        &mut provider,
        operation(
            6,
            "inference_deployment_status",
            json!({"resource":resource}),
        ),
    );
    assert_eq!(status["outcome"]["ok"]["value"]["state"], "ready");
    let managed = exchange(
        &mut provider,
        operation(
            7,
            "infer",
            json!({"request":{
                "model":{"model":format!("endpoint:{endpoint}"),"provider":"modal","version":null,"parameters":null},
                "task":"chat","input":{"messages":[{"role":"user","content":"Hi"}]}
            }}),
        ),
    );
    assert_eq!(
        managed["outcome"]["ok"]["value"]["output"]["message"]["content"],
        "managed hello"
    );
    let deleted = exchange(
        &mut provider,
        operation(
            8,
            "inference_deployment_delete",
            json!({"resource":resource}),
        ),
    );
    assert_eq!(deleted["outcome"]["ok"]["kind"], "done");
    exchange(&mut provider, operation(9, "shutdown", Value::Null));
    assert!(provider.wait().unwrap().success());
    assert_eq!(
        std::fs::read_to_string(&log).unwrap(),
        "configure\nterminate\ninfer\ndeployment\ndeployment\nmanaged-infer\ndelete\n"
    );

    let mut verification = command.spawn().unwrap();
    let verified = exchange(&mut verification, startup(credential, true));
    assert_eq!(verified["outcome"]["ok"]["kind"], "verified_account");
    assert_eq!(verified["outcome"]["ok"]["value"], "ap-Fixture");
    assert!(verification.wait().unwrap().success());
    assert_eq!(
        std::fs::read_to_string(&log).unwrap(),
        "configure\nterminate\ninfer\ndeployment\ndeployment\nmanaged-infer\ndelete\nverify\n"
    );
}
