# 🚀 Universal Telegram Media Downloader Bot

A high-performance asynchronous Telegram Bot to download videos, shorts, and reels from **YouTube**, **Instagram**, **Facebook**, and more with interactive quality selection.

---

## ✨ Features

- 🔴 **YouTube**: Long videos, Shorts, and Audio extraction (MP3).
- 📸 **Instagram**: Reels, Video posts, IGTV.
- 🔵 **Facebook**: Public Videos, Watch videos, and Reels.
- 🎵 **TikTok & 🐦 Twitter/X**: Supported out-of-the-box.
- 🎛 **Interactive Quality Selection**:
  - `⚡ Best Quality (<50MB)`
  - `🎬 720p HD` / `📺 480p SD` / `📱 360p Low`
  - `🎵 Audio Only (MP3)`
- ⏱ **Metadata & Preview**: Displays title, author, duration, and thumbnail preview before downloading.
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

---

## ⚙️ Advanced Settings (`.env`)

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *Required* | Your Telegram Bot Token from @BotFather |
| `MAX_FILE_SIZE_MB` | `50` | Maximum file size allowed by Telegram Bot API (50 MB) |
| `DOWNLOAD_DIR` | `downloads` | Local folder for temporary downloads |

---

## 🍪 Instagram & Facebook Cookies (Optional)
If you want to download private or restricted posts from Instagram/Facebook, place a Netscape format `cookies.txt` file in the root folder. The bot will automatically detect and use it for authenticated requests.
