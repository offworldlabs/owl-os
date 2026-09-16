# Owl OS

Mender-enabled OS images for Raspberry Pi 5 radar nodes. The image comes pre-loaded with Docker, SDRplay API, Avahi mDNS, and a WiFi captive portal for easy setup.

##  Contents

- **Docker CE** with Compose plugin
- **SDRplay API v3.15** for RSPDuo hardware
- **SDRconnect v1.0.5** for standalone SDR analysis
- **Chrony** for NTP clock disciplining
- **Cloudflared** for secure tunneling
- **Avahi mDNS** for `ret<node_id>.local` per-node discovery, plus a shared `owl.local`
- **WiFi Connect** captive portal for network setup
- **Mender client** for OTA updates

## Quick Start

1. **Flash the image** to an SD card (64GB+) using Raspberry Pi Imager
   - Download from [Releases](https://github.com/offworldlabs/owl-os/releases)
   - Select `owl-os-vx.x.x.img` as custom OS
   - Do not apply OS customisation settings

2. **Boot and connect to WiFi**
   - Connect to the `node-setup` WiFi network
   - Captive portal opens automatically (or go to http://192.168.42.1)
   - Enter your WiFi credentials - node reboots and connects

3. **Accept device in Mender**
   - Node appears as "pending" once online
   - Accept to enable OTA updates

4. **Deploy [retina-node](https://github.com/offworldlabs/retina-node) stack** via Mender OTA

## Mender Cloud Services

Cloud services (mender-authd, mender-updated, mender-connect) are handled differently between the two build artifacts:

- **`.img` (fresh flash)** — cloud services are **disabled** by default. `mender.conf` is backed up to `/data/mender-cloud-disabled/` and the three Mender systemd services are masked. The user must consent via the retina-gui install flow to enable them.
- **`.mender` (OTA artifact)** — cloud services are **re-enabled** via `debugfs` in the `image2mender` post-processing step. This is required because `mender-updated` must start after reboot to run `ArtifactVerifyReboot` and commit the update — without it the update hangs and rolls back.

After install, users can toggle cloud services on/off from `http://owl.local`. The preference persists across reboots and OTA updates. Toggling is blocked while any update is in progress.

### Install Lock

During a retina-node install, retina-gui holds an `install.lock` in `/data/retina-gui/` to prevent concurrent installs and block cloud service toggling mid-update. The lock is released on completion (success or failure) with a 40-minute stale timeout as a safety net. Mender state scripts write a separate `mender-update.status` file for real-time progress polling (downloading → installing → done).

## Configuration

### Node Configuration

After deploying retina-node, visit `http://owl.local` to configure capture settings, location, ADS-B truth source, and tar1090. See [retina-node](https://github.com/offworldlabs/retina-node) for details.

### Addressing nodes

Each node has a permanent name of its own, `ret<node_id>.local`, derived from the Mender node_id and unchanged for the life of the board. It is shown under **Configuration > This node** and is the address to bookmark.

`owl.local` is a shared entry point published by *every* node at once, so it reaches whichever one answers first. With a single node on the network it redirects to that node. With more than one it shows a list of every node found, each linking to its own `ret<node_id>.local`. A node dropping off the network does not take `owl.local` with it — the others were already answering it.

Use `owl.local` to find a node, and `ret<node_id>.local` to work with one. In particular **do not use `owl.local` for SSH**: it can resolve to a different node between connections, which will trip `REMOTE HOST IDENTIFICATION HAS CHANGED`.

Nodes can be given a friendly name under **Configuration > This node**. It is only a label for the node list, so renaming never breaks a bookmark or an SSH config. That section also shows the node's own `ret<node_id>.local` address.

### Cloudflare Tunnel (Optional)

To enable Cloudflare tunnel forwarding, create a token file on the node:

```bash
sudo mkdir -p /data/cloudflared
echo "YOUR_TUNNEL_TOKEN" | sudo tee /data/cloudflared/tunnel-token
sudo chmod 600 /data/cloudflared/tunnel-token
sudo systemctl restart cloudflared
```

The token persists across OTA updates.

### SSH Access

**End users:** Add your SSH key via the web GUI at `http://owl.local` after boot. Once added, connect with:
```bash
ssh node@ret<node_id>.local
# or by IP
ssh node@<ip-address>
```

Always SSH to the node's own `ret<node_id>.local`, never to `owl.local` — that name is answered by every node on the network, so which host you land on can change between connections and SSH will refuse on the host key mismatch. **Configuration > This node** shows its address.

Keys persist across reboots and OTA updates.

**Developers:** Public keys can be baked into the image at build time by adding them to `ssh_pub_keys/`:
```bash
cp ~/.ssh/id_ed25519.pub ssh_pub_keys/yourname.pub
```

## Creating a Release

Tag a commit with `os-vx.x.x` and push:
```bash
git tag os-v1.0.0
git push origin os-v1.0.0
```

This triggers the GHA workflow (`.github/workflows/build_os.yml`) which:
1. Builds OS image and Mender artifact
2. Uploads to GitHub Releases
3. Uploads Mender artifact to OffWorld Lab Mender server

> **Note:** Currently triggers on any `os-v*` tag. TODO: Change to only PR merges into main.

## SDRconnect

Run in server mode (headless device):
```bash
/opt/sdrconnect/SDRconnect --server
```

Connect from a SDRconnect client on another machine using the Pi's IP.

> **Warning:** Conflicts with blah2 - stop containers first:
> ```bash
> cd /data/mender-docker-compose/current/manifests && docker compose -p retina-node down
> ```

### Mender Tenant Token

**For GitHub Actions:** tenant token is added via GH secrets.

**For local builds:** Create a custom config file:
```bash
echo 'mender_tenant_token: "YOUR_TOKEN_HERE"' > configuration/mender/mender_custom.yml
```

This file is `.gitignore`d to prevent accidental commits.

## Building from Source

### Requirements

Install [EDI](https://docs.get-edi.io/en/stable/getting_started_v2.html) and dependencies:

**Ubuntu 24.04 or newer:**
```bash
sudo apt install buildah containers-storage crun curl distrobox \
  dosfstools e2fsprogs fakeroot genimage git mender-artifact \
  mmdebstrap mtools parted python3-sphinx python3-testinfra \
  podman rsync zerofree
```

### Build
```bash
edi -v project make owl-os-pi5.yml
```

The Pi 4B image configuration is `owl-os-pi4.yml` with its matching
`configuration/overlay/owl-os-pi4.global.yml`. It uses the existing arm64 Pi 4
boot specification and leaves the managed radar stack on its current Compose
manifest by default. This is an image-build option; an adapted card does not
prove fresh-image boot.

The Pi 4 GPU managed-stack selector is a separate, explicit
`owl_pi4_gpu_stack: True` playbook parameter. Enable it only with a reviewed
versioned override from the paired `blah2-arm` change installed at
`/data/retina-node/compose/pi4-gpu.override.yml`. Set the required
`owl_pi4_compose_override_src` playbook parameter to the build-side file
produced by `blah2-arm/deploy/pi4/render-override.py`; the OWL role copies its
literal content into that persistent location, owned by root. That override must select an
approved immutable radar image ID or registry digest; this repository does not
invent a published image tag. The selector installs root-owned
`/etc/owl/retina-compose.env` so boot, GUI operations, and the watchdog use the
base manifest plus the persistent override for the one `retina-node` project.
It preserves the existing `config-merger` service. The boot and watchdog
preflight fails if either file is absent, Compose rejects the result, or the
effective radar/API image, labels, GPU selection and render-device mapping
do not match the opt-in contract, including
after Mender replaces the managed base manifest. The GUI remains available for
setup when a manifest is absent, but its Compose actions fail until the files
are valid. To return to the default path, build without the opt-in parameter;
do not leave stale drop-ins installed on an adapted card.
For the pinned `mender-docker-compose` 1.0.0 update module, the role sets its
supported `DOCKER_COMPOSE_CMD` option through a managed block in
`/etc/mender/mender-docker-compose.conf`, preserving other settings. The small
wrapper applies the same persistent override to the module's `new`, `current`,
or `previous` manifest slot, including the `ps` health query issued outside
the manifest directory. A local mock executes the installed module's install
and rollback paths. On-device managed-update behavior still needs validation;
the updater tracks only base-manifest image IDs for cleanup, so the approved
override images must remain preloaded and must not be blindly pruned.
Run the local selector checks with
`python3 -m unittest discover -s plugins/playbooks/os_setup/roles/pi4_compose_selection/tests -v`.
That command skips only the installed-module integration when the default
`/usr/share/mender/modules/v3/docker-compose` is absent. To require the
captured installed-module install/rollback fixture, set
`OWL_MENDER_MODULE=/absolute/path/to/captured-1.0.0-script` for the same
command; an invalid explicit path fails. The test prints the module SHA-256.

**Output artifacts:**
- `owl-os-vx.x.x.img` - Flashable OS image with A/B partitioning
- `owl-os-vx.x.x.mender` - OTA update artifact

### Clean
```bash
edi -v project clean owl-os-pi5.yml
```

## Default Credentials

- **Username:** `node`
- **Password:** `raspberry`

## Credits

Built using [EDI-PI](https://github.com/lueschem/edi-pi) by [lueschem](https://github.com/lueschem).
