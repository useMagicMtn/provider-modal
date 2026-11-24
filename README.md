<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/logo-dark.svg">
    <img src=".github/assets/logo-light.svg" alt="MagicMtn logo" width="180" height="180">
  </picture>
</p>

# Modal provider for MagicMtn

Run MagicMtn sandboxes on [Modal](https://modal.com/). Launch a Linux VM from a prepared image, run commands in its workspace, and terminate it from a standalone MagicMtn installation or a Rust SDK host.

## Set up Modal

Use a Modal workspace with a token and a Linux AMD64 sandbox image. Build the image in Modal and map its exact OCI digest to the resulting Modal image ID. The [setup guide](USAGE.md#prepare-a-sandbox-image) includes a preparation command for the MagicMtn Canonical image.

## Build and install

Install [MagicMtn](https://github.com/useMagicMtn/magicmtn#install-from-source), Rust with edition 2024 support, and Python 3.11 or newer. Keep this checkout beside the `magicmtn` checkout and use the [pinned SDK revision](MODULE.bazel). See the [Python setup](USAGE.md#install-the-modal-sdk) for installing the Modal SDK in the interpreter your host uses.

```sh
test -f .cargo/config.toml || cp .cargo/config.toml.example .cargo/config.toml
./package.sh
magic provider install --file PATH_TO_PACKAGE
```

Replace `PATH_TO_PACKAGE` with the path printed by `package.sh`. Start the local server with the Modal app and image mapping, then configure the token with `magic --local provider configure modal --secret-file ./modal-credentials.json`. Follow the [setup guide](USAGE.md#configure-a-standalone-server) for the file format and environment settings.

## Run a sandbox

Set `IMAGE` to the same digest-pinned reference used in the image mapping.

```sh
sandbox_id=$(magic --local computer new --provider modal --image "$IMAGE")
magic --local computer exec "$sandbox_id" -- /bin/echo "Hello from Modal"
magic --local computer terminate "$sandbox_id" --yes
```

See the [usage guide](USAGE.md) for runtime behavior and SDK integration, and [CONTRIBUTING.md](CONTRIBUTING.md) for development and packaging.

## License

[Apache License 2.0](LICENSE).
