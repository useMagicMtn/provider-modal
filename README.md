<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/logo-dark.svg">
    <img src=".github/assets/logo-light.svg" alt="MagicMtn logo" width="180" height="180">
  </picture>
</p>

# Modal provider for MagicMtn

Run MagicMtn sandboxes on [Modal](https://modal.com/). Launch a Linux VM from a prepared image, run commands in its workspace, and terminate it from a standalone MagicMtn installation or a Rust SDK host.

## Set up Modal

Use a Modal workspace with a token and an app named `magicmtn`. MagicMtn prepares Canonical or your custom Linux AMD64 image on Modal and saves the resulting image ID locally. See the [setup guide](USAGE.md#prepare-images-with-magicmtn).

## Build and install

Install [MagicMtn](https://github.com/useMagicMtn/magicmtn#install-from-source), Rust with edition 2024 support, and Python 3.11 or newer. Keep this checkout beside the `magicmtn` checkout and use the [pinned SDK revision](MODULE.bazel). See the [Python setup](USAGE.md#install-the-modal-sdk) for installing the Modal SDK in the interpreter your host uses.

```sh
test -f .cargo/config.toml || cp .cargo/config.toml.example .cargo/config.toml
./package.sh
magic provider install --file PATH_TO_PACKAGE
```

Replace `PATH_TO_PACKAGE` with the path printed by `package.sh`. Start the local server, then configure the token with `magic --local provider configure modal --secret-file ./modal-credentials.json`. Follow the [setup guide](USAGE.md#prepare-images-with-magicmtn) for image preparation.

## Run a sandbox

Prepare Canonical and create a sandbox from that catalog selection:

```sh
magic --local images prepare canonical --provider modal
sandbox_id=$(magic --local computer new --provider modal --image canonical)
magic --local computer exec "$sandbox_id" -- /bin/echo "Hello from Modal"
magic --local computer terminate "$sandbox_id" --yes
```

See the [usage guide](USAGE.md) for runtime behavior and SDK integration, and [CONTRIBUTING.md](CONTRIBUTING.md) for development and packaging.

## License

[Apache License 2.0](LICENSE).
