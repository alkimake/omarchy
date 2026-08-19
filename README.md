# Omarchy Applications

Personal applications and integrations for Omarchy. This repository is standalone: it does not fork or replace the Omarchy installation under `/usr/share/omarchy`.

## Applications

### agents-kimi

Adds Kimi Code token history and membership limits to Omarchy's stock Agents panel.

```bash
./setup.sh agents-kimi
```

Remove it with:

```bash
./setup.sh --uninstall agents-kimi
```

Running `./setup.sh` without an application name installs every application in this repository. Installation uses symlinks into user-owned directories, so pulling repository updates changes the linked application without copying files into the Omarchy package.

See [apps/agents-kimi/README.md](apps/agents-kimi/README.md) for behavior, authentication, and troubleshooting.

## Tests

```bash
python -m unittest discover -s apps/agents-kimi/tests -p 'test_*.py' -v
bash apps/agents-kimi/tests/test_setup.sh
```
