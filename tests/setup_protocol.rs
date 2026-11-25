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
 *'"operation":"terminate"'*)
  printf 'terminate\n' >> '__LOG__'
  printf '%s\n' 'null' ;;
 *) exit 1 ;;
esac
"#.replace("__LOG__", &log.to_string_lossy().replace('\'', "'\\''"));
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
        .env("MAGICMTN_MODAL_PYTHON", &python)
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
    exchange(&mut provider, operation(4, "shutdown", Value::Null));
    assert!(provider.wait().unwrap().success());
    assert_eq!(
        std::fs::read_to_string(&log).unwrap(),
        "configure\nterminate\n"
    );

    let mut verification = command.spawn().unwrap();
    let verified = exchange(&mut verification, startup(credential, true));
    assert_eq!(verified["outcome"]["ok"]["kind"], "verified_account");
    assert_eq!(verified["outcome"]["ok"]["value"], "ap-Fixture");
    assert!(verification.wait().unwrap().success());
    assert_eq!(
        std::fs::read_to_string(&log).unwrap(),
        "configure\nterminate\nverify\n"
    );
}
