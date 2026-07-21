# Releasing the WatchDock Agent

## How to Release

### Option 1: Tag (recommended — triggers GitHub Actions automatically)

```bash
# 1. Bump agent_version in agent.py (line ~326)
# 2. Commit your changes
git add -p
git commit -m "Release vX.Y.Z: <summary>"

# 3. Tag and push
git tag -a agent-vX.Y.Z -m "Release agent vX.Y.Z"
git push origin main && git push origin agent-vX.Y.Z
```

GitHub Actions will build the tarball, create the GitHub release, and upload the artefacts automatically.

### Option 2: Manual trigger

Go to **Actions → Release Agent → Run workflow** and enter the version number.

### Option 3: Build locally

```bash
./build_release.sh 1.2.0
# Creates: platform-obs-agent-1.2.0.tar.gz
```

---

## Version Numbering

`MAJOR.MINOR.PATCH` — follow [semver](https://semver.org/):

| Bump | When |
|------|------|
| PATCH | Bug fixes |
| MINOR | New features (backward compatible) |
| MAJOR | Breaking changes |

---

## Pre-release Checklist

- [ ] `agent_version` in `agent.py` matches the tag
- [ ] All changes committed and pushed to `main`
- [ ] No sensitive data or credentials in the diff

---

## Baking a VM image with the agent pre-installed

Since 1.4.0 the agent stores a persistent `agent_id` in `agent_config.json`, and that is how the backend tells one server from another. If you snapshot or create an AMI from a box that already has the agent installed, the image carries that `agent_id` (and `/etc/machine-id`), so every VM cloned from it reports the same identity and they all collapse into one server record. The extra boxes then look invisible in the dashboard.

Before creating the image, clear both identifiers so each clone generates its own on first boot:

```bash
sudo systemctl stop watchdock-agent
# Drop the agent's pinned identity
sudo python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/opt/watchdock-agent/agent_config.json")
c = json.loads(p.read_text())
c.pop("agent_id", None)
p.write_text(json.dumps(c, indent=2))
PY
# Drop the systemd machine-id so the OS regenerates it per clone
sudo truncate -s 0 /etc/machine-id
sudo rm -f /var/lib/dbus/machine-id
```

On first boot each clone regenerates `/etc/machine-id`, the agent finds no `agent_id`, adopts that fresh machine-id, and registers as its own server. This only matters for image/snapshot workflows; installing the agent per box already gives each one a unique `agent_id`.
