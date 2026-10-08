# ROMForge Self-Hosted Fleet Runbook

This runbook guides you through deploying and configuring dedicated high-performance **Self-Hosted Fleet Runners** for ROMForge.

---

## 1. Architectural Posture

ROMForge is designed around a **hybrid dual-tier build topology**:

| Tier | Infrastructure | Execution Model | Build Time |
| :--- | :--- | :--- | :--- |
| **Tier 1: Cloud-Hosted ($0)** | GitHub Standard Hosted Runners (4 vCPUs) | 20-Candidate Silicon Mining Matrix (EPYC Zen5 Turin / Xeon Granite Rapids) + Fusion Slots + State Relaying | ~4–8 hours total relay across 1–2 runs |
| **Tier 2: Self-Hosted Fleet** | Dedicated Bare-Metal / High-Core VPS (16–64 vCPUs, 64–256 GB RAM) | Deterministic Zero-Lottery Routing (`runs-on: [self-hosted, romforge]`), Direct Fusion Compilation | **~25–45 minutes total cold build** |

When a self-hosted runner with the label `romforge` is online, ROMForge's `plan` workflow job **automatically routes all compilation slots to the fleet** with zero mining overhead (`mining: ["solo"]`). If the fleet runner is offline or busy, ROMForge gracefully falls back to GitHub-hosted silicon mining.

---

## 2. Hardware Recommendations

| Component | Minimum | Recommended | Extreme / Enterprise |
| :--- | :--- | :--- | :--- |
| **CPU** | 8 Cores / 16 Threads (Zen3 / Intel 12th Gen) | 16 Cores / 32 Threads (AMD Ryzen 9 7950X / 9950X / EPYC Zen4) | 32–64 Cores (AMD Threadripper 7000 / EPYC 9004) |
| **RAM** | 32 GB DDR4/DDR5 | 64 GB DDR5 + 32 GB zRAM | 128 GB+ DDR5 + 64 GB zRAM |
| **Storage** | 1 TB NVMe SSD (PCIe 3.0, 3000 MB/s) | 2 TB Gen4 NVMe SSD (PCIe 4.0, 7000 MB/s, Btrfs zstd:1) | 4 TB Gen5 NVMe SSD / Enterprise U.2 (Btrfs zstd:1) |
| **Network** | 500 Mbps Up/Down | 1 Gbps+ Symmetrical | 10 Gbps Symmetrical |

---

## 3. OS & Kernel Configuration

### Operating System
- **Recommended**: Ubuntu 24.04 LTS (Noble Numbat) or Ubuntu 22.04 LTS (Jammy Jellyfish).
- Linux Kernel: 6.8+ (supports modern btrfs zstd improvements and zram multi-streams).

### System & Kernel Limits (`/etc/sysctl.d/99-romforge.conf`)
Add the following system parameters to optimize memory and file descriptor handling under extreme compilation load:

```ini
# /etc/sysctl.d/99-romforge.conf
fs.file-max = 2097152
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 8192
vm.max_map_count = 1048576
vm.swappiness = 100
vm.watermark_boost_factor = 0
vm.watermark_scale_factor = 125
vm.page-cluster = 0
```
Apply settings:
```bash
sudo sysctl --system
```

---

## 4. Storage Setup: Btrfs with Transparent zstd Compression

Android builds generate ~100–150 GB of uncompressed data with tremendous redundancy (ELF binaries, intermediates, duplicate jars). Formatting the dedicated build volume with **Btrfs** and `zstd:1` compression cuts physical I/O in half and accelerates compile speeds.

### Mount Configuration
Create dedicated mount `/mnt/romforge`:
```bash
# Format dedicated NVMe partition (replace /dev/nvme0n1p2 with your target partition)
sudo mkfs.btrfs -f -L romforge_build /dev/nvme0n1p2

# Create mount directory
sudo mkdir -p /mnt/romforge /opt/romforge

# Add to /etc/fstab for persistent mount
UUID=$(sudo blkid -s UUID -o value /dev/nvme0n1p2)
echo "UUID=$UUID /mnt/romforge btrfs defaults,noatime,compress-force=zstd:1,space_cache=v2,discard=async 0 0" | sudo tee -a /etc/fstab

# Mount volume and set open permissions
sudo mount /mnt/romforge
sudo chmod 1777 /mnt /mnt/romforge /opt /opt/romforge
```

---

## 5. Memory & zRAM Configuration

Enable **zRAM** with zstd compression to guarantee zero-OOM resilience during memory-intensive linking (e.g. `libart.so`, `libandroid_runtime.so`, metalava).

```bash
sudo modprobe zram num_devices=1
echo zstd | sudo tee /sys/block/zram0/comp_algorithm
# Allocate 32 GB compressed swap memory
echo 32G | sudo tee /sys/block/zram0/disksize
sudo mkswap /dev/zram0
sudo swapon -p 100 /dev/zram0
```

---

## 6. GitHub Actions Runner Installation & Registration

### Step 1: Create Runner User
```bash
sudo useradd -m -s /bin/bash runner
sudo usermod -aG sudo runner
echo "runner ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/runner
```

### Step 2: Download & Extract Runner
```bash
sudo su - runner
mkdir -p actions-runner && cd actions-runner
RUNNER_VERSION="2.321.0"
curl -o actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz -L https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
tar xzf actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
rm actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
```

### Step 3: Register Runner with `romforge` Label
Obtain a runner registration token from your GitHub Repository (**Settings → Actions → Runners → New runner**):

```bash
./config.sh \
  --url https://github.com/<YOUR_GITHUB_ORG_OR_USER>/<YOUR_REPO> \
  --token <YOUR_REGISTRATION_TOKEN> \
  --name "romforge-node-01" \
  --labels "self-hosted,linux,x64,romforge" \
  --work "/mnt/romforge/_work" \
  --unattended \
  --replace
```

### Step 4: Install and Start Systemd Service
```bash
sudo ./svc.sh install runner
sudo ./svc.sh start
sudo ./svc.sh status
```

---

## 7. Verifying Fleet Integration

Once the runner service is active:
1. Navigate to **Actions → ROMForge — universal build → Run workflow**.
2. Run a build on `qassa-a10` or any configured ROM.
3. Observe the `plan` step logs:
   ```text
   🚀 Self-hosted romforge fleet runner detected! Routing jobs to fleet with zero lottery.
   slot_runner: ["self-hosted", "romforge"]
   mining: ["solo"]
   ```
4. Slices will execute directly on your bare-metal runner without candidate discard overhead, completing in single-pass fusion cycles.
