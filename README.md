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

### docker-compose Update Module

OTA deployments of the retina-node stack run through a fork of Mender's packaged
`docker-compose` Update Module. The packaged file is moved aside with
`dpkg-divert` rather than overwritten, so apt stays consistent and the upstream
version is kept for comparison. The fork changes four things:

- **Images are loaded before the running stack is stopped.** The packaged module
  stops first and loads second, which held the radar down for the length of the
  image load. Loading first cut a measured outage from around 148 seconds to
  about 11.
- **It takes the restart lock** in `/data/retina-gui/restart.lock`, so an update
  cannot interleave with a restart started by retina-gui or the RSPduo watchdog.
- **A failed install no longer stops the stack it was replacing.** The packaged
  module ran `down` against the new composition while the old one was still
  live, and because both share one compose project name that took the running
  stack down with it.
- **A rollback with nothing staged does nothing.** See below.

#### Rollback states

`rollback_artifact` can be entered in four different store states, and what is
safe depends on which. The store lives at `/data/mender-docker-compose`.

| State | Directories present | Correct action |
| --- | --- | --- |
| Nothing staged (e.g. Download cut short) | `current/` | leave the running stack alone |
| After commit, before `Cleanup` | `current/` + `cleanup/` | rotate, stop the new one, restart the old |
| Install failed after the rotation | `new/` + `previous/` | stop `new/`, restart `previous/` |
| Install cut short before the rotation | `new/` + `current/` | leave the running stack alone |

The first row is the one upstream gets wrong, and it is not hypothetical: a power
cut during the Download phase left a node with no stack at all. Upstream's
post-commit branch only asks whether `new/` and `previous/` are absent, which is
equally true when nothing was ever staged. It renames the live `current/` to
`new/` and stops it, nothing is restored, and the module still exits 0, so the
deployment is recorded as successfully rolled back on a node that is no longer
running anything.

`cleanup/` is what separates the two: `commit_artifact` leaves the superseded
composition there, so a genuine post-commit rollback always has it. The guard
therefore returns early only when `new/`, `previous/` and `cleanup/` are all
absent and `current/` is present.

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
