# 🚀 Universal Telegram Media Downloader Bot

A high-performance asynchronous Telegram Bot to download videos, shorts, and reels from **YouTube**, **Instagram**, **Facebook**, and more with interactive quality selection.

---

## ✨ Features

- 🔴 **YouTube**: Long videos, Shorts, and Audio extraction (MP3).
- 📸 **Instagram**: Reels, Video posts, IGTV.
- 🔵 **Facebook**: Public Videos, Watch videos, and Reels.
- 📦 **TeraBox**: Auto-resolved and downloaded using custom high-speed CDN API.
- 🎵 **TikTok & 🐦 Twitter/X**: Supported out-of-the-box.
- 🌐 **Universal Non-DRM & Direct Streams**: Reddit, Pinterest, Vimeo, Twitch, and direct `.mp4` URLs.
- 🎛 **Interactive Quality Selection**:
  - `⚡ Best Quality (<50MB)`
  - `🎬 720p HD` / `📺 480p SD` / `📱 360p Low`
  - `🎵 Audio Only (MP3)`
- ⏱ **Metadata & Preview**: Displays title, author, duration, and thumbnail preview before downloading.
- 👥 **Group & Supergroup Support**: Threaded message replies and isolated user quality panel protection.
- 🚦 **Concurrency Overflow Protection**: Instant server busy notification when max downloads (2) are running.
- 🔄 **Admin `/refresh` Command**: Instant cancellation of stuck/previous tasks and temporary storage cleanup.
- 🧹 **Auto-Cleanup**: Automatically purges temporary files to save disk space.
- 🛡 **Smart File Limit Handling**: Alerts users if media exceeds Telegram's standard 50MB Bot API limit.

---

## 🛠️ Requirements & Setup

