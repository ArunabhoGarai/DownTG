# 🌐 AWS EC2 Ubuntu Deployment & Resource Guide

This guide details how to deploy the Telegram Downloader Bot on an **AWS EC2 Ubuntu** instance while ensuring it **never hampers or starves other processes** (like web servers, databases, or existing bots) running on the same server.

---

## 📊 1. Resource Consumption Breakdown

| Resource | Idle State | Active Download & Upload | Impact on other processes |
|---|---|---|---|
| **RAM (Memory)** | ~45 – 70 MB | ~120 – 250 MB | Low. Controlled by internal semaphore and systemd memory limits. |
| **CPU** | ~0.1% | ~10 – 40% (for 2–5 sec during remuxing/MP3 encode) | Kept low because video streams are remuxed rather than re-encoded. `Nice=10` ensures other processes have higher priority. |
| **Disk Space** | < 100 MB | Transient (equal to video size < 50 MB) | Zero permanent growth; temporary files are automatically deleted immediately after upload. |
| **Network (I/O)** | Negligible | Download speed + Upload speed | Bandwidth shared across instance. |

---

## 🛡️ 2. How We Safeguard Your Other Server Processes

1. **Concurrency Throttle (`MAX_CONCURRENT_DOWNLOADS=2`)**:
   - Limits how many videos are downloaded simultaneously. If multiple users request downloads at the same second, they are queued gracefully.
2. **Process Priority (`Nice=10`)**:
   - Gives the bot lower CPU scheduling priority than your web servers (Nginx/Node/Apache/MySQL/Postgres). The Linux kernel will always prioritize your other services first.
3. **Hard Memory & CPU Caps (`systemd cgroups`)**:
   - `MemoryMax=350M` prevents the bot from consuming excessive RAM or triggering OOM (Out Of Memory) crashes.
   - `CPUQuota=60%` limits the bot from using more than 60% of a single CPU core.
4. **Swap Space Protection**:
   - Creating a 1–2GB swap file ensures micro/small instances never kill existing services under memory spikes.

---

## 🚀 3. Step-by-Step EC2 Ubuntu Setup

### Step A: Connect to your EC2 Instance
```bash
ssh -i your-key.pem ubuntu@YOUR_EC2_PUBLIC_IP
```

### Step B: (Optional but Recommended) Setup a 1GB Swap File
*(If you haven't set up swap on your `t2.micro` or `t3.micro`, run this once):*
```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Step C: Install System Packages
```bash
sudo apt update && sudo apt install -y python3-pip python3-venv ffmpeg git
```

### Step D: Clone or Upload Your Bot
```bash
# Example: create bot directory
mkdir -p ~/downloader_tg
cd ~/downloader_tg
```
*(Copy your files or `git clone` into `~/downloader_tg`)*

### Step E: Set Up Virtual Environment & Dependencies
```bash
cd ~/downloader_tg
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step F: Configure Environment Variables
Create or edit `.env`:
```bash
nano .env
```
Paste your settings:
```env
BOT_TOKEN=your_telegram_bot_token_from_botfather
MAX_FILE_SIZE_MB=50
MAX_CONCURRENT_DOWNLOADS=2
DOWNLOAD_DIR=downloads
```
Save with `Ctrl+O`, `Enter`, and exit with `Ctrl+X`.

---

## 🔄 4. Run as a Background Service with Systemd

To keep the bot running 24/7 with automatic restart and resource throttling:

1. Copy the systemd service file:
```bash
sudo cp tgbot.service /etc/systemd/system/tgbot.service
```

2. Reload systemd daemon and enable service on boot:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tgbot.service
sudo systemctl start tgbot.service
```

3. Check bot status:
```bash
sudo systemctl status tgbot.service
```

4. View live logs anytime:
```bash
journalctl -u tgbot.service -f
```

---

## 🛑 Management Commands

- **Restart Bot**: `sudo systemctl restart tgbot`
- **Stop Bot**: `sudo systemctl stop tgbot`
- **View Logs**: `journalctl -u tgbot -n 50 --no-pager`

### 🍪 Isolated Platform Cookies Setup

You can update cookies directly from your Telegram chat or via EC2 terminal:

#### Option 1: Direct in Telegram Chat (Easiest)
- Send any `cookies.txt` file as a document to your bot with a caption:
  - `youtube`
  - `instagram`
  - `facebook`
  - `generic`
- Check active cookies anytime with `/cookiestatus`.

#### Option 2: Via EC2 Terminal
- **🔴 YouTube**: `/home/ubuntu/DownTG/DownTG/cookies.txt`
- **📸 Instagram**: `/home/ubuntu/DownTG/DownTG/cooky/instagram/cookies.txt`
- **🔵 Facebook**: `/home/ubuntu/DownTG/DownTG/cooky/facebook/cookies.txt`
- **🌐 Generic**: `/home/ubuntu/DownTG/DownTG/cooky/generic/cookies.txt`
