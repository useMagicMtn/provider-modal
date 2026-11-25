<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/logo-dark.svg">
    <img src=".github/assets/logo-light.svg" alt="MagicMtn logo" width="180" height="180">
  </picture>
</p>

# Modal provider for MagicMtn

Run MagicMtn sandboxes on [Modal](https://modal.com/). Launch a Linux VM from a prepared image, run commands in its workspace, and terminate it from a standalone MagicMtn installation or a Rust SDK host.

## Set up Modal

Install the provider with `magic provider install modal` and configure a token for your Modal workspace. Installation includes a private Python runtime and the Modal SDK. Configuration creates or selects an app named `magicmtn`; use `--option app=NAME` to choose another name. MagicMtn prepares Canonical or your custom Linux AMD64 image on Modal and saves the resulting image ID locally. See the [setup guide](USAGE.md#prepare-images-with-magicmtn).

## Build and install

Install [MagicMtn](https://github.com/useMagicMtn/magicmtn#install-from-source), Rust with edition 2024 support, Python 3, and `uv==0.13.10`. Keep this checkout beside the `magicmtn` checkout and use the [pinned SDK revision](MODULE.bazel). Packaging builds the private Python runtime and installs the hash-locked dependencies into it.

```sh
test -f .cargo/config.toml || cp .cargo/config.toml.example .cargo/config.toml
./package.sh
magic provider install --file PATH_TO_PACKAGE
```

Replace `PATH_TO_PACKAGE` with the path printed by `package.sh`. Start the local server, then configure the token with `magic provider configure modal --secret-file ./modal-credentials.json`. Follow the [setup guide](USAGE.md#prepare-images-with-magicmtn) for image preparation.

## Run a sandbox

Prepare Canonical and create a sandbox from that catalog selection:

```sh
magic images prepare canonical --provider modal
sandbox_id=$(magic computer new --provider modal --image canonical)
magic computer exec "$sandbox_id" -- /bin/echo "Hello from Modal"
magic computer terminate "$sandbox_id" --yes
```

See the [usage guide](USAGE.md) for runtime behavior and SDK integration, and [CONTRIBUTING.md](CONTRIBUTING.md) for development and packaging.

## License

[Apache License 2.0](LICENSE).