### 1. Prerequisites
- **Python 3.10+** (Tested on Python 3.12)
- **Telegram Bot Token** from [@BotFather](https://t.me/botfather)

---

### 2. Installation

1. Open PowerShell or Command Prompt in this folder:
   ```powershell
   cd "c:\Users\mousu\OneDrive\Desktop\downloader tg"
   ```

2. Create and activate a Python Virtual Environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install required dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

4. Configure your Telegram Bot Token:
   - Open the `.env` file in your text editor.
   - Replace `YOUR_TELEGRAM_BOT_TOKEN_HERE` with your actual Bot Token from BotFather:
     ```env
     BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRstuVWXyz
     ```

---

### 3. Run the Bot

```powershell
python bot.py
```

Once running, you should see:
```
✅ Bot is online and listening for messages! Press Ctrl+C to stop.
```

---

## 📱 How to Use

1. Open your bot on Telegram and press `/start`.
2. Send or forward any video link (YouTube, Instagram Reel, Facebook Video/Reel).
3. The bot will fetch the video details and present buttons for quality selection.
4. Tap your preferred quality, and the bot will download and send it directly to your chat!

If a standard quality fails for a particular link, tap **Available formats**.
The bot will show up to eight real video formats found by yt-dlp for that URL;
selecting a video-only stream automatically combines it with the best available
audio stream.

---

## ⚙️ Advanced Settings (`.env`)

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *Required* | Your Telegram Bot Token from @BotFather |
| `TELEGRAM_API_ID` | *Optional* | Telegram API ID from [my.telegram.org](https://my.telegram.org) (Unlocks 2GB uploads!) |
| `TELEGRAM_API_HASH` | *Optional* | Telegram API Hash from [my.telegram.org](https://my.telegram.org) (Unlocks 2GB uploads!) |
| `MAX_FILE_SIZE_MB` | `2000` (MTProto) / `50` (Standard) | Maximum file size limit in MB |
| `DOWNLOAD_DIR` | `downloads` | Local folder for temporary downloads |

---

## ⚡ Unlocking 2GB Uploads via MTProto (Optional)

By default, the standard Telegram HTTP Bot API limits file uploads to **50 MB**.
You can instantly unlock **2,000 MB (2 GB)** uploads with live percentage progress:

1. Go to **[my.telegram.org](https://my.telegram.org)** and log in with your phone number.
2. Click **API development tools** and create an app (App title: `MyDownloader`, Short name: `mydownloader`).
3. Copy your `api_id` and `api_hash`.
4. Open `.env` and paste them:
   ```env
   TELEGRAM_API_ID=12345678
   TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
   ```
5. Restart your bot! The bot will now upload videos up to **2 GB** directly over Telegram's MTProto protocol.

---

## 🍪 Managing Cookies Directly From Telegram Chat (Easy & Fast)

You don't even need to SSH into your server to update cookies! You can manage them directly inside your Telegram chat:

### 1. Send Cookie File as a Telegram Document
Simply drag-and-drop or send any `.txt` cookie file directly to your Telegram bot with one of these captions:
- Caption `youtube` → Saves to YouTube cookies (`cookies.txt`)
- Caption `instagram` → Saves to `cooky/instagram/cookies.txt`
- Caption `facebook` → Saves to `cooky/facebook/cookies.txt`
- Caption `terabox` → Saves to `cooky/terabox/cookies.txt`
- Caption `generic` → Saves to `cooky/generic/cookies.txt`

### 2. Cookie Commands
- **`/cookiestatus`**: Check which platform cookies are currently active, their file size, and line count.
- **`/setcookie <platform> <raw_cookie_text>`**: Paste cookie text directly in chat.
- **`/clearcookie <platform>`**: Delete cookies for a specific platform.

---

## 🛑 Admin Control Commands

Only the bot administrator (`ADMIN_USER_ID`) can trigger these maintenance & authorization commands:

- **`/help`**: Full developer & admin control panel in DMs (all cookie commands, group controls, server limits).
- **`/startgc`**: Activates and authorizes the bot in the current group chat.
- **`/stopgc`**: Deactivates the bot in the current group chat.
- **`/gchelp`**: Member command guide (usable by anyone in authorized group chats).
- **`/refresh`** (or `/killtasks` / `/reset`): Cancels all active download tasks, cleans temporary files from `downloads/`, clears URL cache, and resets concurrency slots to `2/2` instantly.

*(Make sure to set `ADMIN_USER_ID=your_telegram_id` in `.env` so only you can use these commands).*

---

## 👥 Group Chat Setup & Bot Privacy Mode

To allow the bot to **automatically detect links sent in group chats** without needing to type `/dl`:

1. Open **[@BotFather](https://t.me/botfather)** on Telegram.
2. Send `/setprivacy`.
3. Choose your bot from the list.
4. Select **`Disable`**. *(This grants the bot permission to read links sent directly by group members).*
5. Add the bot to your group and run **`/startgc`** to activate it!
6. Each link sent in the group will display a **`[ ⏹️ Stop / Cancel ]`** button so the requester or admin can cancel anytime.

---

## 🧩 Human-in-the-Loop (HITL) Remote CAPTCHA Solver (Docker)

If TeraBox triggers a slider CAPTCHA (`need verify_v2`), the bot launches an interactive headless browser on your EC2 and sends a secure **Live Inspector URL** to you on Telegram. You slide the puzzle on your phone or PC, and the bot automatically extracts the session tokens, saves them, and continues the download.

### 1. Run Browserless via Docker on EC2:
```bash
docker run -d \
  --name browserless \
  -p 3000:3000 \
  --restart unless-stopped \
  -e "MAX_CONCURRENT_SESSIONS=2" \
  -e "CONNECTION_TIMEOUT=300000" \
  -e "TOKEN=my_secure_token_123" \
  ghcr.io/browserless/chromium:latest
```

### 2. Configure `.env` on EC2:
```env
BROWSERLESS_URL=http://127.0.0.1:3000
BROWSERLESS_PUBLIC_URL=http://YOUR_EC2_PUBLIC_IP:3000
BROWSERLESS_TOKEN=my_secure_token_123
CAPTCHA_TIMEOUT_SEC=180
```
*(Make sure port `3000` is open in your AWS EC2 Security Group inbound rules).*
